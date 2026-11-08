from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from orchestune.local_trial.contracts import (
    AgentInfo,
    AgentPayload,
    AttemptInput,
    AttemptKind,
    AttemptResult,
    Checkpoint,
    DisciplineStatus,
    ImplementationPayload,
    PlanPayload,
    ProcessRecord,
    ProcessStatus,
    TrialError,
    TrialPhase,
    TrialPlan,
)
from orchestune.local_trial.models import Change, GitIntent
from orchestune.local_trial.planning import validate_plan
from orchestune.local_trial.service import (
    ResumeRequest,
    StartRequest,
    TrialDependencies,
    _reconcile_pending_git,
    resume_trial,
    start_trial,
)
from orchestune.local_trial.state import LocalTrialStore
from tests.test_local_trial_service import (
    FakeAgentRunner,
    FakeProcessRunner,
    FakeWorkspace,
    _make_fake_deps,
)


def _proc_rec(
    *,
    outcome: ProcessStatus = ProcessStatus.SUCCESS,
    returncode: int | None = 0,
    stop_confirmed: bool = True,
    elapsed_seconds: float = 1.0,
    timeout_seconds: float = 60.0,
    stdout_tail: str = "",
    stderr_tail: str = "",
    detail: str = "",
) -> ProcessRecord:
    return ProcessRecord(
        outcome=outcome,
        returncode=returncode,
        stop_confirmed=stop_confirmed,
        elapsed_seconds=elapsed_seconds,
        timeout_seconds=timeout_seconds,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        detail=detail,
    )


