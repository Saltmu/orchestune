"""Lifecycle policy and transition checks for local trial (#1734, #1737)."""

from __future__ import annotations

from orchestune.local_trial.contracts import TrialError, canonical_digest, plan_digest
from orchestune.local_trial.models import (
    AttemptKind,
    AttemptResult,
    Checkpoint,
    DisciplineStatus,
    Evidence,
    ImplementationPayload,
    ProcessRecord,
    ProcessStatus,
    ReviewPayload,
    ReviewVerdict,
    TrialPhase,
    TrialPlan,
    TrialState,
    WorkspaceSnapshot,
)
from orchestune.local_trial.workspace import workspace_digest


def _validate_process_record(result: AttemptResult, expected_kind: AttemptKind) -> None:
    proc = result.process
    if proc.outcome == ProcessStatus.INTERRUPTED or proc.stop_confirmed is False:
        raise TrialError("interrupted", "Process interrupted or stop unconfirmed")
    if proc.outcome == ProcessStatus.TIMED_OUT:
        code = "ci_failed" if expected_kind == AttemptKind.CI else "agent_failed"
        raise TrialError(code, f"Process execution timed out: {proc.detail}")
    if expected_kind != AttemptKind.CI:
        if proc.outcome != ProcessStatus.SUCCESS or proc.returncode != 0:
            raise TrialError("agent_failed", f"Agent process failed: {proc.detail}")
    elif proc.outcome == ProcessStatus.START_FAILED:
        raise TrialError("ci_failed", f"CI start failed: {proc.detail}")


def _validate_fix_payload(inner: object, state: TrialState) -> None:
    if not isinstance(inner, ImplementationPayload):
        return
    valid_fids = {f.id for f in state.findings}
    for r in inner.responses:
        if r.finding_id not in valid_fids:
            raise TrialError(
                "artifact_invalid",
                f"Response for unknown finding_id: {r.finding_id}",
            )


def _validate_review_payload(inner: object) -> None:
    if not isinstance(inner, ReviewPayload):
        return
    if inner.verdict == ReviewVerdict.PASS and len(inner.findings) > 0:
        raise TrialError(
            "review_failed", "Review verdict pass but findings are present"
        )
    if inner.verdict == ReviewVerdict.CHANGES_REQUESTED and len(inner.findings) == 0:
        raise TrialError(
            "review_failed", "Review verdict changes_requested but findings are empty"
        )
    f_ids = [f.id for f in inner.findings]
    if len(f_ids) != len(set(f_ids)):
        raise TrialError("review_failed", "Duplicate finding IDs in review payload")


def _validate_payload(
    result: AttemptResult,
    state: TrialState,
    expected_kind: AttemptKind,
    expected_input_head: str,
    expected_plan_digest: str | None,
) -> None:
    if expected_kind not in (
        AttemptKind.PLAN,
        AttemptKind.IMPLEMENT,
        AttemptKind.FIX,
        AttemptKind.REVIEW,
    ):
        return
    agent_payload = result.agent_payload
    if agent_payload is None:
        raise TrialError("artifact_invalid", "Missing agent payload")
    if (
        agent_payload.trial_id != state.trial_id
        or agent_payload.attempt_id != result.input.attempt_id
    ):
        raise TrialError("identity_mismatch", "Payload trial_id or attempt_id mismatch")
    if agent_payload.kind != expected_kind:
        raise TrialError(
            "artifact_invalid",
            f"Payload kind mismatch: {agent_payload.kind} != {expected_kind}",
        )
    if agent_payload.input_head_sha.lower() != expected_input_head.lower():
        raise TrialError("head_changed", "Agent payload input_head_sha mismatch")
    if (
        expected_plan_digest is not None
        and agent_payload.plan_digest != expected_plan_digest
    ):
        raise TrialError("approval_stale", "Payload plan_digest mismatch")

    inner = agent_payload.payload
    if expected_kind == AttemptKind.FIX:
        _validate_fix_payload(inner, state)
    elif expected_kind == AttemptKind.REVIEW:
        _validate_review_payload(inner)


