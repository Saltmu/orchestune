"""Unit tests for local trial planning and service execution workflows (#1737)."""

from __future__ import annotations

import contextlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from orchestune.infra.managed_process import (
    ManagedProcessResult,
    ManagedProcessSpec,
    ProcessOutcome,
)
from orchestune.local_trial.contracts import TrialError
from orchestune.local_trial.models import (
    AgentInfo,
    AgentPayload,
    AttemptInput,
    AttemptKind,
    AttemptResult,
    Checkpoint,
    DisciplineStatus,
    Finding,
    FindingSeverity,
    ImplementationPayload,
    PlanPayload,
    ProcessRecord,
    ProcessStatus,
    RepositoryIdentity,
    ReviewPayload,
    ReviewVerdict,
    TrialPhase,
    TrialPlan,
    TrialState,
    WorkspaceIdentity,
    WorkspaceSnapshot,
)
from orchestune.local_trial.planning import resolve_ci_selection, validate_plan
from orchestune.local_trial.service import (
    ResumeRequest,
    StartRequest,
    TrialDependencies,
    inspect_trial,
    record_decision,
    resume_trial,
    start_trial,
)


def test_resolve_ci_selection_override(tmp_path: Path) -> None:
    preferred, cands = resolve_ci_selection(
        str(tmp_path), override=("python", "-m", "unittest")
    )
    assert preferred == ("python", "-m", "unittest")
    assert cands == ()


def test_resolve_ci_selection_config(tmp_path: Path) -> None:
    (tmp_path / "orchestune.toml").write_text('ci-command = ["pytest", "-v"]\n')
    preferred, cands = resolve_ci_selection(str(tmp_path))
    assert preferred == ("pytest", "-v")
    assert cands == ()


def test_resolve_ci_selection_invalid_config(tmp_path: Path) -> None:
    (tmp_path / "orchestune.toml").write_text("ci-command = 12345\n")
    with pytest.raises(TrialError) as exc:
        resolve_ci_selection(str(tmp_path))
    assert exc.value.code == "config_invalid"


def test_validate_plan_valid(tmp_path: Path) -> None:
    (tmp_path / "CONTRIBUTING.md").write_text("rules")
    plan = TrialPlan(
        goal="Implement feature",
        acceptance_criteria=("ac1",),
        write_paths=("src/foo.py",),
        discipline_paths=("CONTRIBUTING.md",),
        discipline_notes=("note",),
        ci_argv=("pytest",),
        discipline_status=DisciplineStatus.COMPATIBLE,
    )
    digest = validate_plan(plan, tmp_path, "do it", "a" * 40, preferred_ci=("pytest",))
    assert len(digest) == 64


def test_validate_plan_forbidden_paths(tmp_path: Path) -> None:
    plan = TrialPlan(
        goal="Implement feature",
        acceptance_criteria=("ac1",),
        write_paths=(".git/config",),
        discipline_paths=(),
        discipline_notes=(),
        ci_argv=("pytest",),
        discipline_status=DisciplineStatus.COMPATIBLE,
    )
    with pytest.raises(TrialError) as exc:
        validate_plan(plan, tmp_path, "do it", "a" * 40)
    assert exc.value.code == "artifact_invalid"


def test_validate_plan_escape(tmp_path: Path) -> None:
    plan = TrialPlan(
        goal="Implement feature",
        acceptance_criteria=("ac1",),
        write_paths=("../escape.py",),
        discipline_paths=(),
        discipline_notes=(),
        ci_argv=("pytest",),
        discipline_status=DisciplineStatus.COMPATIBLE,
    )
    with pytest.raises(TrialError) as exc:
        validate_plan(plan, tmp_path, "do it", "a" * 40)
    assert exc.value.code == "artifact_invalid"


def test_validate_plan_dot_slash_prefix_rejected(tmp_path: Path) -> None:
    (tmp_path / "CONTRIBUTING.md").write_text("rules")
    for bad_path in ("./src/foo.py", ".\\src\\foo.py"):
        plan = TrialPlan(
            goal="Implement feature",
            acceptance_criteria=("ac1",),
            write_paths=(bad_path,),
            discipline_paths=(),
            discipline_notes=(),
            ci_argv=("pytest",),
            discipline_status=DisciplineStatus.COMPATIBLE,
        )
        with pytest.raises(TrialError) as exc:
            validate_plan(plan, tmp_path, "do it", "a" * 40)
        assert exc.value.code == "artifact_invalid"


