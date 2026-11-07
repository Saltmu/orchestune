"""Unit tests for local trial policy and attempt validation (#1737)."""

from __future__ import annotations

import pytest

from orchestune.local_trial.contracts import TrialError
from orchestune.local_trial.models import (
    AgentInfo,
    AgentPayload,
    AttemptInput,
    AttemptKind,
    AttemptResult,
    Budget,
    Checkpoint,
    DisciplineStatus,
    Evidence,
    Finding,
    FindingAction,
    FindingResponse,
    FindingSeverity,
    ImplementationPayload,
    Observation,
    ProcessRecord,
    ProcessStatus,
    RepositoryIdentity,
    RequestSnapshot,
    ReviewPayload,
    ReviewVerdict,
    TrialPhase,
    TrialPlan,
    TrialState,
    WorkspaceIdentity,
    WorkspaceSnapshot,
)
from orchestune.local_trial.policy import assess_ready, validate_attempt


def _dummy_state(
    *,
    trial_id: str = "t1",
    head_sha: str = "a" * 40,
    base_sha: str = "b" * 40,
    plan_digest: str | None = "p" * 64,
    findings: tuple[Finding, ...] = (),
) -> TrialState:
    return TrialState(
        schema_version=1,
        trial_id=trial_id,
        repository=RepositoryIdentity(
            source_root="/repo",
            primary_root="/repo",
            common_dir="/repo/.git",
            base_ref="main",
            base_sha=base_sha,
        ),
        workspace=WorkspaceIdentity(
            implementation_path=f"/repo/.orchestune/worktrees/{trial_id}/impl",
            review_path=f"/repo/.orchestune/worktrees/{trial_id}/rev",
            branch=f"trial/{trial_id}",
        ),
        request=RequestSnapshot(text="test request", digest="r" * 64),
        agent=AgentInfo(name="codex", executable="/bin/codex", version="1.0.0"),
        plan=TrialPlan(
            goal="test",
            acceptance_criteria=("ac1",),
            write_paths=("foo.py",),
            discipline_paths=(),
            discipline_notes=(),
            ci_argv=("pytest",),
            discipline_status=DisciplineStatus.COMPATIBLE,
        ),
        approved_plan_digest=plan_digest,
        approved_worktree_digest="w" * 64,
        approved_index_digest="i" * 64,
        phase=TrialPhase.IMPLEMENTING,
        checkpoint=Checkpoint.IMPLEMENT,
        reason=None,
        revision=1,
        budget=Budget(plan_revision=0, cycles_started=1, reviews_started=0),
        head_sha=head_sha,
        pending_attempt=None,
        pending_git=None,
        attempt_ids=(),
        ci_evidence=None,
        review_evidence=None,
        findings=findings,
        responses=(),
        stop_confirmations=(),
        controller=None,
        observation=Observation(
            started_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            elapsed_seconds=0.0,
        ),
        decision=None,
    )


def _proc(
    *,
    outcome: ProcessStatus = ProcessStatus.SUCCESS,
    returncode: int = 0,
    stop_confirmed: bool = True,
) -> ProcessRecord:
    return ProcessRecord(
        outcome=outcome,
        returncode=returncode,
        stop_confirmed=stop_confirmed,
        elapsed_seconds=1.0,
        timeout_seconds=60.0,
        stdout_tail="",
        stderr_tail="",
        detail="",
    )


def _attempt_input(
    *,
    trial_id: str = "t1",
    attempt_id: str = "att1",
    kind: AttemptKind = AttemptKind.PLAN,
    input_head_sha: str = "a" * 40,
    base_sha: str = "b" * 40,
    plan_digest: str | None = None,
) -> AttemptInput:
    return AttemptInput(
        trial_id=trial_id,
        attempt_id=attempt_id,
        kind=kind,
        input_head_sha=input_head_sha,
        base_sha=base_sha,
        plan_digest=plan_digest,
        plan_revision=0,
        cwd="/repo",
        artifact_dir="/repo/artifacts",
        started_at="2026-01-01T00:00:00Z",
    )


