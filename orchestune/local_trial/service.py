"""High-level orchestration service for local trial execution (#1734, #1737)."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

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
    GitIntent,
    ImplementationPayload,
    Observation,
    PlanPayload,
    ProcessRecord,
    ProcessStatus,
    RepositoryIdentity,
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
from orchestune.local_trial.policy import (
    assess_ready,
    classify_ci_failure,
    determine_plan_reason,
    is_workspace_dirty_unapproved,
    make_evidence,
    resolve_resume_needs_attention,
    validate_attempt,
    validate_persisted_attempt,
    validate_plan_approval,
    verify_clean_plan,
    verify_clean_review,
)
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
    runner = get_default_runner()
    return TrialDependencies(
        store=LocalTrialStore(common_dir=common_dir, primary_root=primary_root),
        workspace=ws,
        agents=LocalAgentRunner(runner=runner),
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
    if not detected:
        raise TrialError("agent_unavailable", "No trial agent available")
    if len(detected) > 1:
        raise TrialError(
            "invalid_input", "Multiple agents available; please specify agent_name"
        )
    return detected[0]


def _save_state(state: TrialState, deps: TrialDependencies, **kw: Any) -> TrialState:
    rev = kw.pop("revision", state.revision + 1)
    new_state = replace(state, revision=rev, **kw)
    deps.store.save(new_state, expected_revision=new_state.revision - 1)
    return new_state


def _exhaust_budget(state: TrialState, deps: TrialDependencies) -> TrialState:
    return _save_state(
        state, deps, phase=TrialPhase.NEEDS_ATTENTION, reason="budget_exhausted"
    )


def _load_attempts(
    state: TrialState, deps: TrialDependencies
) -> tuple[AttemptResult, ...]:
    return tuple(
        att
        for aid in state.attempt_ids
        if (att := deps.store.load_attempt(state.trial_id, aid)) is not None
    )


_EMPTY_VIEW = TrialView(
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
_RESETS: dict[str, Any] = {
    "approved_plan_digest": None,
    "approved_worktree_digest": None,
    "approved_index_digest": None,
    "ci_evidence": None,
    "review_evidence": None,
    "findings": (),
    "responses": (),
}
_COMMIT_RESETS: dict[str, Any] = {
    "pending_git": None,
    "checkpoint": Checkpoint.CI,
    "phase": TrialPhase.IMPLEMENTING,
    "ci_evidence": None,
    "review_evidence": None,
    "approved_worktree_digest": None,
    "approved_index_digest": None,
}


_EMPTY_STATE_FIELDS: dict[str, Any] = {
    **{
        k: None
        for k in "plan approved_plan_digest approved_worktree_digest approved_index_digest reason pending_attempt pending_git ci_evidence review_evidence decision".split()
    },
    **{k: () for k in ("attempt_ids", "findings", "responses", "stop_confirmations")},
}


def _make_view(
    state: TrialState | None,
    deps: TrialDependencies,
    snapshot: WorkspaceSnapshot | None,
    ci_candidates: tuple[tuple[str, ...], ...] = (),
    effective_phase: TrialPhase | None = None,
    reason: str | None = None,
) -> TrialView:
    if state is None or snapshot is None:
        return _EMPTY_VIEW
    ready, ar = assess_ready(state, snapshot, deps.workspace.includes_head(state))
    vr = (
        reason
        if reason is not None
        else (ar if state.phase == TrialPhase.READY and not ready else state.reason)
    )
    return TrialView(
        state=state,
        effective_phase=effective_phase or state.phase,
        reason=vr,
        diff=deps.workspace.diff(state),
        attempts=_load_attempts(state, deps),
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
    repo_ident, now = deps.workspace.discover(request.repo), deps.clock()
    ctrl = ControllerInfo(pid=os.getpid(), started_at=now, token=deps.new_id())
    obs = Observation(started_at=now, updated_at=now, elapsed_seconds=0.0)
    req_snap = RequestSnapshot(
        text=request.request_text,
        digest=request_digest(request.request_text),
        ci_argv_override=preferred_ci,
    )
    state = TrialState(
        **_EMPTY_STATE_FIELDS,
        schema_version=SCHEMA_VERSION,
        trial_id=trial_id,
        repository=repo_ident,
        workspace=deps.workspace.paths(repo_ident, trial_id),
        request=req_snap,
        agent=agent,
        phase=TrialPhase.PREPARING,
        checkpoint=Checkpoint.PREPARE,
        revision=0,
        budget=Budget(plan_revision=0, cycles_started=0, reviews_started=0),
        head_sha=repo_ident.base_sha,
        controller=ctrl,
        observation=obs,
    )
    return state, ()


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
    return _save_state(
        state,
        deps,
        plan=plan,
        pending_attempt=None,
        attempt_ids=tuple(dict.fromkeys((*state.attempt_ids, attempt_id))),
        phase=TrialPhase.PLANNED,
        checkpoint=Checkpoint.AWAIT_APPROVAL,
        reason=determine_plan_reason(plan),
        observation=replace(state.observation, updated_at=deps.clock()),
    )


def _handle_plan_error(
    state: TrialState,
    err: TrialError,
    deps: TrialDependencies,
) -> TrialState:
    pending = state.pending_attempt if err.code == "interrupted" else None
    return _save_state(
        state,
        deps,
        pending_attempt=pending,
        phase=TrialPhase.NEEDS_ATTENTION,
        reason=err.code,
        observation=replace(state.observation, updated_at=deps.clock()),
    )


def _run_plan_stage(
    state: TrialState,
    deps: TrialDependencies,
    preferred_ci: tuple[str, ...] | None,
    candidates: tuple[tuple[str, ...], ...],
    snap_before: WorkspaceSnapshot,
) -> TrialView:
    plan_inp = _create_attempt_input(state, deps, AttemptKind.PLAN)
    state = _save_state(
        state,
        deps,
        pending_attempt=plan_inp,
        observation=replace(state.observation, updated_at=deps.clock()),
    )

    try:
        req_candidates = (
            (preferred_ci,)
            if preferred_ci is not None and not candidates
            else candidates
        )
        req = build_agent_request(
            state, plan_inp, diff="", previous_results=(), ci_candidates=req_candidates
        )
        res = deps.agents.run(req, state.agent)
        deps.store.record_attempt(res)
        snap_curr = deps.workspace.inspect(state.repository, state.workspace)
        verify_clean_plan(snap_before, snap_curr)
        validate_attempt(res, state, AttemptKind.PLAN, state.head_sha, None)
        state = _handle_plan_success(
            state, res, deps, preferred_ci, plan_inp.attempt_id
        )
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

        obs = replace(state.observation, updated_at=deps.clock())
        intent = GitIntent.simple("prepare", state.repository.base_sha)
        state = _save_state(state, deps, pending_git=intent, observation=obs)
        deps.workspace.prepare(state.repository, state.workspace)
        snap_before = deps.workspace.inspect(state.repository, state.workspace)
        state = _save_state(
            state,
            deps,
            pending_git=None,
            checkpoint=Checkpoint.PLAN,
            head_sha=snap_before.head_sha,
            observation=obs,
        )
        return _run_plan_stage(state, deps, preferred_ci, candidates, snap_before)


def _record_interrupted_attempt(
    state: TrialState, deps: TrialDependencies, inp: AttemptInput
) -> None:
    proc = ProcessRecord(
        outcome=ProcessStatus.INTERRUPTED,
        returncode=None,
        stop_confirmed=False,
        elapsed_seconds=0.0,
        timeout_seconds=0.0,
        stdout_tail="",
        stderr_tail="",
        detail="stopped confirmed via confirmation",
    )
    deps.store.record_attempt(
        AttemptResult(
            schema_version=SCHEMA_VERSION,
            input=inp,
            process=proc,
            agent_payload=None,
            ended_at=deps.clock(),
        )
    )


def _handle_confirmations(
    request: ResumeRequest, state: TrialState, deps: TrialDependencies
) -> TrialState | None:
    if request.confirm_stopped is not None:
        pa = state.pending_attempt
        if pa is None or pa.attempt_id != request.confirm_stopped:
            raise TrialError(
                "state_invalid",
                f"confirm_stopped ID {request.confirm_stopped} does not match pending attempt",
            )
        sc = StopConfirmation(
            attempt_id=request.confirm_stopped, recorded_at=deps.clock()
        )
        if deps.store.load_attempt(state.trial_id, request.confirm_stopped) is None:
            _record_interrupted_attempt(state, deps, pa)
        return _save_state(
            state,
            deps,
            pending_attempt=None,
            phase=TrialPhase.NEEDS_ATTENTION,
            reason="stopped_confirmed",
            stop_confirmations=(*state.stop_confirmations, sc),
            observation=replace(state.observation, updated_at=deps.clock()),
        )
    if request.confirm_worktree is not None:
        snap = deps.workspace.inspect(state.repository, state.workspace)
        if workspace_digest(snap) != request.confirm_worktree:
            raise TrialError("state_invalid", "confirm_worktree digest mismatch")
        return _save_state(
            state,
            deps,
            approved_worktree_digest=request.confirm_worktree,
            approved_index_digest=snap.index_digest,
            observation=replace(state.observation, updated_at=deps.clock()),
        )
    return None


def _verify_workspace_state(
    state: TrialState, deps: TrialDependencies, *, check_dirty: bool = True
) -> tuple[RepositoryIdentity, WorkspaceSnapshot]:
    repo_curr = deps.workspace.discover(state.repository.source_root)
    if repo_curr.base_sha != state.repository.base_sha:
        raise TrialError("base_changed", "Repository base SHA changed")
    snap = deps.workspace.inspect(state.repository, state.workspace)
    exp_b = state.workspace.branch.removeprefix("refs/heads/")
    mismatch = (
        repo_curr.source_root != state.repository.source_root
        or repo_curr.primary_root != state.repository.primary_root
        or (snap.branch is not None and snap.branch != exp_b)
        or snap.head_sha.lower() != state.head_sha.lower()
    )
    if mismatch:
        raise TrialError(
            "identity_mismatch", "Repository or workspace identity mismatch"
        )
    if check_dirty and is_workspace_dirty_unapproved(state, snap):
        raise TrialError("worktree_dirty", "Implementation worktree is dirty")
    return repo_curr, snap


def _apply_approval(
    request: ResumeRequest, state: TrialState, deps: TrialDependencies
) -> TrialState:
    validate_plan_approval(state, request.approve_plan)
    _, snap = _verify_workspace_state(state, deps)
    chk = (
        Checkpoint.CI
        if state.budget.cycles_started > 0
        and state.head_sha.lower() != state.repository.base_sha.lower()
        else Checkpoint.IMPLEMENT
    )
    return _save_state(
        state,
        deps,
        approved_plan_digest=request.approve_plan,
        approved_worktree_digest=workspace_digest(snap),
        approved_index_digest=snap.index_digest,
        checkpoint=chk,
        phase=TrialPhase.IMPLEMENTING,
        reason=None,
        observation=replace(state.observation, updated_at=deps.clock()),
    )


def _commit_implementation(
    state: TrialState, deps: TrialDependencies, **state_updates: Any
) -> TrialState:
    snap = deps.workspace.inspect(state.repository, state.workspace)
    if not snap.changes:
        return _save_state(
            state,
            deps,
            phase=TrialPhase.NEEDS_ATTENTION,
            reason="no_changes",
            **state_updates,
        )
    intent = deps.workspace.stage_commit(state)
    state = _save_state(state, deps, pending_git=intent, **state_updates)
    head = deps.workspace.finish_commit(state, intent)
    return _save_state(state, deps, head_sha=head, **_COMMIT_RESETS)


def _create_attempt_input(
    state: TrialState, deps: TrialDependencies, kind: AttemptKind
) -> AttemptInput:
    att_id, ws = deps.new_id(), state.workspace
    cwd = ws.review_path if kind == AttemptKind.REVIEW else ws.implementation_path
    return AttemptInput(
        trial_id=state.trial_id,
        attempt_id=att_id,
        kind=kind,
        input_head_sha=state.head_sha,
        base_sha=state.repository.base_sha,
        plan_digest=state.approved_plan_digest,
        plan_revision=state.budget.plan_revision,
        cwd=cwd,
        artifact_dir=f"{cwd}/.orchestune/artifacts/{att_id}",
        started_at=deps.clock(),
    )


def _finish_implement_attempt(
    state: TrialState,
    res: AttemptResult,
    deps: TrialDependencies,
    attempt_id: str,
) -> TrialState:
    aids = tuple(dict.fromkeys((*state.attempt_ids, attempt_id)))
    try:
        validate_attempt(
            res, state, res.input.kind, state.head_sha, state.approved_plan_digest
        )
    except TrialError as err:
        if err.code == "agent_failed":
            return _save_state(
                state,
                deps,
                pending_attempt=None,
                attempt_ids=aids,
                phase=TrialPhase.NEEDS_ATTENTION,
                reason="agent_failed",
            )
        raise
    responses = state.responses
    payload = res.agent_payload.payload if res.agent_payload else None
    if isinstance(payload, ImplementationPayload):
        new_fids = {r.finding_id for r in payload.responses}
        responses = (
            *(r for r in responses if r.finding_id not in new_fids),
            *payload.responses,
        )
    return _commit_implementation(
        state, deps, pending_attempt=None, attempt_ids=aids, responses=responses
    )


def _execute_implement(state: TrialState, deps: TrialDependencies) -> TrialState:
    if state.budget.cycles_started >= 3:
        return _exhaust_budget(state, deps)

    kind = (
        AttemptKind.IMPLEMENT if state.budget.cycles_started == 0 else AttemptKind.FIX
    )
    budget = replace(state.budget, cycles_started=state.budget.cycles_started + 1)
    att_inp = _create_attempt_input(state, deps, kind)
    state = _save_state(state, deps, budget=budget, pending_attempt=att_inp)

    req = build_agent_request(
        state,
        att_inp,
        diff=deps.workspace.diff(state),
        previous_results=_load_attempts(state, deps),
    )
    res = deps.agents.run(req, state.agent)
    deps.store.record_attempt(res)
    return _finish_implement_attempt(state, res, deps, att_inp.attempt_id)


def _run_ci_process(spec: ManagedProcessSpec, deps: TrialDependencies) -> ProcessRecord:
    try:
        r = deps.process_runner.run(spec)
        out = (
            ProcessStatus(r.outcome.value)
            if r.outcome.value in ProcessStatus._value2member_map_
            else ProcessStatus.NONZERO_EXIT
        )
        return ProcessRecord(
            outcome=out,
            returncode=r.returncode,
            stop_confirmed=r.stop_confirmed,
            elapsed_seconds=r.elapsed_seconds,
            timeout_seconds=r.timeout_seconds,
            stdout_tail=r.stdout_tail,
            stderr_tail=r.stderr_tail,
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


def _handle_ci_result(
    state: TrialState,
    ci_id: str,
    proc_rec: ProcessRecord,
    deps: TrialDependencies,
) -> TrialState:
    aids = tuple(dict.fromkeys((*state.attempt_ids, ci_id)))
    ok = (
        proc_rec.outcome == ProcessStatus.SUCCESS
        and proc_rec.returncode == 0
        and proc_rec.stop_confirmed
    )
    if ok:
        ci_argv = state.plan.ci_argv if state.plan and state.plan.ci_argv else ()
        ev = make_evidence(ci_id, state, ci_argv, state.approved_plan_digest or "")
        return _save_state(
            state,
            deps,
            pending_attempt=None,
            attempt_ids=aids,
            ci_evidence=ev,
            checkpoint=Checkpoint.REVIEW,
        )

    phase, reason, checkpoint = classify_ci_failure(state, proc_rec)
    pending_att = state.pending_attempt if proc_rec.stop_confirmed is False else None
    return _save_state(
        state,
        deps,
        pending_attempt=pending_att,
        attempt_ids=aids,
        phase=phase,
        reason=reason,
        checkpoint=checkpoint,
    )


def _execute_ci(state: TrialState, deps: TrialDependencies) -> TrialState:
    ci_argv = state.plan.ci_argv if state.plan else None
    if state.plan is None or ci_argv is None or state.approved_plan_digest is None:
        raise TrialError("ci_missing", "Plan has no CI command")
    if state.budget.reviews_started >= 3:
        return _exhaust_budget(state, deps)
    ci_inp = _create_attempt_input(state, deps, AttemptKind.CI)
    ci_id = ci_inp.attempt_id
    state = _save_state(state, deps, pending_attempt=ci_inp)

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

    return _handle_ci_result(state, ci_id, proc_rec, deps)


def _prepare_review_workspace(state: TrialState, deps: TrialDependencies) -> TrialState:
    intent = GitIntent.simple("prepare_review", state.head_sha)
    state = _save_state(state, deps, pending_git=intent)
    deps.workspace.prepare_review(state)
    return _save_state(state, deps, pending_git=None)


def _handle_review_verdict(
    state: TrialState,
    rev_id: str,
    inner_rev: ReviewPayload,
    deps: TrialDependencies,
) -> TrialState:
    aids = tuple(dict.fromkeys((*state.attempt_ids, rev_id)))
    passed = inner_rev.verdict == ReviewVerdict.PASS
    ci_argv = state.plan.ci_argv if state.plan and state.plan.ci_argv else ()
    plan_dig = state.approved_plan_digest or ""
    ev = make_evidence(rev_id, state, ci_argv, plan_dig) if passed else None
    return _save_state(
        state,
        deps,
        pending_attempt=None,
        attempt_ids=aids,
        review_evidence=ev,
        ci_evidence=state.ci_evidence if passed else None,
        findings=() if passed else inner_rev.findings,
        phase=TrialPhase.READY if passed else TrialPhase.FIXING,
        checkpoint=Checkpoint.DECISION if passed else Checkpoint.IMPLEMENT,
        reason=None if passed else state.reason,
    )


def _finish_review_attempt(
    state: TrialState,
    res: AttemptResult,
    deps: TrialDependencies,
    attempt_id: str,
) -> TrialState:
    verify_clean_review(deps.workspace.inspect_review(state), state.head_sha)
    aids = tuple(dict.fromkeys((*state.attempt_ids, attempt_id)))
    try:
        validate_attempt(
            res, state, AttemptKind.REVIEW, state.head_sha, state.approved_plan_digest
        )
    except TrialError as err:
        if err.code == "agent_failed":
            return _save_state(
                state,
                deps,
                pending_attempt=None,
                attempt_ids=aids,
                phase=TrialPhase.NEEDS_ATTENTION,
                reason="agent_failed",
            )
        raise
    payload = res.agent_payload.payload if res.agent_payload else None
    if not isinstance(payload, ReviewPayload):
        raise TrialError("artifact_invalid", "Expected ReviewPayload")
    return _handle_review_verdict(state, attempt_id, payload, deps)


def _execute_review(state: TrialState, deps: TrialDependencies) -> TrialState:
    if (
        state.plan is None
        or state.plan.ci_argv is None
        or state.approved_plan_digest is None
    ):
        raise TrialError("state_invalid", "Review requires approved plan and CI")
    if state.budget.reviews_started >= 3:
        return _exhaust_budget(state, deps)

    state = _prepare_review_workspace(state, deps)
    rev_inp = _create_attempt_input(state, deps, AttemptKind.REVIEW)
    budget = replace(state.budget, reviews_started=state.budget.reviews_started + 1)
    state = _save_state(state, deps, budget=budget, pending_attempt=rev_inp)

    rev_req = build_agent_request(
        state,
        rev_inp,
        diff=deps.workspace.diff(state),
        previous_results=_load_attempts(state, deps),
    )
    rev_res = deps.agents.run(rev_req, state.agent)
    deps.store.record_attempt(rev_res)
    return _finish_review_attempt(state, rev_res, deps, rev_inp.attempt_id)


def _reconcile_pending_git(state: TrialState, deps: TrialDependencies) -> TrialState:
    intent = state.pending_git
    if intent is None:
        return state
    op = intent.operation
    if op == "prepare":
        deps.workspace.prepare(state.repository, state.workspace)
        snap = deps.workspace.inspect(state.repository, state.workspace)
        return _save_state(
            state,
            deps,
            pending_git=None,
            checkpoint=Checkpoint.PLAN,
            head_sha=snap.head_sha,
        )
    if op == "prepare_review":
        deps.workspace.prepare_review(state)
        return _save_state(state, deps, pending_git=None)
    if op == "commit":
        try:
            head = deps.workspace.reconcile_commit(state, intent)
            if head is None:
                raise TrialError(
                    "execution_unknown", "Unreconciled pending Git commit operation"
                )
            return _save_state(
                state, deps, pending_git=None, head_sha=head, checkpoint=Checkpoint.CI
            )
        except (TrialError, OSError) as err:
            raise TrialError(
                "execution_unknown", "Unreconciled pending Git operation"
            ) from err
    raise TrialError("execution_unknown", f"Unknown pending git operation: {op}")


def _reconcile_attempt_by_kind(
    state: TrialState,
    res: AttemptResult,
    snap: WorkspaceSnapshot,
    deps: TrialDependencies,
) -> TrialState:
    kind, aid = res.input.kind, res.input.attempt_id
    if kind == AttemptKind.PLAN:
        if snap.changes:
            raise TrialError(
                "worktree_dirty", "Worktree modified during planning phase"
            )
        try:
            validate_attempt(res, state, AttemptKind.PLAN, state.head_sha, None)
        except TrialError as err:
            if err.code == "agent_failed":
                return _handle_plan_error(state, err, deps)
            raise
        return _handle_plan_success(
            state, res, deps, state.request.ci_argv_override, aid
        )
    if kind in (AttemptKind.IMPLEMENT, AttemptKind.FIX):
        return _finish_implement_attempt(state, res, deps, aid)
    if kind == AttemptKind.CI:
        if snap.changes:
            raise TrialError("worktree_dirty", "Worktree modified during CI")
        try:
            validate_attempt(
                res, state, AttemptKind.CI, state.head_sha, state.approved_plan_digest
            )
        except TrialError as err:
            if err.code != "ci_failed":
                raise
        return _handle_ci_result(state, aid, res.process, deps)
    if kind == AttemptKind.REVIEW:
        return _finish_review_attempt(state, res, deps, aid)
    raise TrialError("artifact_invalid", f"Unsupported attempt kind: {kind}")


def _reconcile_pending_attempt(
    state: TrialState, deps: TrialDependencies
) -> TrialState:
    if state.pending_attempt is None:
        return state
    persisted = deps.store.load_attempt(
        state.trial_id, state.pending_attempt.attempt_id
    )
    res = validate_persisted_attempt(state, persisted, SCHEMA_VERSION)
    repo_curr = deps.workspace.discover(state.repository.source_root)
    if repo_curr.base_sha != state.repository.base_sha:
        raise TrialError("base_changed", "Repository base SHA changed")
    snap = deps.workspace.inspect(state.repository, state.workspace)
    if snap.head_sha.lower() != state.head_sha.lower():
        raise TrialError("head_changed", "Worktree head SHA does not match trial state")
    return _reconcile_attempt_by_kind(state, res, snap, deps)


def _reconcile_pending_operations(
    state: TrialState, deps: TrialDependencies
) -> TrialState:
    return _reconcile_pending_attempt(_reconcile_pending_git(state, deps), deps)


def _invalidate_approval_state(
    state: TrialState,
    req_snap: RequestSnapshot,
    budget: Budget,
    deps: TrialDependencies,
    *,
    plan: TrialPlan | None = None,
    phase: TrialPhase = TrialPhase.PLANNED,
    checkpoint: Checkpoint = Checkpoint.AWAIT_APPROVAL,
    reason: str | None = None,
) -> TrialState:
    return _save_state(
        state,
        deps,
        request=req_snap,
        plan=plan if plan is not None else state.plan,
        budget=budget,
        phase=phase,
        checkpoint=checkpoint,
        reason=reason,
        observation=replace(state.observation, updated_at=deps.clock()),
        **_RESETS,
    )


def _handle_additional_request(
    request_text: str, state: TrialState, deps: TrialDependencies
) -> tuple[TrialState, TrialView]:
    req_snap = RequestSnapshot(
        text=request_text,
        digest=request_digest(request_text),
        ci_argv_override=state.request.ci_argv_override,
    )
    b = Budget(
        plan_revision=state.budget.plan_revision + 1,
        cycles_started=0,
        reviews_started=0,
    )
    state = _invalidate_approval_state(
        state,
        req_snap,
        b,
        deps,
        phase=TrialPhase.PREPARING,
        checkpoint=Checkpoint.PLAN,
    )
    snap_before = deps.workspace.inspect(state.repository, state.workspace)
    return state, _run_plan_stage(
        state, deps, state.request.ci_argv_override, (), snap_before
    )


def _handle_ci_override_update(
    override: tuple[str, ...], state: TrialState, deps: TrialDependencies
) -> tuple[TrialState, TrialView | None]:
    if state.pending_attempt is not None or state.pending_git is not None:
        raise TrialError(
            "execution_unknown", "Cannot change CI command with pending operations"
        )
    if state.phase == TrialPhase.CLOSED:
        raise TrialError("state_invalid", "Cannot change CI command for closed trial")

    _, snap = _verify_workspace_state(state, deps)
    if state.plan is not None and state.plan.ci_argv == override:
        return state, _make_view(state, deps, snap, ())

    budget = replace(state.budget, plan_revision=state.budget.plan_revision + 1)
    req_snap = replace(state.request, ci_argv_override=override)

    if state.plan and state.plan.ci_argv:
        new_p = replace(state.plan, ci_argv=override)
        validate_plan(
            new_p,
            state.repository.primary_root,
            state.request.text,
            state.repository.base_sha,
            preferred_ci=override,
        )
        reason = determine_plan_reason(new_p)
        state = _invalidate_approval_state(
            state, req_snap, budget, deps, plan=new_p, reason=reason
        )
        return state, _make_view(state, deps, snap, ())

    state = _invalidate_approval_state(state, req_snap, budget, deps)
    return state, _run_plan_stage(state, deps, override, (), snap)


def _handle_request_updates(
    request: ResumeRequest, state: TrialState, deps: TrialDependencies
) -> tuple[TrialState, TrialView | None]:
    if request.request_text is not None and request.request_text.strip():
        return _handle_additional_request(request.request_text, state, deps)
    if request.ci_argv_override is not None:
        return _handle_ci_override_update(request.ci_argv_override, state, deps)
    return state, None


def _resume_from_needs_attention(
    state: TrialState, deps: TrialDependencies
) -> TrialState:
    res = resolve_resume_needs_attention(state)
    if res is None:
        return state
    phase, chk = res
    return _save_state(state, deps, phase=phase, checkpoint=chk, reason=None)


def _execute_checkpoint(
    state: TrialState, deps: TrialDependencies
) -> TrialState | TrialView:
    chk = state.checkpoint
    if chk == Checkpoint.PLAN:
        snap_before = deps.workspace.inspect(state.repository, state.workspace)
        return _run_plan_stage(
            state, deps, state.request.ci_argv_override, (), snap_before
        )
    if chk == Checkpoint.IMPLEMENT:
        return _execute_implement(state, deps)
    if chk == Checkpoint.CI:
        return _execute_ci(state, deps)
    return _execute_review(state, deps) if chk == Checkpoint.REVIEW else state


def _check_unapproved_dirty(
    state: TrialState, deps: TrialDependencies
) -> TrialView | None:
    snap = deps.workspace.inspect(state.repository, state.workspace)
    if is_workspace_dirty_unapproved(state, snap):
        return _make_view(
            state,
            deps,
            snap,
            effective_phase=TrialPhase.NEEDS_ATTENTION,
            reason="worktree_dirty",
        )
    return None


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

        repo_curr, snap = _verify_workspace_state(state, deps, check_dirty=False)

        if dirty_view := _check_unapproved_dirty(state, deps):
            return dirty_view

        if (
            snap.changes
            and state.approved_plan_digest is not None
            and state.checkpoint != Checkpoint.PLAN
            and state.approved_worktree_digest == workspace_digest(snap)
            and state.approved_index_digest == snap.index_digest
        ):
            state = _commit_implementation(state, deps)

        state = _resume_from_needs_attention(state, deps)

        while state.phase not in (
            TrialPhase.READY,
            TrialPhase.CLOSED,
            TrialPhase.NEEDS_ATTENTION,
        ):
            prev_rev = state.revision
            res = _execute_checkpoint(state, deps)
            if isinstance(res, TrialView):
                return res
            state = res
            if state.revision == prev_rev:
                break

        snap = deps.workspace.inspect(state.repository, state.workspace)
        return _make_view(state, deps, snap, ())


def inspect_trial(trial_id: str | None, deps: TrialDependencies) -> TrialView:
    """Read-only inspection returning current TrialView without acquiring lock."""
    tid = trial_id if trial_id is not None else deps.store.active_trial_id()
    if tid is None:
        return _make_view(None, deps, None)
    state = deps.store.load(tid)
    repo = deps.workspace.discover(state.repository.source_root)
    snap = deps.workspace.inspect(repo, state.workspace)
    eff_phase, eff_reason = state.phase, None
    if (
        state.pending_attempt is not None or state.pending_git is not None
    ) and not deps.controller_alive(state.controller.pid if state.controller else None):
        eff_phase, eff_reason = TrialPhase.NEEDS_ATTENTION, "execution_unknown"

    return _make_view(
        state,
        deps,
        snap,
        effective_phase=eff_phase,
        reason=eff_reason,
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
            inc = deps.workspace.includes_head(state)
            ready, reason = assess_ready(state, snap, inc)
            if not ready or not inc:
                err = (
                    f"Trial not ready for adoption: {reason}"
                    if not ready
                    else "Trial head is not included in base branch"
                )
                raise TrialError("state_invalid", err)
        now = deps.clock()
        state = _save_state(
            state,
            deps,
            decision=Decision(value=value, recorded_at=now),
            phase=TrialPhase.CLOSED,
            checkpoint=Checkpoint.CLOSED,
            observation=replace(state.observation, updated_at=now),
        )
        deps.store.close(state)
        deps.workspace.ensure_storage_ignored(state.repository)
        snap = deps.workspace.inspect(state.repository, state.workspace)
        return _make_view(state, deps, snap)