def test_reconcile_pending_git_prepare_and_review(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(repo=str(tmp_path), request_text="req", agent_name="codex")
    view = start_trial(req, deps)
    state = view.state
    assert state is not None

    # Test "prepare"
    intent_prep = GitIntent(
        operation="prepare",
        pre_head_sha=ws.base_sha,
        expected_tree_sha=None,
        target_sha=ws.base_sha,
        allowed_paths=(),
    )
    s_prep = replace(state, pending_git=intent_prep)
    store.save(s_prep, expected_revision=None)
    reconciled = _reconcile_pending_git(s_prep, deps)
    assert reconciled.pending_git is None
    assert reconciled.checkpoint == Checkpoint.PLAN

    # Resuming after prepare recovery runs planning stage to AWAIT_APPROVAL
    res_prep = resume_trial(ResumeRequest(trial_id=state.trial_id), deps)
    assert res_prep.state is not None
    assert res_prep.state.pending_git is None
    assert res_prep.state.checkpoint == Checkpoint.AWAIT_APPROVAL
    assert res_prep.effective_phase == TrialPhase.PLANNED

    # Test "prepare_review"
    intent_rev = GitIntent(
        operation="prepare_review",
        pre_head_sha=state.head_sha,
        expected_tree_sha=None,
        target_sha=state.head_sha,
        allowed_paths=(),
    )
    s_rev = replace(state, pending_git=intent_rev)
    store.save(s_rev, expected_revision=None)
    reconciled_rev = _reconcile_pending_git(s_rev, deps)
    assert reconciled_rev.pending_git is None


def test_reconcile_pending_git_commit(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(repo=str(tmp_path), request_text="req", agent_name="codex")
    view = start_trial(req, deps)
    state = view.state
    assert state is not None

    intent = GitIntent(
        operation="commit",
        pre_head_sha=ws.head_sha,
        expected_tree_sha=None,
        target_sha="b" * 40,
        allowed_paths=("foo.py",),
    )
    s_commit = replace(state, pending_git=intent)
    store.save(s_commit, expected_revision=None)

    class NoneReconcileWs(FakeWorkspace):
        def reconcile_commit(self, st, it):
            return None

    deps_none = replace(deps, workspace=NoneReconcileWs(tmp_path))
    with pytest.raises(TrialError) as exc:
        _reconcile_pending_git(s_commit, deps_none)
    assert exc.value.code == "execution_unknown"
    assert store.load(state.trial_id).pending_git is not None

    reconciled = _reconcile_pending_git(s_commit, deps)
    assert reconciled.pending_git is None
    assert reconciled.checkpoint == Checkpoint.CI
    assert reconciled.head_sha == ws.head_sha


def test_reconcile_pending_attempt_validation_errors(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(repo=str(tmp_path), request_text="req", agent_name="codex")
    view = start_trial(req, deps)
    state = view.state
    assert state is not None

    att_inp = AttemptInput(
        trial_id=state.trial_id,
        attempt_id="att1",
        kind=AttemptKind.PLAN,
        input_head_sha=state.head_sha,
        base_sha=state.repository.base_sha,
        plan_digest=None,
        plan_revision=0,
        cwd=state.workspace.implementation_path,
        artifact_dir="/art",
        started_at="2026-01-01T00:00:00Z",
    )
    s_pending = replace(state, pending_attempt=att_inp)
    store.save(s_pending, expected_revision=None)

    # 1. Missing persisted attempt -> execution_unknown
    with pytest.raises(TrialError) as exc1:
        resume_trial(ResumeRequest(trial_id=state.trial_id), deps)
    assert exc1.value.code == "execution_unknown"

    # 2. Stop not confirmed -> execution_unknown
    proc_unconf = _proc_rec(stop_confirmed=False)
    res_unconf = AttemptResult(
        schema_version=1,
        input=att_inp,
        process=proc_unconf,
        agent_payload=None,
        ended_at="2026-01-01T00:00:00Z",
    )
    store.record_attempt(res_unconf)
    with pytest.raises(TrialError) as exc2:
        resume_trial(ResumeRequest(trial_id=state.trial_id), deps)
    assert exc2.value.code == "execution_unknown"

    # 3. Input mismatch -> artifact_invalid
    proc_ok = _proc_rec(stop_confirmed=True)
    mismatch_inp = replace(att_inp, base_sha="different_base")
    res_mismatch = replace(res_unconf, input=mismatch_inp, process=proc_ok)
    store.record_attempt(res_mismatch)
    with pytest.raises(TrialError) as exc3:
        resume_trial(ResumeRequest(trial_id=state.trial_id), deps)
    assert exc3.value.code == "artifact_invalid"

    # 4. Dirty worktree during PLAN -> worktree_dirty
    valid_res = replace(res_unconf, process=proc_ok)
    store.record_attempt(valid_res)
    ws.changes = (Change(status="M", path="dirty.py"),)
    with pytest.raises(TrialError) as exc4:
        resume_trial(ResumeRequest(trial_id=state.trial_id), deps)
    assert exc4.value.code == "worktree_dirty"


def test_reconcile_pending_attempt_success_plan_and_ci(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(repo=str(tmp_path), request_text="req", agent_name="codex")
    view = start_trial(req, deps)
    state = view.state
    assert state is not None

    plan = TrialPlan(
        goal="test plan",
        acceptance_criteria=("ac1",),
        write_paths=("foo.py",),
        discipline_paths=(),
        discipline_notes=(),
        ci_argv=("pytest",),
        discipline_status=DisciplineStatus.COMPATIBLE,
    )
    att_id = "plan_att_1"
    plan_inp = AttemptInput(
        trial_id=state.trial_id,
        attempt_id=att_id,
        kind=AttemptKind.PLAN,
        input_head_sha=state.head_sha,
        base_sha=state.repository.base_sha,
        plan_digest=None,
        plan_revision=0,
        cwd=state.workspace.implementation_path,
        artifact_dir="/art",
        started_at="2026-01-01T00:00:00Z",
    )
    plan_res = AttemptResult(
        schema_version=1,
        input=plan_inp,
        process=_proc_rec(stop_confirmed=True),
        agent_payload=AgentPayload(
            schema_version=1,
            trial_id=state.trial_id,
            attempt_id=att_id,
            kind=AttemptKind.PLAN,
            input_head_sha=state.head_sha,
            plan_digest=None,
            payload=PlanPayload(plan=plan),
        ),
        ended_at="2026-01-01T00:00:00Z",
    )
    store.record_attempt(plan_res)
    s_plan = replace(state, pending_attempt=plan_inp)
    store.save(s_plan, expected_revision=None)

    agent_calls = len(agents.call_history)
    res_view = resume_trial(ResumeRequest(trial_id=state.trial_id), deps)
    assert len(agents.call_history) == agent_calls
    assert res_view.state is not None
    assert res_view.state.pending_attempt is None
    assert res_view.state.checkpoint == Checkpoint.AWAIT_APPROVAL
    assert att_id in res_view.state.attempt_ids


def test_reconcile_pending_attempt_implement_and_review(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(repo=str(tmp_path), request_text="req", agent_name="codex")
    view = start_trial(req, deps)
    state = view.state
    assert state is not None and state.plan is not None

    plan_dig = validate_plan(state.plan, tmp_path, "req", ws.base_sha)
    s_approved = replace(
        state,
        approved_plan_digest=plan_dig,
        checkpoint=Checkpoint.IMPLEMENT,
        phase=TrialPhase.IMPLEMENTING,
        budget=replace(state.budget, cycles_started=1),
    )

    impl_id = "impl_att_1"
    impl_inp = AttemptInput(
        trial_id=state.trial_id,
        attempt_id=impl_id,
        kind=AttemptKind.IMPLEMENT,
        input_head_sha=state.head_sha,
        base_sha=state.repository.base_sha,
        plan_digest=plan_dig,
        plan_revision=0,
        cwd=state.workspace.implementation_path,
        artifact_dir="/art",
        started_at="2026-01-01T00:00:00Z",
    )
    impl_res = AttemptResult(
        schema_version=1,
        input=impl_inp,
        process=_proc_rec(stop_confirmed=True),
        agent_payload=AgentPayload(
            schema_version=1,
            trial_id=state.trial_id,
            attempt_id=impl_id,
            kind=AttemptKind.IMPLEMENT,
            input_head_sha=state.head_sha,
            plan_digest=plan_dig,
            payload=ImplementationPayload(summary="done", responses=()),
        ),
        ended_at="2026-01-01T00:00:00Z",
    )
    store.record_attempt(impl_res)
    ws.changes = (Change(status="M", path="foo.py"),)
    s_impl = replace(s_approved, pending_attempt=impl_inp)
    store.save(s_impl, expected_revision=None)

    agent_calls = len(agents.call_history)
    res_view = resume_trial(ResumeRequest(trial_id=state.trial_id), deps)
    assert "implement" not in agents.call_history[agent_calls:]
    assert res_view.effective_phase == TrialPhase.READY
    assert res_view.state is not None
    assert impl_id in res_view.state.attempt_ids
    assert res_view.state.budget.cycles_started == 1


def test_reconcile_pending_attempt_ci_and_review(tmp_path: Path) -> None:
    deps, store, ws, agents, proc_runner = _make_fake_deps(tmp_path)
    req = StartRequest(repo=str(tmp_path), request_text="req", agent_name="codex")
    view = start_trial(req, deps)
    state = view.state
    assert state is not None and state.plan is not None

    plan_dig = validate_plan(state.plan, tmp_path, "req", ws.base_sha)
    s_ci = replace(
        state,
        approved_plan_digest=plan_dig,
        checkpoint=Checkpoint.CI,
        phase=TrialPhase.VALIDATING,
        budget=replace(state.budget, cycles_started=3),
    )

    # Persisted CI failure attempt
    ci_id = "ci_att_1"
    ci_inp = AttemptInput(
        trial_id=state.trial_id,
        attempt_id=ci_id,
        kind=AttemptKind.CI,
        input_head_sha=state.head_sha,
        base_sha=state.repository.base_sha,
        plan_digest=plan_dig,
        plan_revision=0,
        cwd=state.workspace.implementation_path,
        artifact_dir="/art",
        started_at="2026-01-01T00:00:00Z",
    )
    ci_res = AttemptResult(
        schema_version=1,
        input=ci_inp,
        process=_proc_rec(
            outcome=ProcessStatus.NONZERO_EXIT,
            returncode=1,
            stop_confirmed=True,
            stdout_tail="pytest failed\n",
        ),
        agent_payload=None,
        ended_at="2026-01-01T00:00:00Z",
    )
    store.record_attempt(ci_res)
    s_ci_pending = replace(s_ci, pending_attempt=ci_inp)
    store.save(s_ci_pending, expected_revision=None)

    ci_calls = len(proc_runner.call_history)
    res_view = resume_trial(ResumeRequest(trial_id=state.trial_id), deps)
    # Process runner wasn't called again for the reconciled CI attempt
    assert len(proc_runner.call_history) == ci_calls
    assert res_view.effective_phase == TrialPhase.NEEDS_ATTENTION
    assert res_view.reason == "budget_exhausted"
    assert res_view.state is not None
    assert ci_id in res_view.state.attempt_ids

    # Persisted CI timed out attempt recovers to NEEDS_ATTENTION with ci_failed
    ci_to_id = "ci_att_to"
    ci_to_inp = replace(ci_inp, attempt_id=ci_to_id)
    ci_to_res = AttemptResult(
        schema_version=1,
        input=ci_to_inp,
        process=_proc_rec(
            outcome=ProcessStatus.TIMED_OUT,
            returncode=None,
            stop_confirmed=True,
            stdout_tail="CI timed out\n",
        ),
        agent_payload=None,
        ended_at="2026-01-01T00:00:00Z",
    )
    store.record_attempt(ci_to_res)
    s_ci_to_pending = replace(s_ci, pending_attempt=ci_to_inp)
    store.save(s_ci_to_pending, expected_revision=None)

    res_to_view = resume_trial(ResumeRequest(trial_id=state.trial_id), deps)
    assert res_to_view.effective_phase == TrialPhase.NEEDS_ATTENTION
    assert res_to_view.reason == "ci_failed"
    assert res_to_view.state is not None
    assert res_to_view.state.pending_attempt is None
    assert ci_to_id in res_to_view.state.attempt_ids


def test_reconcile_with_real_store(tmp_path: Path) -> None:
    store = LocalTrialStore(
        common_dir=tmp_path / "common", primary_root=tmp_path / "primary"
    )
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
        new_id=lambda: uuid.uuid4().hex,
        controller_alive=lambda pid: True,
    )
    req = StartRequest(repo=str(tmp_path), request_text="req", agent_name="codex")
    view = start_trial(req, deps)
    state = view.state
    assert state is not None and state.plan is not None

    plan_dig = validate_plan(state.plan, tmp_path, "req", ws.base_sha)

    # Simulate an interrupted attempt persisted with real store
    attempt_id = "b" * 32
    att_inp = AttemptInput(
        trial_id=state.trial_id,
        attempt_id=attempt_id,
        kind=AttemptKind.IMPLEMENT,
        input_head_sha=state.head_sha,
        base_sha=state.repository.base_sha,
        plan_digest=plan_dig,
        plan_revision=0,
        cwd=state.workspace.implementation_path,
        artifact_dir=f"{state.workspace.implementation_path}/.orchestune/artifacts/{attempt_id}",
        started_at="2026-01-01T00:00:00Z",
    )
    st_loaded = store.load(state.trial_id)
    st_pending = replace(
        st_loaded,
        revision=st_loaded.revision + 1,
        approved_plan_digest=plan_dig,
        checkpoint=Checkpoint.IMPLEMENT,
        phase=TrialPhase.IMPLEMENTING,
        budget=replace(st_loaded.budget, cycles_started=1),
        pending_attempt=att_inp,
    )
    with store.lock():
        store.save(st_pending, expected_revision=st_loaded.revision)
        store.record_attempt(
            AttemptResult(
                schema_version=1,
                input=att_inp,
                process=_proc_rec(stop_confirmed=True),
                agent_payload=AgentPayload(
                    schema_version=1,
                    trial_id=state.trial_id,
                    attempt_id=attempt_id,
                    kind=AttemptKind.IMPLEMENT,
                    input_head_sha=state.head_sha,
                    plan_digest=plan_dig,
                    payload=ImplementationPayload(summary="done", responses=()),
                ),
                ended_at="2026-01-01T00:00:00Z",
            )
        )
    ws.changes = (Change(status="M", path="foo.py"),)

    res_view = resume_trial(ResumeRequest(trial_id=state.trial_id), deps)
    assert res_view.effective_phase == TrialPhase.READY
    assert res_view.state is not None
    assert res_view.state.pending_attempt is None
    assert attempt_id in res_view.state.attempt_ids

    # Repeated resume does not fail with stale_revision
    res_repeat = resume_trial(ResumeRequest(trial_id=state.trial_id), deps)
    assert res_repeat.effective_phase == TrialPhase.READY