def test_validate_attempt_identity_mismatch() -> None:
    state = _dummy_state(trial_id="t1")
    inp = _attempt_input(trial_id="other", kind=AttemptKind.PLAN)
    res = AttemptResult(
        schema_version=1,
        input=inp,
        process=_proc(),
        agent_payload=None,
        ended_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(TrialError) as exc:
        validate_attempt(res, state, AttemptKind.PLAN, "a" * 40, None)
    assert exc.value.code == "identity_mismatch"


def test_validate_attempt_kind_mismatch() -> None:
    state = _dummy_state(trial_id="t1")
    inp = _attempt_input(trial_id="t1", kind=AttemptKind.PLAN)
    res = AttemptResult(
        schema_version=1,
        input=inp,
        process=_proc(),
        agent_payload=None,
        ended_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(TrialError) as exc:
        validate_attempt(res, state, AttemptKind.IMPLEMENT, "a" * 40, None)
    assert exc.value.code == "artifact_invalid"


def test_validate_attempt_head_mismatch() -> None:
    state = _dummy_state(trial_id="t1", head_sha="a" * 40)
    inp = _attempt_input(trial_id="t1", kind=AttemptKind.PLAN, input_head_sha="c" * 40)
    res = AttemptResult(
        schema_version=1,
        input=inp,
        process=_proc(),
        agent_payload=None,
        ended_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(TrialError) as exc:
        validate_attempt(res, state, AttemptKind.PLAN, "a" * 40, None)
    assert exc.value.code == "head_changed"


def test_validate_attempt_fix_unknown_finding() -> None:
    state = _dummy_state(
        trial_id="t1",
        findings=(
            Finding(
                id="f1",
                severity=FindingSeverity.HIGH,
                path="foo.py",
                line=1,
                reason="err",
                requested_change="fix",
            ),
        ),
    )
    inp = _attempt_input(
        trial_id="t1",
        kind=AttemptKind.FIX,
        input_head_sha="a" * 40,
        plan_digest="p" * 64,
    )
    payload = ImplementationPayload(
        summary="fixed",
        responses=(
            FindingResponse(
                finding_id="unknown_finding",
                action=FindingAction.FIXED,
                explanation="done",
            ),
        ),
    )
    agent_payload = AgentPayload(
        schema_version=1,
        trial_id="t1",
        attempt_id="att1",
        kind=AttemptKind.FIX,
        input_head_sha="a" * 40,
        plan_digest="p" * 64,
        payload=payload,
    )
    res = AttemptResult(
        schema_version=1,
        input=inp,
        process=_proc(),
        agent_payload=agent_payload,
        ended_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(TrialError) as exc:
        validate_attempt(res, state, AttemptKind.FIX, "a" * 40, "p" * 64)
    assert exc.value.code == "artifact_invalid"


def test_validate_attempt_review_verdict_findings_contradiction() -> None:
    state = _dummy_state(trial_id="t1")
    inp = _attempt_input(
        trial_id="t1",
        kind=AttemptKind.REVIEW,
        input_head_sha="a" * 40,
        plan_digest="p" * 64,
    )
    finding = Finding(
        id="f1",
        severity=FindingSeverity.MEDIUM,
        path="foo.py",
        line=10,
        reason="warn",
        requested_change="fix",
    )
    # PASS with findings
    p_pass = ReviewPayload(
        verdict=ReviewVerdict.PASS,
        findings=(finding,),
    )
    res = AttemptResult(
        schema_version=1,
        input=inp,
        process=_proc(),
        agent_payload=AgentPayload(
            schema_version=1,
            trial_id="t1",
            attempt_id="att1",
            kind=AttemptKind.REVIEW,
            input_head_sha="a" * 40,
            plan_digest="p" * 64,
            payload=p_pass,
        ),
        ended_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(TrialError) as exc:
        validate_attempt(res, state, AttemptKind.REVIEW, "a" * 40, "p" * 64)
    assert exc.value.code == "review_failed"

    # CHANGES_REQUESTED with empty findings
    p_cr = ReviewPayload(
        verdict=ReviewVerdict.CHANGES_REQUESTED,
        findings=(),
    )
    res2 = AttemptResult(
        schema_version=1,
        input=inp,
        process=_proc(),
        agent_payload=AgentPayload(
            schema_version=1,
            trial_id="t1",
            attempt_id="att1",
            kind=AttemptKind.REVIEW,
            input_head_sha="a" * 40,
            plan_digest="p" * 64,
            payload=p_cr,
        ),
        ended_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(TrialError) as exc2:
        validate_attempt(res2, state, AttemptKind.REVIEW, "a" * 40, "p" * 64)
    assert exc2.value.code == "review_failed"


def test_validate_attempt_interrupted() -> None:
    state = _dummy_state(trial_id="t1")
    inp = _attempt_input(trial_id="t1", kind=AttemptKind.PLAN)
    res = AttemptResult(
        schema_version=1,
        input=inp,
        process=_proc(outcome=ProcessStatus.INTERRUPTED, stop_confirmed=False),
        agent_payload=None,
        ended_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(TrialError) as exc:
        validate_attempt(res, state, AttemptKind.PLAN, "a" * 40, None)
    assert exc.value.code == "interrupted"


def test_assess_ready_matrix() -> None:
    state = _dummy_state()
    snap = WorkspaceSnapshot(
        head_sha="a" * 40,
        base_tip_sha="b" * 40,
        index_digest="i" * 64,
        content_digest="c" * 64,
        branch=None,
        changes=(),
    )
    # Missing CI evidence
    ready, reason = assess_ready(state, "b" * 40, snap, True)
    assert ready is False
    assert reason == "ci_failed"

    # Add passed CI evidence
    from dataclasses import replace

    ci_ev = Evidence(
        attempt_id="ci1",
        base_sha="b" * 40,
        head_sha="a" * 40,
        plan_digest="p" * 64,
        ci_argv_digest="c" * 64,
        passed=True,
    )
    state = replace(state, ci_evidence=ci_ev)
    ready, reason = assess_ready(state, "b" * 40, snap, True)
    assert ready is False
    assert reason == "review_failed"

    # Add passed Review evidence
    rev_ev = Evidence(
        attempt_id="rev1",
        base_sha="b" * 40,
        head_sha="a" * 40,
        plan_digest="p" * 64,
        ci_argv_digest="c" * 64,
        passed=True,
    )
    state = replace(state, review_evidence=rev_ev)
    ready, reason = assess_ready(state, "b" * 40, snap, True)
    assert ready is True
    assert reason is None

    # Dirty workspace
    from orchestune.local_trial.models import Change

    snap_dirty = replace(snap, changes=(Change(status="M", path="foo.py"),))
    ready, reason = assess_ready(state, "b" * 40, snap_dirty, True)
    assert ready is False
    assert reason == "worktree_dirty"

    # Base changed
    ready, reason = assess_ready(state, "x" * 40, snap, True)
    assert ready is False
    assert reason == "base_changed"