def test_validate_plan_ci_mismatch(tmp_path: Path) -> None:
    plan = TrialPlan(
        goal="Implement feature",
        acceptance_criteria=("ac1",),
        write_paths=("foo.py",),
        discipline_paths=(),
        discipline_notes=(),
        ci_argv=("npm", "test"),
        discipline_status=DisciplineStatus.COMPATIBLE,
    )
    with pytest.raises(TrialError) as exc:
        validate_plan(plan, tmp_path, "do it", "a" * 40, preferred_ci=("pytest",))
    assert exc.value.code == "artifact_invalid"


class FakeStore:
    def __init__(self) -> None:
        self.state: TrialState | None = None
        self.attempts: dict[str, AttemptResult] = {}
        self.active_id: str | None = None

    @contextlib.contextmanager
    def lock(self):
        yield

    def active_trial_id(self) -> str | None:
        return self.active_id

    def reserve(self, state: TrialState) -> None:
        self.active_id = state.trial_id
        self.state = state

    def load(self, trial_id: str) -> TrialState:
        if self.state is None or self.state.trial_id != trial_id:
            raise TrialError("not_found", "trial not found")
        return self.state

    def save(self, state: TrialState, *, expected_revision: int | None) -> None:
        self.state = state

    def record_attempt(self, result: AttemptResult) -> None:
        self.attempts[result.input.attempt_id] = result

    def load_attempt(self, trial_id: str, attempt_id: str) -> AttemptResult | None:
        return self.attempts.get(attempt_id)

    def close(self, state: TrialState) -> None:
        if self.active_id == state.trial_id:
            self.active_id = None
        self.state = state


class FakeWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.base_sha = "a" * 40
        self.head_sha = "a" * 40
        self.changes: tuple[Any, ...] = ()
        self.is_included = False

    def storage_roots(self, repo: str) -> tuple[str, str]:
        return (str(self.root / ".orchestune"), str(self.root))

    def discover(self, repo: str) -> RepositoryIdentity:
        return RepositoryIdentity(
            source_root=str(self.root),
            primary_root=str(self.root),
            common_dir=str(self.root / ".git"),
            base_ref="main",
            base_sha=self.base_sha,
        )

    def paths(self, repo: RepositoryIdentity, trial_id: str) -> WorkspaceIdentity:
        return WorkspaceIdentity(
            implementation_path=str(self.root / "worktrees" / trial_id / "impl"),
            review_path=str(self.root / "worktrees" / trial_id / "rev"),
            branch=f"trial/{trial_id}",
        )

    def ensure_storage_ignored(self, repo: RepositoryIdentity) -> None:
        pass

    def prepare(self, repo: RepositoryIdentity, ws: WorkspaceIdentity) -> None:
        pass

    def inspect(
        self, repo: RepositoryIdentity, ws: WorkspaceIdentity
    ) -> WorkspaceSnapshot:
        return WorkspaceSnapshot(
            head_sha=self.head_sha,
            base_tip_sha=self.base_sha,
            index_digest="i" * 64,
            content_digest="c" * 64,
            branch=None,
            changes=self.changes,
        )

    def stage_commit(self, state: TrialState):
        from orchestune.local_trial.models import GitIntent

        return GitIntent(
            operation="commit",
            pre_head_sha=self.head_sha,
            expected_tree_sha=None,
            target_sha="c" * 40,
            allowed_paths=("foo.py",),
        )

    def finish_commit(self, state: TrialState, intent) -> str:
        self.head_sha = intent.target_sha
        self.changes = ()
        return self.head_sha

    def reconcile_commit(self, state: TrialState, intent) -> str | None:
        return self.head_sha

    def prepare_review(self, state: TrialState) -> None:
        pass

    def inspect_review(self, state: TrialState) -> WorkspaceSnapshot:
        return self.inspect(state.repository, state.workspace)

    def diff(self, state: TrialState) -> str:
        return "diff"

    def working_diff(self, state: TrialState) -> str:
        return "working_diff" if self.changes else ""

    def includes_head(self, state: TrialState) -> bool:
        return self.is_included


