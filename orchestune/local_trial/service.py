"""High-level orchestration service for local trial execution (#1734, #1737)."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal

from orchestune.infra.local_trial_agent import LocalAgentRunner
from orchestune.infra.managed_process import (
    ManagedProcessSpec,
    ProcessRunner,
    get_default_runner,
)
from orchestune.infra.process_utils import is_process_alive
from orchestune.local_trial.contracts import (
    AgentRunner,
    TrialError,
    TrialStore,
    TrialWorkspace,
    canonical_digest,
    plan_digest,
    request_digest,
)
from orchestune.local_trial.models import (
    SCHEMA_VERSION,
    AgentInfo,
    AttemptInput,
    AttemptKind,
    AttemptResult,
    Budget,
    Checkpoint,
    ControllerInfo,
    Decision,
    DisciplineStatus,
    Evidence,
    GitIntent,
    ImplementationPayload,
    Observation,
    PlanPayload,
    ProcessRecord,
    ProcessStatus,
    RequestSnapshot,
    ReviewPayload,
    ReviewVerdict,
    StopConfirmation,
    TrialPhase,
    TrialPlan,
    TrialState,
    TrialView,
    WorkspaceSnapshot,
)
from orchestune.local_trial.planning import resolve_ci_selection, validate_plan
from orchestune.local_trial.policy import assess_ready, validate_attempt
from orchestune.local_trial.prompts import build_agent_request
from orchestune.local_trial.state import LocalTrialStore
from orchestune.local_trial.workspace import LocalTrialWorkspace, workspace_digest


@dataclass(frozen=True, kw_only=True)
class StartRequest:
    repo: str
    request_text: str
    agent_name: Literal["claude", "codex"] | str | None = None
    ci_argv_override: tuple[str, ...] | None = None


@dataclass(frozen=True, kw_only=True)
class ResumeRequest:
    trial_id: str
    approve_plan: str | None = None
    confirm_stopped: str | None = None
    confirm_worktree: str | None = None
    request_text: str | None = None
    ci_argv_override: tuple[str, ...] | None = None


@dataclass(frozen=True, kw_only=True)
class TrialDependencies:
    store: TrialStore
    workspace: TrialWorkspace
    agents: AgentRunner
    process_runner: ProcessRunner
    clock: Callable[[], str]
    new_id: Callable[[], str]
    controller_alive: Callable[[int | None], bool]


def make_dependencies(repo: str) -> TrialDependencies:
    """Construct production dependencies from repository path."""
    ws = LocalTrialWorkspace()
    primary_root, common_dir = ws.storage_roots(repo)
    store = LocalTrialStore(common_dir=common_dir, primary_root=primary_root)
    runner = get_default_runner()
    agents = LocalAgentRunner(runner=runner)
    return TrialDependencies(
        store=store,
        workspace=ws,
        agents=agents,
        process_runner=runner,
        clock=lambda: datetime.now(UTC).isoformat(),
        new_id=lambda: uuid.uuid4().hex,
        controller_alive=is_process_alive,
    )


def _resolve_agent(agents: AgentRunner, agent_name: str | None) -> AgentInfo:
    detected = agents.detect()
    if agent_name is not None:
        matches = [a for a in detected if a.name == agent_name]
        if not matches:
            raise TrialError(
                "agent_unavailable", f"Specified agent {agent_name} is unavailable"
            )
        return matches[0]
    if len(detected) == 0:
        raise TrialError("agent_unavailable", "No trial agent available")
    if len(detected) > 1:
        raise TrialError(
            "invalid_input", "Multiple agents available; please specify agent_name"
        )
    return detected[0]


def _make_view(
    state: TrialState,
    deps: TrialDependencies,
    snapshot: WorkspaceSnapshot,
    ci_candidates: tuple[tuple[str, ...], ...],
) -> TrialView:
    attempts: list[AttemptResult] = []
    for aid in state.attempt_ids:
        att = deps.store.load_attempt(state.trial_id, aid)
        if att is not None:
            attempts.append(att)
    ready, _ = assess_ready(
        state,
        state.repository.base_sha,
        snapshot,
        deps.workspace.includes_head(state),
    )
    return TrialView(
        state=state,
        effective_phase=state.phase,
        reason=state.reason,
        diff=deps.workspace.diff(state),
        attempts=tuple(attempts),
        next_actions=(),
        worktree_digest=workspace_digest(snapshot),
        ci_candidates=ci_candidates,
        ready_is_current=ready,
    )


def _init_state(
    request: StartRequest,
    deps: TrialDependencies,
    agent: AgentInfo,
    trial_id: str,
    preferred_ci: tuple[str, ...] | None,
) -> tuple[TrialState, tuple[tuple[str, ...], ...]]:
    repo_ident = deps.workspace.discover(request.repo)
    ws_ident = deps.workspace.paths(repo_ident, trial_id)
    now = deps.clock()
    req_snap = RequestSnapshot(
        text=request.request_text,
        digest=request_digest(request.request_text),
        ci_argv_override=preferred_ci,
    )
    budget = Budget(plan_revision=0, cycles_started=0, reviews_started=0)
    controller = ControllerInfo(pid=os.getpid(), started_at=now, token=deps.new_id())
    obs = Observation(started_at=now, updated_at=now, elapsed_seconds=0.0)
    state = TrialState(
        schema_version=SCHEMA_VERSION,
        trial_id=trial_id,
        repository=repo_ident,
        workspace=ws_ident,
        request=req_snap,
        agent=agent,
        plan=None,
        approved_plan_digest=None,
        approved_worktree_digest=None,
        approved_index_digest=None,
        phase=TrialPhase.PREPARING,
        checkpoint=Checkpoint.PREPARE,
        reason=None,
        revision=0,
        budget=budget,
        head_sha=repo_ident.base_sha,
        pending_attempt=None,
        pending_git=None,
        attempt_ids=(),
        ci_evidence=None,
        review_evidence=None,
        findings=(),
        responses=(),
        stop_confirmations=(),
        controller=controller,
        observation=obs,
        decision=None,
    )
    return state, ()


def _determine_plan_reason(plan: TrialPlan) -> str:
    if plan.discipline_status == DisciplineStatus.INCOMPATIBLE:
        return "discipline_incompatible"
    if plan.discipline_status == DisciplineStatus.UNVERIFIED:
        return "discipline_unverified"
    if plan.ci_argv is None:
        return "ci_missing"
    return "approval_required"


def _verify_clean_plan(
    snap_before: WorkspaceSnapshot, snap_after: WorkspaceSnapshot
) -> None:
    if (
        snap_after.head_sha != snap_before.head_sha
        or snap_after.index_digest != snap_before.index_digest
        or snap_after.content_digest != snap_before.content_digest
    ):
        raise TrialError("worktree_dirty", "Worktree modified during planning phase")


def _handle_plan_success(
    state: TrialState,
    res: AttemptResult,
    deps: TrialDependencies,
    preferred_ci: tuple[str, ...] | None,
    attempt_id: str,
) -> TrialState:
    if res.agent_payload is None or not isinstance(
        res.agent_payload.payload, PlanPayload
    ):
        raise TrialError("artifact_invalid", "Expected PlanPayload")
    plan = res.agent_payload.payload.plan
    validate_plan(
        plan,
        state.repository.primary_root,
        state.request.text,
        state.repository.base_sha,
        preferred_ci=preferred_ci,
    )
    return replace(
        state,
        plan=plan,
        pending_attempt=None,
        attempt_ids=(*state.attempt_ids, attempt_id),
        phase=TrialPhase.PLANNED,
        checkpoint=Checkpoint.AWAIT_APPROVAL,
        reason=_determine_plan_reason(plan),
        revision=state.revision + 1,
        observation=replace(state.observation, updated_at=deps.clock()),
    )


def _handle_plan_error(
    state: TrialState,
    err: TrialError,
    deps: TrialDependencies,
) -> TrialState:
    pending = (
        state.pending_attempt
        if err.code == "interrupted"
        else None
    )
    new_state = replace(
        state,
        pending_attempt=pending,
        phase=TrialPhase.NEEDS_ATTENTION,
        reason=err.code,
        revision=state.revision + 1,
        observation=replace(state.observation, updated_at=deps.clock()),
    )
    deps.store.save(new_state, expected_revision=state.revision)
    return new_state


def _run_plan_stage(
    state: TrialState,
    deps: TrialDependencies,
    preferred_ci: tuple[str, ...] | None,
    candidates: tuple[tuple[str, ...], ...],
    snap_before: WorkspaceSnapshot,
) -> TrialView:
    attempt_id = deps.new_id()
    plan_inp = AttemptInput(
        trial_id=state.trial_id,
        attempt_id=attempt_id,
        kind=AttemptKind.PLAN,
        input_head_sha=state.head_sha,
        base_sha=state.repository.base_sha,
        plan_digest=None,
        plan_revision=state.budget.plan_revision,
        cwd=state.workspace.implementation_path,
        artifact_dir=f"{state.workspace.implementation_path}/.orchestune/artifacts/{attempt_id}",
        started_at=deps.clock(),
    )
    state = replace(
        state,
        pending_attempt=plan_inp,
        revision=state.revision + 1,
        observation=replace(state.observation, updated_at=deps.clock()),
    )
    deps.store.save(state, expected_revision=state.revision - 1)

    try:
        req = build_agent_request(
            state, plan_inp, diff="", previous_results=(), ci_candidates=candidates
        )
        res = deps.agents.run(req, state.agent)
        deps.store.record_attempt(res)
        snap_after = deps.workspace.inspect(state.repository, state.workspace)
        _verify_clean_plan(snap_before, snap_after)
        validate_attempt(res, state, AttemptKind.PLAN, state.head_sha, None)
        state = _handle_plan_success(state, res, deps, preferred_ci, attempt_id)
        deps.store.save(state, expected_revision=state.revision - 1)
        return _make_view(state, deps, snap_after, candidates)
    except TrialError as err:
        state = _handle_plan_error(state, err, deps)
        snap_curr = deps.workspace.inspect(state.repository, state.workspace)
        return _make_view(state, deps, snap_curr, candidates)


def start_trial(request: StartRequest, deps: TrialDependencies) -> TrialView:
    """Initialize workspace and state, run initial plan, and pause at await_approval."""
    if not request.request_text or not request.request_text.strip():
        raise TrialError("invalid_input", "request_text cannot be empty")
    agent = _resolve_agent(deps.agents, request.agent_name)
    preferred_ci, candidates = resolve_ci_selection(
        request.repo, request.ci_argv_override
    )
    trial_id = deps.new_id()
    state, _ = _init_state(request, deps, agent, trial_id, preferred_ci)

    with deps.store.lock():
        if deps.store.active_trial_id() is not None:
            raise TrialError("already_active", "Another trial is already active")
        deps.workspace.ensure_storage_ignored(state.repository)
        deps.store.reserve(state)
        deps.store.save(state, expected_revision=None)

        prep_intent = GitIntent(
            operation="prepare",
            pre_head_sha=state.repository.base_sha,
            expected_tree_sha=None,
            target_sha=state.repository.base_sha,
            allowed_paths=(),
        )
        state = replace(
            state,
            pending_git=prep_intent,
            revision=state.revision + 1,
            observation=replace(state.observation, updated_at=deps.clock()),
        )
        deps.store.save(state, expected_revision=state.revision - 1)
        deps.workspace.prepare(state.repository, state.workspace)
        snap_before = deps.workspace.inspect(state.repository, state.workspace)
        state = replace(
            state,
            pending_git=None,
            checkpoint=Checkpoint.PLAN,
            head_sha=snap_before.head_sha,
            revision=state.revision + 1,
            observation=replace(state.observation, updated_at=deps.clock()),
        )
        deps.store.save(state, expected_revision=state.revision - 1)
        return _run_plan_stage(state, deps, preferred_ci, candidates, snap_before)


def _handle_confirmations(
    request: ResumeRequest, state: TrialState, deps: TrialDependencies
) -> TrialState | None:
    if request.confirm_stopped is not None:
        stop_conf = StopConfirmation(
            attempt_id=request.confirm_stopped, recorded_at=deps.clock()
        )
        confirmations = (*state.stop_confirmations, stop_conf)
        pending = state.pending_attempt
        if pending and pending.attempt_id == request.confirm_stopped:
            pending = None
        state = replace(
            state,
            pending_attempt=pending,
            stop_confirmations=confirmations,
            revision=state.revision + 1,
            observation=replace(state.observation, updated_at=deps.clock()),
        )
        deps.store.save(state, expected_revision=state.revision - 1)
        return state

    if request.confirm_worktree is not None:
        snap = deps.workspace.inspect(state.repository, state.workspace)
        if workspace_digest(snap) != request.confirm_worktree:
            raise TrialError("state_invalid", "confirm_worktree digest mismatch")
    return None


def _apply_approval(
    request: ResumeRequest, state: TrialState, deps: TrialDependencies
) -> TrialState:
    if state.plan is None:
        raise TrialError("state_invalid", "Cannot approve trial without plan")
    curr_dig = plan_digest(state.plan, state.request.digest, state.repository.base_sha)
    if request.approve_plan != curr_dig:
        raise TrialError(
            "approval_stale", "Approved plan digest does not match current plan"
        )
    if state.plan.discipline_status != DisciplineStatus.COMPATIBLE:
        raise TrialError(
            "discipline_incompatible", "Plan discipline status is not compatible"
        )
    if state.plan.ci_argv is None:
        raise TrialError("ci_missing", "Plan has no CI command")

    repo_curr = deps.workspace.discover(state.repository.source_root)
    if repo_curr.base_sha != state.repository.base_sha:
        raise TrialError("base_changed", "Repository base SHA changed")
    snap = deps.workspace.inspect(state.repository, state.workspace)
    if snap.changes:
        raise TrialError("worktree_dirty", "Implementation worktree is dirty")

    state = replace(
        state,
        approved_plan_digest=request.approve_plan,
        approved_worktree_digest=workspace_digest(snap),
        approved_index_digest=snap.index_digest,
        checkpoint=Checkpoint.IMPLEMENT,
        phase=TrialPhase.IMPLEMENTING,
        reason=None,
        revision=state.revision + 1,
        observation=replace(state.observation, updated_at=deps.clock()),
    )
    deps.store.save(state, expected_revision=state.revision - 1)
    return state


def _commit_implementation(state: TrialState, deps: TrialDependencies) -> TrialState:
    snap = deps.workspace.inspect(state.repository, state.workspace)
    if not snap.changes:
        state = replace(
            state,
            phase=TrialPhase.NEEDS_ATTENTION,
            reason="no_changes",
            revision=state.revision + 1,
        )
        deps.store.save(state, expected_revision=state.revision - 1)
        return state

    intent = deps.workspace.stage_commit(state)
    state = replace(state, pending_git=intent, revision=state.revision + 1)
    deps.store.save(state, expected_revision=state.revision - 1)
    new_head = deps.workspace.finish_commit(state, intent)
    state = replace(
        state,
        pending_git=None,
        head_sha=new_head,
        checkpoint=Checkpoint.CI,
        revision=state.revision + 1,
    )
    deps.store.save(state, expected_revision=state.revision - 1)
    return state


def _execute_implement(state: TrialState, deps: TrialDependencies) -> TrialState:
    if state.budget.cycles_started >= 3:
        state = replace(
            state,
            phase=TrialPhase.NEEDS_ATTENTION,
            reason="budget_exhausted",
            revision=state.revision + 1,
        )
        deps.store.save(state, expected_revision=state.revision - 1)
        return state

    kind = (
        AttemptKind.IMPLEMENT if state.budget.cycles_started == 0 else AttemptKind.FIX
    )
    budget = replace(state.budget, cycles_started=state.budget.cycles_started + 1)
    att_id = deps.new_id()
    att_inp = AttemptInput(
        trial_id=state.trial_id,
        attempt_id=att_id,
        kind=kind,
        input_head_sha=state.head_sha,
        base_sha=state.repository.base_sha,
        plan_digest=state.approved_plan_digest,
        plan_revision=state.budget.plan_revision,
        cwd=state.workspace.implementation_path,
        artifact_dir=f"{state.workspace.implementation_path}/.orchestune/artifacts/{att_id}",
        started_at=deps.clock(),
    )
    state = replace(
        state, budget=budget, pending_attempt=att_inp, revision=state.revision + 1
    )
    deps.store.save(state, expected_revision=state.revision - 1)

    all_attempts = tuple(
        att
        for aid in state.attempt_ids
        if (att := deps.store.load_attempt(state.trial_id, aid)) is not None
    )
    req = build_agent_request(
        state, att_inp, diff=deps.workspace.diff(state), previous_results=all_attempts
    )
    res = deps.agents.run(req, state.agent)
    deps.store.record_attempt(res)
    validate_attempt(res, state, kind, state.head_sha, state.approved_plan_digest)
    responses = state.responses
    if res.agent_payload is not None and isinstance(
        res.agent_payload.payload, ImplementationPayload
    ):
        responses = (*responses, *res.agent_payload.payload.responses)
    state = replace(
        state,
        pending_attempt=None,
        attempt_ids=(*state.attempt_ids, att_id),
        responses=responses,
    )
    return _commit_implementation(state, deps)


def _run_ci_process(spec: ManagedProcessSpec, deps: TrialDependencies) -> ProcessRecord:
    try:
        proc_res = deps.process_runner.run(spec)
        outcome = (
            ProcessStatus(proc_res.outcome.value)
            if proc_res.outcome.value in ProcessStatus._value2member_map_
            else ProcessStatus.NONZERO_EXIT
        )
        return ProcessRecord(
            outcome=outcome,
            returncode=proc_res.returncode,
            stop_confirmed=proc_res.stop_confirmed,
            elapsed_seconds=proc_res.elapsed_seconds,
            timeout_seconds=proc_res.timeout_seconds,
            stdout_tail=proc_res.stdout_tail,
            stderr_tail=proc_res.stderr_tail,
            detail="",
        )
    except KeyboardInterrupt:
        return ProcessRecord(
            outcome=ProcessStatus.INTERRUPTED,
            returncode=None,
            stop_confirmed=False,
            elapsed_seconds=0.0,
            timeout_seconds=900.0,
            stdout_tail="",
            stderr_tail="",
            detail="Process interrupted",
        )


def _handle_ci_success(
    state: TrialState,
    ci_id: str,
    ci_argv: tuple[str, ...],
    approved_plan_digest: str,
) -> TrialState:
    ci_ev = Evidence(
        attempt_id=ci_id,
        base_sha=state.repository.base_sha,
        head_sha=state.head_sha,
        plan_digest=approved_plan_digest,
        ci_argv_digest=canonical_digest(list(ci_argv)),
        passed=True,
    )
    return replace(
        state,
        pending_attempt=None,
        attempt_ids=(*state.attempt_ids, ci_id),
        ci_evidence=ci_ev,
        checkpoint=Checkpoint.REVIEW,
        revision=state.revision + 1,
    )


def _handle_ci_result(
    state: TrialState,
    ci_id: str,
    proc_rec: ProcessRecord,
    ci_argv: tuple[str, ...],
    approved_plan_digest: str,
) -> TrialState:
    if (
        proc_rec.outcome == ProcessStatus.SUCCESS
        and proc_rec.returncode == 0
        and proc_rec.stop_confirmed
    ):
        return _handle_ci_success(state, ci_id, ci_argv, approved_plan_digest)

    phase: TrialPhase
    reason: str | None
    checkpoint: Checkpoint
    if (
        proc_rec.outcome in (ProcessStatus.START_FAILED, ProcessStatus.TIMED_OUT)
        or proc_rec.stop_confirmed is False
    ):
        phase, reason, checkpoint = (
            TrialPhase.NEEDS_ATTENTION,
            "ci_failed",
            state.checkpoint,
        )
    elif state.budget.cycles_started < 3:
        phase, reason, checkpoint = (
            TrialPhase.FIXING,
            state.reason,
            Checkpoint.IMPLEMENT,
        )
    else:
        phase, reason, checkpoint = (
            TrialPhase.NEEDS_ATTENTION,
            "budget_exhausted",
            state.checkpoint,
        )

    pending_att = (
        state.pending_attempt if proc_rec.stop_confirmed is False else None
    )

    return replace(
        state,
        pending_attempt=pending_att,
        attempt_ids=(*state.attempt_ids, ci_id),
        phase=phase,
        reason=reason,
        checkpoint=checkpoint,
        revision=state.revision + 1,
    )


def _execute_ci(state: TrialState, deps: TrialDependencies) -> TrialState:
    if (
        state.plan is None
        or state.plan.ci_argv is None
        or state.approved_plan_digest is None
    ):
        raise TrialError("ci_missing", "Plan has no CI command")
    ci_argv = state.plan.ci_argv
    approved_plan_digest = state.approved_plan_digest
    ci_id = deps.new_id()
    ci_inp = AttemptInput(
        trial_id=state.trial_id,
        attempt_id=ci_id,
        kind=AttemptKind.CI,
        input_head_sha=state.head_sha,
        base_sha=state.repository.base_sha,
        plan_digest=approved_plan_digest,
        plan_revision=state.budget.plan_revision,
        cwd=state.workspace.implementation_path,
        artifact_dir=f"{state.workspace.implementation_path}/.orchestune/artifacts/{ci_id}",
        started_at=deps.clock(),
    )
    state = replace(state, pending_attempt=ci_inp, revision=state.revision + 1)
    deps.store.save(state, expected_revision=state.revision - 1)

    spec = ManagedProcessSpec(
        args=list(ci_argv),
        cwd=state.workspace.implementation_path,
        timeout_seconds=900,
        stage=f"trial:ci:{state.trial_id}",
        tail_bytes=1048576,
    )
    proc_rec = _run_ci_process(spec, deps)
    ci_res = AttemptResult(
        schema_version=1,
        input=ci_inp,
        process=proc_rec,
        agent_payload=None,
        ended_at=deps.clock(),
    )
    deps.store.record_attempt(ci_res)
    snap_ci = deps.workspace.inspect(state.repository, state.workspace)
    if snap_ci.head_sha != state.head_sha or snap_ci.changes:
        raise TrialError("worktree_dirty", "Worktree modified during CI")

    state = _handle_ci_result(state, ci_id, proc_rec, ci_argv, approved_plan_digest)
    deps.store.save(state, expected_revision=state.revision - 1)
    return state


def _prepare_review_workspace(state: TrialState, deps: TrialDependencies) -> TrialState:
    rev_intent = GitIntent(
        operation="prepare_review",
        pre_head_sha=state.head_sha,
        expected_tree_sha=None,
        target_sha=state.head_sha,
        allowed_paths=(),
    )
    state = replace(state, pending_git=rev_intent, revision=state.revision + 1)
    deps.store.save(state, expected_revision=state.revision - 1)
    deps.workspace.prepare_review(state)
    state = replace(state, pending_git=None, revision=state.revision + 1)
    deps.store.save(state, expected_revision=state.revision - 1)
    return state


def _handle_review_verdict(
    state: TrialState,
    rev_id: str,
    inner_rev: ReviewPayload,
    ci_argv: tuple[str, ...],
    approved_plan_digest: str,
) -> TrialState:
    if inner_rev.verdict == ReviewVerdict.PASS:
        rev_ev = Evidence(
            attempt_id=rev_id,
            base_sha=state.repository.base_sha,
            head_sha=state.head_sha,
            plan_digest=approved_plan_digest,
            ci_argv_digest=canonical_digest(list(ci_argv)),
            passed=True,
        )
        return replace(
            state,
            pending_attempt=None,
            attempt_ids=(*state.attempt_ids, rev_id),
            review_evidence=rev_ev,
            findings=(),
            phase=TrialPhase.READY,
            checkpoint=Checkpoint.DECISION,
            reason=None,
            revision=state.revision + 1,
        )
    return replace(
        state,
        pending_attempt=None,
        attempt_ids=(*state.attempt_ids, rev_id),
        findings=inner_rev.findings,
        ci_evidence=None,
        review_evidence=None,
        phase=TrialPhase.FIXING,
        checkpoint=Checkpoint.IMPLEMENT,
        revision=state.revision + 1,
    )


def _run_review_agent(
    state: TrialState,
    rev_inp: AttemptInput,
    deps: TrialDependencies,
    approved_plan_digest: str,
) -> ReviewPayload:
    all_attempts = tuple(
        att
        for aid in state.attempt_ids
        if (att := deps.store.load_attempt(state.trial_id, aid)) is not None
    )
    rev_req = build_agent_request(
        state, rev_inp, diff=deps.workspace.diff(state), previous_results=all_attempts
    )
    rev_res = deps.agents.run(rev_req, state.agent)
    deps.store.record_attempt(rev_res)
    validate_attempt(
        rev_res, state, AttemptKind.REVIEW, state.head_sha, approved_plan_digest
    )
    if rev_res.agent_payload is None or not isinstance(
        rev_res.agent_payload.payload, ReviewPayload
    ):
        raise TrialError("artifact_invalid", "Expected ReviewPayload")
    return rev_res.agent_payload.payload


def _verify_clean_review(state: TrialState, deps: TrialDependencies) -> None:
    snap_rev = deps.workspace.inspect_review(state)
    if snap_rev.head_sha != state.head_sha or snap_rev.changes:
        raise TrialError("worktree_dirty", "Review worktree modified during review")


def _execute_review(state: TrialState, deps: TrialDependencies) -> TrialState:
    if (
        state.plan is None
        or state.plan.ci_argv is None
        or state.approved_plan_digest is None
    ):
        raise TrialError("state_invalid", "Review requires approved plan and CI")
    ci_argv = state.plan.ci_argv
    approved_plan_digest = state.approved_plan_digest
    if state.budget.reviews_started >= 3:
        state = replace(
            state,
            phase=TrialPhase.NEEDS_ATTENTION,
            reason="budget_exhausted",
            revision=state.revision + 1,
        )
        deps.store.save(state, expected_revision=state.revision - 1)
        return state

    state = _prepare_review_workspace(state, deps)
    rev_id = deps.new_id()
    rev_inp = AttemptInput(
        trial_id=state.trial_id,
        attempt_id=rev_id,
        kind=AttemptKind.REVIEW,
        input_head_sha=state.head_sha,
        base_sha=state.repository.base_sha,
        plan_digest=approved_plan_digest,
        plan_revision=state.budget.plan_revision,
        cwd=state.workspace.review_path,
        artifact_dir=f"{state.workspace.review_path}/.orchestune/artifacts/{rev_id}",
        started_at=deps.clock(),
    )
    budget = replace(state.budget, reviews_started=state.budget.reviews_started + 1)
    state = replace(
        state, budget=budget, pending_attempt=rev_inp, revision=state.revision + 1
    )
    deps.store.save(state, expected_revision=state.revision - 1)

    inner_rev = _run_review_agent(state, rev_inp, deps, approved_plan_digest)
    _verify_clean_review(state, deps)
    state = _handle_review_verdict(
        state, rev_id, inner_rev, ci_argv, approved_plan_digest
    )
    deps.store.save(state, expected_revision=state.revision - 1)
    return state


def _reconcile_pending_operations(
    state: TrialState, deps: TrialDependencies
) -> TrialState:
    if state.pending_git is not None:
        try:
            reconciled_head = deps.workspace.reconcile_commit(state, state.pending_git)
            if reconciled_head is None:
                raise TrialError("execution_unknown", "Reconcile returned no head SHA")
            state = replace(
                state,
                pending_git=None,
                head_sha=reconciled_head,
                checkpoint=Checkpoint.CI,
                revision=state.revision + 1,
            )
            deps.store.save(state, expected_revision=state.revision - 1)
        except (TrialError, OSError) as err:
            raise TrialError(
                "execution_unknown", "Unreconciled pending Git operation"
            ) from err
    if state.pending_attempt is not None:
        raise TrialError(
            "execution_unknown", "Cannot resume with unconfirmed pending attempt"
        )
    return state


def _handle_additional_request(
    request_text: str, state: TrialState, deps: TrialDependencies
) -> tuple[TrialState, TrialView]:
    req_snap = RequestSnapshot(
        text=request_text,
        digest=request_digest(request_text),
        ci_argv_override=state.request.ci_argv_override,
    )
    budget = Budget(
        plan_revision=state.budget.plan_revision + 1,
        cycles_started=0,
        reviews_started=0,
    )
    state = replace(
        state,
        request=req_snap,
        budget=budget,
        approved_plan_digest=None,
        approved_worktree_digest=None,
        approved_index_digest=None,
        ci_evidence=None,
        review_evidence=None,
        phase=TrialPhase.PREPARING,
        checkpoint=Checkpoint.PLAN,
        revision=state.revision + 1,
    )
    deps.store.save(state, expected_revision=state.revision - 1)
    snap_before = deps.workspace.inspect(state.repository, state.workspace)
    view = _run_plan_stage(state, deps, state.request.ci_argv_override, (), snap_before)
    return state, view


def _handle_ci_override_update(
    override: tuple[str, ...], state: TrialState, deps: TrialDependencies
) -> tuple[TrialState, TrialView | None]:
    req_snap = replace(state.request, ci_argv_override=override)
    state = replace(state, request=req_snap, revision=state.revision + 1)
    deps.store.save(state, expected_revision=state.revision - 1)
    if state.plan is not None and state.plan.ci_argv is None:
        snap_before = deps.workspace.inspect(state.repository, state.workspace)
        view = _run_plan_stage(state, deps, override, (), snap_before)
        return state, view
    return state, None


def _handle_request_updates(
    request: ResumeRequest, state: TrialState, deps: TrialDependencies
) -> tuple[TrialState, TrialView | None]:
    if request.request_text is not None and request.request_text.strip():
        return _handle_additional_request(request.request_text, state, deps)
    if request.ci_argv_override is not None:
        return _handle_ci_override_update(request.ci_argv_override, state, deps)
    return state, None


def resume_trial(request: ResumeRequest, deps: TrialDependencies) -> TrialView:
    """Resume execution from previous checkpoint, handling approval, fix, CI, and review."""
    with deps.store.lock():
        state = deps.store.load(request.trial_id)
        stopped_state = _handle_confirmations(request, state, deps)
        if stopped_state is not None:
            snap = deps.workspace.inspect(
                stopped_state.repository, stopped_state.workspace
            )
            return _make_view(stopped_state, deps, snap, ())

        state = _reconcile_pending_operations(state, deps)
        state, plan_view = _handle_request_updates(request, state, deps)
        if plan_view is not None:
            return plan_view

        if request.approve_plan is not None:
            state = _apply_approval(request, state, deps)

        while state.phase not in (
            TrialPhase.READY,
            TrialPhase.CLOSED,
            TrialPhase.NEEDS_ATTENTION,
        ):
            prev_rev = state.revision
            if state.checkpoint == Checkpoint.IMPLEMENT:
                state = _execute_implement(state, deps)
            elif state.checkpoint == Checkpoint.CI:
                state = _execute_ci(state, deps)
            elif state.checkpoint == Checkpoint.REVIEW:
                state = _execute_review(state, deps)
            if state.revision == prev_rev:
                break

        snap = deps.workspace.inspect(state.repository, state.workspace)
        return _make_view(state, deps, snap, ())


def inspect_trial(trial_id: str | None, deps: TrialDependencies) -> TrialView:
    """Read-only inspection returning current TrialView without acquiring lock."""
    tid = trial_id if trial_id is not None else deps.store.active_trial_id()
    if tid is None:
        return TrialView(
            state=None,
            effective_phase=TrialPhase.PREPARING,
            reason="not_found",
            diff="",
            attempts=(),
            next_actions=(),
            worktree_digest=None,
            ci_candidates=(),
            ready_is_current=False,
        )
    state = deps.store.load(tid)
    repo = deps.workspace.discover(state.repository.source_root)
    snap = deps.workspace.inspect(repo, state.workspace)
    ready, reason = assess_ready(
        state, repo.base_sha, snap, deps.workspace.includes_head(state)
    )
    effective_phase = state.phase
    if (
        state.pending_attempt is not None or state.pending_git is not None
    ) and not deps.controller_alive(state.controller.pid if state.controller else None):
        effective_phase = TrialPhase.NEEDS_ATTENTION
        reason = "execution_unknown"

    attempts = tuple(
        att
        for aid in state.attempt_ids
        if (att := deps.store.load_attempt(state.trial_id, aid)) is not None
    )
    return TrialView(
        state=state,
        effective_phase=effective_phase,
        reason=reason if not ready else state.reason,
        diff=deps.workspace.diff(state),
        attempts=attempts,
        next_actions=(),
        worktree_digest=workspace_digest(snap),
        ci_candidates=(),
        ready_is_current=ready,
    )


def record_decision(
    trial_id: str,
    value: Literal["keep", "adopted"],
    deps: TrialDependencies,
) -> TrialView:
    """Record user decision and close active trial."""
    with deps.store.lock():
        state = deps.store.load(trial_id)
        if state.pending_attempt is not None or state.pending_git is not None:
            raise TrialError(
                "execution_unknown", "Cannot record decision with pending operations"
            )
        if value == "adopted":
            repo = deps.workspace.discover(state.repository.source_root)
            snap = deps.workspace.inspect(repo, state.workspace)
            ready, reason = assess_ready(
                state,
                state.repository.base_sha,
                snap,
                deps.workspace.includes_head(state),
            )
            if not ready:
                raise TrialError(
                    "state_invalid", f"Trial not ready for adoption: {reason}"
                )
            if not deps.workspace.includes_head(state):
                raise TrialError(
                    "state_invalid", "Trial head is not included in base branch"
                )
        now = deps.clock()
        dec = Decision(value=value, recorded_at=now)
        state = replace(
            state,
            decision=dec,
            phase=TrialPhase.CLOSED,
            checkpoint=Checkpoint.CLOSED,
            revision=state.revision + 1,
            observation=replace(state.observation, updated_at=now),
        )
        deps.store.save(state, expected_revision=state.revision - 1)
        deps.store.close(state)
        deps.workspace.ensure_storage_ignored(state.repository)
        snap = deps.workspace.inspect(state.repository, state.workspace)
        return _make_view(state, deps, snap, ())