def validate_attempt(
    result: AttemptResult,
    state: TrialState,
    expected_kind: AttemptKind,
    expected_input_head: str,
    expected_plan_digest: str | None,
) -> None:
    """Validate an attempt result against state, kind, head and plan digests."""
    inp = result.input
    if inp.trial_id != state.trial_id:
        raise TrialError("identity_mismatch", "Trial ID mismatch in attempt result")
    if inp.kind != expected_kind:
        raise TrialError(
            "artifact_invalid",
            f"Attempt kind {inp.kind} does not match expected {expected_kind}",
        )
    if inp.input_head_sha.lower() != expected_input_head.lower():
        raise TrialError("head_changed", "Attempt input_head_sha mismatch")
    if expected_plan_digest is not None and inp.plan_digest != expected_plan_digest:
        raise TrialError("approval_stale", "Attempt plan_digest mismatch")

    _validate_process_record(result, expected_kind)
    _validate_payload(
        result, state, expected_kind, expected_input_head, expected_plan_digest
    )


def next_phase(state: TrialState) -> tuple[TrialPhase, Checkpoint, str | None]:
    """Pure state transition logic determining next phase and checkpoint."""
    if state.approved_plan_digest is None:
        return (
            TrialPhase.PLANNED,
            Checkpoint.AWAIT_APPROVAL,
            state.reason or "approval_required",
        )
    if state.budget.cycles_started >= 3:
        return (TrialPhase.NEEDS_ATTENTION, Checkpoint.DECISION, "budget_exhausted")
    if state.findings:
        return (TrialPhase.FIXING, Checkpoint.IMPLEMENT, None)
    if state.ci_evidence is None:
        return (TrialPhase.IMPLEMENTING, Checkpoint.CI, None)
    if state.review_evidence is None:
        return (TrialPhase.REVIEWING, Checkpoint.REVIEW, None)
    return (TrialPhase.READY, Checkpoint.DECISION, None)


def _check_evidence_match(ev, state: TrialState) -> bool:
    if ev is None or not ev.passed:
        return False
    if ev.head_sha.lower() != state.head_sha.lower():
        return False
    if ev.plan_digest != state.approved_plan_digest:
        return False
    return bool(ev.base_sha.lower() == state.repository.base_sha.lower())


def assess_ready(
    state: TrialState,
    workspace_snapshot: WorkspaceSnapshot,
    workspace_includes_head: bool = True,
) -> tuple[bool, str | None]:
    """Check if current state and workspace snapshot satisfy all ready criteria."""
    if state.approved_plan_digest is None:
        return (False, "approval_required")
    if state.plan is None:
        return (False, "plan_missing")
    if workspace_snapshot.base_tip_sha.lower() != state.repository.base_sha.lower():
        if not (
            workspace_includes_head
            and workspace_snapshot.base_tip_sha.lower() == state.head_sha.lower()
        ):
            return (False, "base_changed")
    if workspace_snapshot.head_sha.lower() != state.head_sha.lower():
        return (False, "head_changed")
    if workspace_snapshot.changes:
        return (False, "worktree_dirty")
    if state.pending_attempt is not None or state.pending_git is not None:
        return (False, "execution_unknown")
    if not _check_evidence_match(state.ci_evidence, state):
        return (False, "ci_failed")
    if not _check_evidence_match(state.review_evidence, state):
        return (False, "review_failed")
    if state.findings:
        return (False, "findings_pending")
    return (True, None)


def validate_plan_approval(state: TrialState, approved_digest: str | None) -> str:
    """Validate that plan is eligible for user approval and return valid digest."""
    if state.plan is None:
        raise TrialError("state_invalid", "Cannot approve trial without plan")
    if approved_digest is None:
        raise TrialError("approval_stale", "Missing approved plan digest")
    curr_dig = plan_digest(state.plan, state.request.digest, state.repository.base_sha)
    if approved_digest != curr_dig:
        raise TrialError(
            "approval_stale", "Approved plan digest does not match current plan"
        )
    if state.plan.discipline_status != DisciplineStatus.COMPATIBLE:
        raise TrialError(
            "discipline_incompatible", "Plan discipline status is not compatible"
        )
    if state.plan.ci_argv is None:
        raise TrialError("ci_missing", "Plan has no CI command")
    return curr_dig


def determine_plan_reason(plan: TrialPlan) -> str:
    """Determine why a plan is not yet ready for immediate approval."""
    st = str(plan.discipline_status)
    if st != "compatible":
        return f"discipline_{st}"
    return "ci_missing" if plan.ci_argv is None else "approval_required"