class FakeAgentRunner:
    def __init__(self, agent: AgentInfo, ws: FakeWorkspace | None = None) -> None:
        self.agent = agent
        self.ws = ws
        self.call_history: list[str] = []
        self.plan_discipline_status: DisciplineStatus = DisciplineStatus.COMPATIBLE
        self.plan_ci_argv: tuple[str, ...] | None = ("pytest",)
        self.review_verdict: ReviewVerdict = ReviewVerdict.PASS
        self.review_findings: tuple[Finding, ...] = ()
        self.on_run: Any = None

    def detect(self) -> tuple[AgentInfo, ...]:
        return (self.agent,)

    def run(self, req: Any, agent: AgentInfo) -> AttemptResult:
        if self.on_run is not None:
            self.on_run(req)
        from orchestune.local_trial.models import Change

        self.call_history.append(str(req.input.kind))
        if self.ws is not None and req.input.kind in (
            AttemptKind.IMPLEMENT,
            AttemptKind.FIX,
        ):
            self.ws.changes = (Change(status="M", path="foo.py"),)
        proc = ProcessRecord(
            outcome=ProcessStatus.SUCCESS,
            returncode=0,
            stop_confirmed=True,
            elapsed_seconds=1.0,
            timeout_seconds=60.0,
            stdout_tail="",
            stderr_tail="",
            detail="",
        )
        if req.input.kind == AttemptKind.PLAN:
            plan = TrialPlan(
                goal="test",
                acceptance_criteria=("ac1",),
                write_paths=("foo.py",),
                discipline_paths=(),
                discipline_notes=(),
                ci_argv=self.plan_ci_argv,
                discipline_status=self.plan_discipline_status,
            )
            payload = PlanPayload(plan=plan)
            agent_payload = AgentPayload(
                schema_version=1,
                trial_id=req.input.trial_id,
                attempt_id=req.input.attempt_id,
                kind=AttemptKind.PLAN,
                input_head_sha=req.input.input_head_sha,
                plan_digest=req.input.plan_digest,
                payload=payload,
            )
            return AttemptResult(
                schema_version=1,
                input=req.input,
                process=proc,
                agent_payload=agent_payload,
                ended_at="2026-01-01T00:00:00Z",
            )
        elif req.input.kind in (AttemptKind.IMPLEMENT, AttemptKind.FIX):
            impl_payload = ImplementationPayload(
                summary="impl",
                responses=(),
            )
            agent_payload = AgentPayload(
                schema_version=1,
                trial_id=req.input.trial_id,
                attempt_id=req.input.attempt_id,
                kind=req.input.kind,
                input_head_sha=req.input.input_head_sha,
                plan_digest=req.input.plan_digest,
                payload=impl_payload,
            )
            return AttemptResult(
                schema_version=1,
                input=req.input,
                process=proc,
                agent_payload=agent_payload,
                ended_at="2026-01-01T00:00:00Z",
            )
        elif req.input.kind == AttemptKind.REVIEW:
            verdict = getattr(self, "review_verdict", ReviewVerdict.PASS)
            findings = getattr(self, "review_findings", ())
            rev_payload = ReviewPayload(
                verdict=verdict,
                findings=findings,
            )
            agent_payload = AgentPayload(
                schema_version=1,
                trial_id=req.input.trial_id,
                attempt_id=req.input.attempt_id,
                kind=AttemptKind.REVIEW,
                input_head_sha=req.input.input_head_sha,
                plan_digest=req.input.plan_digest,
                payload=rev_payload,
            )
            return AttemptResult(
                schema_version=1,
                input=req.input,
                process=proc,
                agent_payload=agent_payload,
                ended_at="2026-01-01T00:00:00Z",
            )
        raise ValueError(f"Unhandled kind {req.input.kind}")