def classify_ci_failure(
    state: TrialState, proc_rec: ProcessRecord
) -> tuple[TrialPhase, str | None, Checkpoint]:
    """Map CI outcome and retry count to phase, reason, and checkpoint."""
    if (
        proc_rec.outcome in (ProcessStatus.START_FAILED, ProcessStatus.TIMED_OUT)
        or proc_rec.stop_confirmed is False
    ):
        return TrialPhase.NEEDS_ATTENTION, "ci_failed", state.checkpoint
    if state.budget.cycles_started < 3:
        return TrialPhase.FIXING, state.reason, Checkpoint.IMPLEMENT
    return TrialPhase.NEEDS_ATTENTION, "budget_exhausted", state.checkpoint


def validate_persisted_attempt(
    state: TrialState, persisted: AttemptResult | None, schema_version: int = 1
) -> AttemptResult:
    """Validate a loaded attempt result against the current state pending attempt."""
    if persisted is None:
        raise TrialError(
            "execution_unknown", "Cannot resume with unconfirmed pending attempt"
        )
    if persisted.schema_version != schema_version:
        raise TrialError("artifact_invalid", "Invalid attempt schema version")
    pi, si = persisted.input, state.pending_attempt
    assert si is not None
    fields = (
        "trial_id attempt_id kind input_head_sha base_sha "
        "plan_digest plan_revision cwd artifact_dir"
    ).split()
    if any(getattr(pi, f) != getattr(si, f) for f in fields):
        raise TrialError("artifact_invalid", "Pending attempt input mismatch")
    if persisted.process is None or not persisted.process.stop_confirmed:
        raise TrialError("execution_unknown", "Process not confirmed stopped")
    return persisted


def make_evidence(
    attempt_id: str,
    state: TrialState,
    ci_argv: tuple[str, ...],
    plan_digest: str,
) -> Evidence:
    """Create a passing CI or Review evidence record."""
    return Evidence(
        attempt_id=attempt_id,
        base_sha=state.repository.base_sha,
        head_sha=state.head_sha,
        plan_digest=plan_digest,
        ci_argv_digest=canonical_digest(list(ci_argv)),
        passed=True,
    )


def verify_clean_plan(before: WorkspaceSnapshot, after: WorkspaceSnapshot) -> None:
    """Verify worktree was not modified during planning phase."""
    if (
        after.head_sha != before.head_sha
        or after.index_digest != before.index_digest
        or after.content_digest != before.content_digest
    ):
        raise TrialError("worktree_dirty", "Worktree modified during planning phase")


def verify_clean_review(snap: WorkspaceSnapshot, head_sha: str) -> None:
    """Verify review worktree is clean and matches trial head SHA."""
    if snap.head_sha != head_sha or snap.changes:
        raise TrialError("worktree_dirty", "Review worktree modified during review")


def is_workspace_dirty_unapproved(state: TrialState, snap: WorkspaceSnapshot) -> bool:
    """Check if workspace has changes that are not approved by a confirmation receipt."""
    if not snap.changes:
        return False
    return not (
        state.approved_worktree_digest == workspace_digest(snap)
        and state.approved_index_digest == snap.index_digest
    )


def resolve_resume_needs_attention(
    state: TrialState,
) -> tuple[TrialPhase, Checkpoint] | None:
    """Resolve next phase and checkpoint when safely resuming from needs_attention."""
    if (
        state.phase != TrialPhase.NEEDS_ATTENTION
        or state.pending_attempt is not None
        or state.pending_git is not None
        or state.reason
        in (
            "budget_exhausted",
            "base_changed",
            "identity_mismatch",
            "head_changed",
        )
        or not state.stop_confirmations
    ):
        return None
    chk = state.checkpoint
    if chk == Checkpoint.PLAN:
        return TrialPhase.PLANNING, chk
    if chk == Checkpoint.IMPLEMENT and state.budget.cycles_started < 3:
        return TrialPhase.IMPLEMENTING, chk
    if chk == Checkpoint.CI and state.budget.reviews_started < 3:
        return TrialPhase.IMPLEMENTING, chk
    if chk == Checkpoint.REVIEW and state.budget.reviews_started < 3:
        chk_ci = not _check_evidence_match(state.ci_evidence, state)
        return (
            (TrialPhase.IMPLEMENTING, Checkpoint.CI)
            if chk_ci
            else (TrialPhase.REVIEWING, chk)
        )
    return None