class FakeProcessRunner:
    def __init__(self) -> None:
        self.call_history: list[list[str]] = []
        self.returncode = 0
        self.stop_confirmed = True

    def run(self, spec: ManagedProcessSpec) -> ManagedProcessResult:
        self.call_history.append(list(spec.args))
        outcome = (
            ProcessOutcome.SUCCESS
            if self.returncode == 0
            else ProcessOutcome.NONZERO_EXIT
        )
        return ManagedProcessResult(
            outcome=outcome,
            stage=spec.stage,
            returncode=self.returncode,
            stdout_tail="CI output\n",
            stderr_tail="",
            stop_confirmed=self.stop_confirmed,
            elapsed_seconds=1.0,
            timeout_seconds=spec.timeout_seconds,
        )


def _make_fake_deps(
    tmp_path: Path,
) -> tuple[
    TrialDependencies, FakeStore, FakeWorkspace, FakeAgentRunner, FakeProcessRunner
]:
    store = FakeStore()
    ws = FakeWorkspace(tmp_path)
    agent = AgentInfo(name="codex", executable="/bin/codex", version="1.0.0")
    agents = FakeAgentRunner(agent, ws=ws)
    proc_runner = FakeProcessRunner()
    deps = TrialDependencies(
        store=store,
        workspace=ws,
        agents=agents,
        process_runner=proc_runner,
        clock=lambda: "2026-01-01T00:00:00Z",
        new_id=lambda: "1" * 32,
        controller_alive=lambda pid: True,
    )
    return deps, store, ws, agents, proc_runner


def test_start_trial_happy_path(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(
        repo=str(tmp_path), request_text="implement something", agent_name="codex"
    )
    view = start_trial(req, deps)

    assert view.state is not None
    assert view.effective_phase == TrialPhase.PLANNED
    assert view.state.checkpoint == Checkpoint.AWAIT_APPROVAL
    assert view.reason == "approval_required"
    assert agents.call_history == ["plan"]


def test_start_trial_discipline_incompatible(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    agents.plan_discipline_status = DisciplineStatus.INCOMPATIBLE
    req = StartRequest(
        repo=str(tmp_path), request_text="implement something", agent_name="codex"
    )
    view = start_trial(req, deps)
    assert view.reason == "discipline_incompatible"
    assert view.effective_phase == TrialPhase.PLANNED


def test_start_trial_ci_missing(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    agents.plan_ci_argv = None
    req = StartRequest(
        repo=str(tmp_path), request_text="implement something", agent_name="codex"
    )
    view = start_trial(req, deps)
    assert view.reason == "ci_missing"
    assert view.effective_phase == TrialPhase.PLANNED


def test_resume_trial_happy_path_to_ready(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(
        repo=str(tmp_path), request_text="implement something", agent_name="codex"
    )
    view1 = start_trial(req, deps)
    assert view1.state is not None and view1.state.plan is not None
    trial_id = view1.state.trial_id

    plan_dig = validate_plan(
        view1.state.plan, tmp_path, "implement something", ws.base_sha
    )

    res_req = ResumeRequest(trial_id=trial_id, approve_plan=plan_dig)
    view2 = resume_trial(res_req, deps)

    assert view2.state is not None
    assert view2.effective_phase == TrialPhase.READY
    assert view2.state.checkpoint == Checkpoint.DECISION
    assert view2.ready_is_current is True
    assert view2.state.ci_evidence is not None
    assert view2.state.review_evidence is not None
    assert agents.call_history == ["plan", "implement", "review"]
    assert proc_runner.call_history == [["pytest"]]


def test_resume_trial_confirm_stopped(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(
        repo=str(tmp_path), request_text="implement something", agent_name="codex"
    )
    view1 = start_trial(req, deps)
    assert view1.state is not None
    trial_id = view1.state.trial_id

    res_req = ResumeRequest(trial_id=trial_id, confirm_stopped="some_attempt")
    view = resume_trial(res_req, deps)
    assert view.state is not None
    assert len(view.state.stop_confirmations) == 1
    assert view.state.stop_confirmations[0].attempt_id == "some_attempt"


def test_resume_trial_budget_exhaustion(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    proc_runner.returncode = 1  # CI fails every time
    req = StartRequest(
        repo=str(tmp_path), request_text="implement something", agent_name="codex"
    )
    view1 = start_trial(req, deps)
    assert view1.state is not None and view1.state.plan is not None
    trial_id = view1.state.trial_id

    plan_dig = validate_plan(
        view1.state.plan, tmp_path, "implement something", ws.base_sha
    )
    res_req = ResumeRequest(trial_id=trial_id, approve_plan=plan_dig)
    view = resume_trial(res_req, deps)
    assert view.state is not None
    assert view.effective_phase == TrialPhase.NEEDS_ATTENTION
    assert view.reason == "budget_exhausted"
    assert view.state.budget.cycles_started == 3

    # Ensure 10 further manual resumes do not launch additional runs
    initial_agent_calls = len(agents.call_history)
    for _ in range(10):
        resume_trial(ResumeRequest(trial_id=trial_id), deps)
    assert len(agents.call_history) == initial_agent_calls


def test_record_decision_keep_and_adopted(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(
        repo=str(tmp_path), request_text="implement something", agent_name="codex"
    )
    view1 = start_trial(req, deps)
    assert view1.state is not None and view1.state.plan is not None
    trial_id = view1.state.trial_id

    plan_dig = validate_plan(
        view1.state.plan, tmp_path, "implement something", ws.base_sha
    )
    resume_trial(ResumeRequest(trial_id=trial_id, approve_plan=plan_dig), deps)

    ws.is_included = True
    view_adopted = record_decision(trial_id, "adopted", deps)
    assert view_adopted.state is not None
    assert view_adopted.state.phase == TrialPhase.CLOSED
    assert view_adopted.state.decision is not None
    assert view_adopted.state.decision.value == "adopted"
    assert store.active_trial_id() is None


def test_inspect_trial_read_only(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(
        repo=str(tmp_path), request_text="implement something", agent_name="codex"
    )
    view = start_trial(req, deps)
    assert view.state is not None
    trial_id = view.state.trial_id

    inspected = inspect_trial(trial_id, deps)
    assert inspected.state is not None
    assert inspected.state.trial_id == trial_id
    assert inspected.effective_phase == TrialPhase.PLANNED


def test_record_decision_keep(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(
        repo=str(tmp_path), request_text="implement something", agent_name="codex"
    )
    view1 = start_trial(req, deps)
    assert view1.state is not None and view1.state.plan is not None
    trial_id = view1.state.trial_id

    plan_dig = validate_plan(
        view1.state.plan, tmp_path, "implement something", ws.base_sha
    )
    resume_trial(ResumeRequest(trial_id=trial_id, approve_plan=plan_dig), deps)

    view_keep = record_decision(trial_id, "keep", deps)
    assert view_keep.state is not None
    assert view_keep.state.phase == TrialPhase.CLOSED
    assert view_keep.state.decision is not None
    assert view_keep.state.decision.value == "keep"
    assert store.active_trial_id() is None


def test_inspect_trial_controller_dead(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(
        repo=str(tmp_path), request_text="implement something", agent_name="codex"
    )
    view = start_trial(req, deps)
    assert view.state is not None
    trial_id = view.state.trial_id

    # If state has pending attempt and controller is not alive:
    assert view.state.controller is not None
    dead_deps = replace(deps, controller_alive=lambda pid: False)
    # Manually put a pending attempt into state to simulate an interrupted run
    state = replace(
        view.state,
        pending_attempt=AttemptInput(
            trial_id=trial_id,
            attempt_id="att1",
            kind=AttemptKind.IMPLEMENT,
            input_head_sha="head",
            base_sha="base",
            plan_digest="plan",
            plan_revision=0,
            cwd="/cwd",
            artifact_dir="/art",
            started_at="2026-01-01T00:00:00Z",
        ),
    )
    store.save(state, expected_revision=None)

    inspected = inspect_trial(trial_id, dead_deps)
    assert inspected.effective_phase == TrialPhase.NEEDS_ATTENTION
    assert inspected.reason == "execution_unknown"


def test_resume_trial_confirm_worktree_mismatch(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(
        repo=str(tmp_path), request_text="implement something", agent_name="codex"
    )
    view = start_trial(req, deps)
    assert view.state is not None

    with pytest.raises(TrialError, match="confirm_worktree digest mismatch"):
        resume_trial(
            ResumeRequest(
                trial_id=view.state.trial_id, confirm_worktree="wrong_digest"
            ),
            deps,
        )


def test_start_trial_validation_errors(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    with pytest.raises(TrialError, match="request_text cannot be empty"):
        start_trial(StartRequest(repo=str(tmp_path), request_text="   "), deps)

    start_trial(
        StartRequest(repo=str(tmp_path), request_text="valid", agent_name="codex"),
        deps,
    )
    with pytest.raises(TrialError, match="Another trial is already active"):
        start_trial(
            StartRequest(repo=str(tmp_path), request_text="second", agent_name="codex"),
            deps,
        )


def test_resume_trial_review_rejection_then_pass(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    # 1st review fails, 2nd review passes
    agents.review_verdict = ReviewVerdict.CHANGES_REQUESTED
    agents.review_findings = (
        Finding(
            id="f1",
            severity=FindingSeverity.HIGH,
            path="foo.py",
            line=1,
            reason="Need fix",
            requested_change="bug found",
        ),
    )

    req = StartRequest(
        repo=str(tmp_path), request_text="implement something", agent_name="codex"
    )
    view1 = start_trial(req, deps)
    assert view1.state is not None and view1.state.plan is not None
    trial_id = view1.state.trial_id
    plan_dig = validate_plan(
        view1.state.plan, tmp_path, "implement something", ws.base_sha
    )

    # Make the 2nd review pass
    review_count = 0

    def on_run(req_arg: Any) -> None:
        nonlocal review_count
        if req_arg.input.kind == AttemptKind.REVIEW:
            review_count += 1
            if review_count > 1:
                agents.review_verdict = ReviewVerdict.PASS
                agents.review_findings = ()

    agents.on_run = on_run

    view2 = resume_trial(ResumeRequest(trial_id=trial_id, approve_plan=plan_dig), deps)
    assert view2.state is not None
    assert view2.effective_phase == TrialPhase.READY
    assert view2.state.checkpoint == Checkpoint.DECISION
    assert agents.call_history == [
        "plan",
        "implement",
        "review",
        "fix",
        "review",
    ]


def test_resume_trial_unconfirmed_pending_attempt_rejected(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(
        repo=str(tmp_path), request_text="implement something", agent_name="codex"
    )
    view = start_trial(req, deps)
    assert view.state is not None
    # inject an unconfirmed pending attempt into state
    state = replace(
        view.state,
        pending_attempt=AttemptInput(
            trial_id=view.state.trial_id,
            attempt_id="att1",
            kind=AttemptKind.IMPLEMENT,
            input_head_sha="head",
            base_sha="base",
            plan_digest="plan",
            plan_revision=0,
            cwd="/cwd",
            artifact_dir="/art",
            started_at="2026-01-01T00:00:00Z",
        ),
    )
    store.save(state, expected_revision=None)

    with pytest.raises(
        TrialError, match="Cannot resume with unconfirmed pending attempt"
    ):
        resume_trial(ResumeRequest(trial_id=view.state.trial_id), deps)


def test_resume_trial_reconciles_pending_git(tmp_path: Path) -> None:
    from orchestune.local_trial.models import GitIntent

    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(
        repo=str(tmp_path), request_text="implement something", agent_name="codex"
    )
    view = start_trial(req, deps)
    assert view.state is not None and view.state.plan is not None
    plan_dig = validate_plan(
        view.state.plan, tmp_path, "implement something", ws.base_sha
    )
    state = replace(
        view.state,
        approved_plan_digest=plan_dig,
        checkpoint=Checkpoint.IMPLEMENT,
        phase=TrialPhase.IMPLEMENTING,
    )
    intent = GitIntent(
        operation="commit",
        pre_head_sha=ws.head_sha,
        expected_tree_sha=None,
        target_sha="c" * 40,
        allowed_paths=("foo.py",),
    )
    state = replace(state, pending_git=intent)
    store.save(state, expected_revision=None)

    # Resume should reconcile git intent and proceed to ready
    view_res = resume_trial(ResumeRequest(trial_id=view.state.trial_id), deps)
    assert view_res.state is not None
    assert view_res.state.pending_git is None
    assert view_res.effective_phase == TrialPhase.READY


def test_resume_trial_new_request_text_resets_budget(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(
        repo=str(tmp_path), request_text="initial request", agent_name="codex"
    )
    view1 = start_trial(req, deps)
    assert view1.state is not None

    view2 = resume_trial(
        ResumeRequest(
            trial_id=view1.state.trial_id, request_text="new request with reset"
        ),
        deps,
    )
    assert view2.state is not None
    assert view2.state.request.text == "new request with reset"
    assert view2.state.budget.plan_revision == 1
    assert view2.state.budget.cycles_started == 0
    assert view2.effective_phase == TrialPhase.PLANNED


def test_resume_trial_ci_override_repairs_missing_ci(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    agents.plan_ci_argv = None
    req = StartRequest(
        repo=str(tmp_path), request_text="implement something", agent_name="codex"
    )
    view1 = start_trial(req, deps)
    assert view1.reason == "ci_missing"
    assert view1.state is not None

    # Supply override on resume
    agents.plan_ci_argv = ("pytest", "-v")
    view2 = resume_trial(
        ResumeRequest(trial_id=view1.state.trial_id, ci_argv_override=("pytest", "-v")),
        deps,
    )
    assert view2.state is not None
    assert view2.state.plan is not None
    assert view2.state.plan.ci_argv == ("pytest", "-v")
    assert view2.reason == "approval_required"


def test_execute_review_rejects_dirty_review_worktree(tmp_path: Path) -> None:
    from orchestune.local_trial.models import Change

    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(
        repo=str(tmp_path), request_text="implement something", agent_name="codex"
    )
    view1 = start_trial(req, deps)
    assert view1.state is not None and view1.state.plan is not None
    trial_id = view1.state.trial_id
    plan_dig = validate_plan(
        view1.state.plan, tmp_path, "implement something", ws.base_sha
    )

    # Simulate dirty review worktree during review
    def on_run(req_arg: Any) -> None:
        if req_arg.input.kind == AttemptKind.REVIEW:
            ws.changes = (Change(status="M", path="review_leak.py"),)

    agents.on_run = on_run

    with pytest.raises(TrialError, match="Review worktree modified during review"):
        resume_trial(ResumeRequest(trial_id=trial_id, approve_plan=plan_dig), deps)


def test_record_decision_adopted_requires_includes_head(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(
        repo=str(tmp_path), request_text="implement something", agent_name="codex"
    )
    view1 = start_trial(req, deps)
    assert view1.state is not None and view1.state.plan is not None
    trial_id = view1.state.trial_id
    plan_dig = validate_plan(
        view1.state.plan, tmp_path, "implement something", ws.base_sha
    )
    resume_trial(ResumeRequest(trial_id=trial_id, approve_plan=plan_dig), deps)

    # When head is not included in base branch:
    ws.is_included = False
    with pytest.raises(TrialError, match="Trial head is not included in base branch"):
        record_decision(trial_id, "adopted", deps)


def test_resume_trial_ci_override_planned_and_ready(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(
        repo=str(tmp_path), request_text="implement something", agent_name="codex"
    )
    view = start_trial(req, deps)
    assert view.state is not None and view.state.plan is not None
    tid = view.state.trial_id
    old_dig = validate_plan(
        view.state.plan, tmp_path, "implement something", ws.base_sha
    )

    # 1. Override CI argv while in PLANNED
    res1 = resume_trial(
        ResumeRequest(trial_id=tid, ci_argv_override=("pytest", "-k", "unit")), deps
    )
    assert res1.state is not None and res1.state.plan is not None
    assert res1.state.plan.ci_argv == ("pytest", "-k", "unit")
    assert (
        res1.state.budget.plan_revision,
        res1.state.budget.cycles_started,
        len(proc_runner.call_history),
    ) == (1, 0, 0)
    new_dig = validate_plan(
        res1.state.plan, tmp_path, "implement something", ws.base_sha
    )
    assert new_dig != old_dig

    # Old digest rejected
    with pytest.raises(
        TrialError, match="Approved plan digest does not match current plan"
    ):
        resume_trial(ResumeRequest(trial_id=tid, approve_plan=old_dig), deps)

    # Approve new digest -> runs implement, CI with new argv, review -> reaches READY
    res2 = resume_trial(ResumeRequest(trial_id=tid, approve_plan=new_dig), deps)
    assert res2.effective_phase == TrialPhase.READY
    assert res2.state is not None
    assert proc_runner.call_history == [["pytest", "-k", "unit"]]
    head_after_first = res2.state.head_sha
    agent_calls_before = len(agents.call_history)

    # 2. Override CI argv while in READY
    res3 = resume_trial(
        ResumeRequest(trial_id=tid, ci_argv_override=("pytest", "-v")), deps
    )
    assert res3.effective_phase == TrialPhase.PLANNED
    assert res3.state is not None and res3.state.plan is not None
    assert (
        res3.state.budget.plan_revision,
        res3.state.budget.cycles_started,
        res3.state.approved_plan_digest,
        res3.state.ci_evidence,
        res3.state.review_evidence,
    ) == (2, 1, None, None, None)
    ready_dig = validate_plan(
        res3.state.plan, tmp_path, "implement something", ws.base_sha
    )

    # Approve ready CI update -> directly runs CI and review on existing clean head
    res4 = resume_trial(ResumeRequest(trial_id=tid, approve_plan=ready_dig), deps)
    assert res4.effective_phase == TrialPhase.READY
    assert res4.state is not None
    assert res4.state.head_sha == head_after_first
    assert proc_runner.call_history == [["pytest", "-k", "unit"], ["pytest", "-v"]]
    impl_calls = [
        c for c in agents.call_history[agent_calls_before:] if c in ("implement", "fix")
    ]
    assert impl_calls == []


def test_resume_trial_ci_override_idempotent_and_dirty_and_closed(
    tmp_path: Path,
) -> None:
    from orchestune.local_trial.models import Change

    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(
        repo=str(tmp_path), request_text="implement something", agent_name="codex"
    )
    view = start_trial(req, deps)
    assert view.state is not None
    tid = view.state.trial_id

    # Idempotent: same argv does not bump revision or change plan
    res_same = resume_trial(
        ResumeRequest(trial_id=tid, ci_argv_override=("pytest",)), deps
    )
    assert res_same.state is not None
    assert res_same.state.budget.plan_revision == 0
    assert res_same.state.revision == view.state.revision

    # Dirty worktree rejected
    ws.changes = (Change(status="M", path="dirty.py"),)
    with pytest.raises(TrialError, match="Implementation worktree is dirty"):
        resume_trial(
            ResumeRequest(trial_id=tid, ci_argv_override=("pytest", "-q")), deps
        )
    ws.changes = ()

    # Closed trial rejected
    store.close(replace(view.state, phase=TrialPhase.CLOSED))
    with pytest.raises(TrialError, match="Cannot change CI command for closed trial"):
        resume_trial(
            ResumeRequest(trial_id=tid, ci_argv_override=("pytest", "-q")), deps
        )


def test_resume_trial_ci_override_preserves_budget(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(
        repo=str(tmp_path), request_text="implement something", agent_name="codex"
    )
    view = start_trial(req, deps)
    assert view.state is not None and view.state.plan is not None
    tid = view.state.trial_id
    plan_dig = validate_plan(
        view.state.plan, tmp_path, "implement something", ws.base_sha
    )
    res_ready = resume_trial(ResumeRequest(trial_id=tid, approve_plan=plan_dig), deps)
    assert res_ready.state is not None
    assert res_ready.effective_phase == TrialPhase.READY

    # Simulate exhausted reviews budget
    store.state = replace(
        res_ready.state,
        budget=replace(res_ready.state.budget, reviews_started=3),
    )

    # Change CI command
    res_override = resume_trial(
        ResumeRequest(trial_id=tid, ci_argv_override=("pytest", "-v")), deps
    )
    assert res_override.state is not None and res_override.state.plan is not None
    assert res_override.state.budget.reviews_started == 3
    dig = validate_plan(
        res_override.state.plan, tmp_path, "implement something", ws.base_sha
    )

    # Approving halts with budget_exhausted instead of bypassing limits
    res_blocked = resume_trial(ResumeRequest(trial_id=tid, approve_plan=dig), deps)
    assert res_blocked.effective_phase == TrialPhase.NEEDS_ATTENTION
    assert res_blocked.reason == "budget_exhausted"
