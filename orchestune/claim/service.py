"""Unified claim orchestration service integrating validation, reservation, worktree, and state."""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orchestune.branch_naming import build_task_branch_name
from orchestune.claim.contracts import (
    ClaimFailure,
    ClaimFailureReason,
    ClaimOutcome,
    ClaimRequest,
    ClaimStage,
    OwnerKind,
    ReservationKind,
)
from orchestune.claim.ownership import (
    ClaimConflict,
    ClaimConflictReason,
    OwnerToken,
    build_reservation,
    evaluate_claim_conflicts,
    new_owner_token,
    owner_token_digest,
)
from orchestune.claim.preflight import (
    ClaimBaseResolutionView,
    PreflightDecision,
    evaluate_claim_preflight,
)
from orchestune.claim.workspace import (
    ClaimWorkspace,
    check_repository_identity_match,
    resolve_claim_workspace,
)
from orchestune.dispatch.labels import transition_status_label
from orchestune.dispatch.state import (
    ActiveWorktree,
    RunState,
    load_run_state,
    save_run_state,
)
from orchestune.dispatch.worktree import prepare_task_worktree
from orchestune.forge import Forge, GitHubForge
from orchestune.infra.git_cli import run_git
from orchestune.infra.process_utils import FileLockContentionError, run_state_lock
from orchestune.issue_parsing import parse_task_from_issue
from orchestune.labels import STATUS_LABEL_PREFIX, StatusLabel
from orchestune.models import IssueRecord
from orchestune.task_metadata import TaskMetadata


class _DefaultConflictView:
    """Fallback conflict view looking up tasks via forge when no view is provided."""

    def __init__(
        self, forge: Forge, cache: dict[int, TaskMetadata] | None = None
    ) -> None:
        self._forge = forge
        self._cache: dict[int, TaskMetadata] = dict(cache or {})

    def task(self, issue_number: int) -> TaskMetadata | None:
        if issue_number in self._cache:
            return self._cache[issue_number]
        try:
            issue = self._forge.get_issue(issue_number)
            if issue is not None:
                parsed = parse_task_from_issue(issue)
                self._cache[issue_number] = parsed
                return parsed
        except Exception:
            pass
        return None


def _resolve_owner_token(request: ClaimRequest) -> tuple[ClaimRequest, str]:
    """Ensure the claim request carries a valid owner token, generating one if needed."""
    if request.owner_token and request.owner_token.strip():
        return request, request.owner_token
    token = new_owner_token().value
    return dataclasses.replace(request, owner_token=token), token


def _conflict_to_outcome(issue_number: int, conflict: ClaimConflict) -> ClaimOutcome:
    """Format a conflict decision into a structured rejection outcome."""
    reason = ClaimFailureReason.CLAIM_CONFLICT
    if conflict.reason == ClaimConflictReason.SAME_ISSUE:
        reason = ClaimFailureReason.EXISTING_CLAIM_UNRECOVERED
    msg = f"Claim conflict with issue #{conflict.active.issue_number}: {conflict.reason.value}"
    failure = ClaimFailure(
        reason=reason,
        message=msg,
        conflicting_issue_number=conflict.active.issue_number,
        conflicting_branch=conflict.active.branch or None,
        conflicting_path=Path(conflict.active.worktree_path)
        if conflict.active.worktree_path
        else None,
    )
    return ClaimOutcome(success=False, issue_number=issue_number, failure=failure)


def _validate_preflight_and_conflict(
    request: ClaimRequest,
    run_state: RunState,
    forge: Forge,
    view: Any,
    default_base: str,
) -> tuple[PreflightDecision | None, IssueRecord | None, ClaimOutcome | None]:
    """Execute preflight and conflict checks in the current run_state snapshot."""
    issue = forge.get_issue(request.issue_number)
    if issue is None:
        failure = ClaimFailure(
            reason=ClaimFailureReason.ISSUE_NOT_FOUND,
            message=f"Issue #{request.issue_number} was not found.",
        )
        return (
            None,
            None,
            ClaimOutcome(
                success=False, issue_number=request.issue_number, failure=failure
            ),
        )

    preflight_view = view if isinstance(view, ClaimBaseResolutionView) else None
    preflight = evaluate_claim_preflight(
        issue,
        labels=issue.labels,
        view=preflight_view,
        default_base=default_base,
    )
    if not preflight.allowed:
        return (
            None,
            issue,
            ClaimOutcome(
                success=False,
                issue_number=request.issue_number,
                failure=preflight.failure,
            ),
        )

    task_meta = parse_task_from_issue(issue)
    reservation = build_reservation(request, task_meta)
    conflict_view = (
        view
        if (view is not None and hasattr(view, "task"))
        else _DefaultConflictView(forge)
    )
    conflict = evaluate_claim_conflicts(reservation, run_state, conflict_view)
    if conflict is not None:
        return None, issue, _conflict_to_outcome(request.issue_number, conflict)

    return preflight, issue, None


def _perform_git_fetch(repo_root: Path, issue_number: int) -> ClaimFailure | None:
    """Execute git fetch origin before making local modifications."""
    try:
        res = run_git(["fetch", "origin"], cwd=repo_root, check=False)
        if res.returncode != 0:
            err = res.stderr or res.stdout or "unknown fetch failure"
            return ClaimFailure(
                reason=ClaimFailureReason.GIT_FETCH_FAILED,
                message=f"git fetch origin failed: {err.strip()}",
            )
    except Exception as e:
        return ClaimFailure(
            reason=ClaimFailureReason.GIT_FETCH_FAILED,
            message=f"git fetch origin raised exception: {e}",
        )
    return None


def _prepare_and_persist_worktree(
    workspace: ClaimWorkspace,
    reservation: ActiveWorktree,
    canonical_branch: str,
    base_ref: str,
    run_state: RunState,
    raw_token: str,
) -> ClaimOutcome | None:
    """Create the worktree and update reservation to WORKTREE_PREPARED/ACTIVE_SAVED."""
    try:
        prep = prepare_task_worktree(
            canonical_branch,
            workspace.worktree_root,
            base_ref,
            reservation.claim_id or "",
            allow_force=False,
            cwd=workspace.repository_root,
        )
    except Exception as e:
        return ClaimOutcome(
            success=False,
            issue_number=reservation.issue_number,
            claim_id=reservation.claim_id,
            branch=canonical_branch,
            stage=ClaimStage.RESERVED,
            failure=ClaimFailure(
                reason=ClaimFailureReason.WORKTREE_CREATION_FAILED,
                message=f"prepare_task_worktree raised exception: {e}",
            ),
            owner_token=raw_token,
        )

    if not prep.accepted:
        return ClaimOutcome(
            success=False,
            issue_number=reservation.issue_number,
            claim_id=reservation.claim_id,
            branch=canonical_branch,
            stage=ClaimStage.RESERVED,
            failure=ClaimFailure(
                reason=ClaimFailureReason.WORKTREE_CREATION_FAILED,
                message=f"prepare_task_worktree rejected: {prep.rejection_reason}",
            ),
            owner_token=raw_token,
        )

    reservation.worktree_path = str(prep.worktree_path)
    reservation.base_sha = prep.base_sha
    reservation.claim_stage = ClaimStage.ACTIVE_SAVED.value
    save_run_state(run_state, workspace.run_state_path)
    return None


def _apply_status_label(
    forge: Forge, issue_number: int, labels: Sequence[str]
) -> ClaimFailure | None:
    """Transition status:in-progress label, returning a ClaimFailure on error."""
    old_labels = [
        lbl
        for lbl in labels
        if lbl.startswith(STATUS_LABEL_PREFIX) and lbl != StatusLabel.IN_PROGRESS
    ]
    try:
        transition_status_label(
            forge,
            issue_number,
            StatusLabel.IN_PROGRESS,
            old_labels,
        )
        return None
    except Exception as e:
        return ClaimFailure(
            reason=ClaimFailureReason.LABEL_UPDATE_FAILED,
            message=f"Failed to transition label: {e}",
        )


def _build_success_outcome(
    reservation: ActiveWorktree,
    raw_token: str,
    owner_kind: OwnerKind,
    reservation_kind: ReservationKind,
) -> ClaimOutcome:
    """Construct a completed ClaimOutcome from the finalized reservation."""
    return ClaimOutcome(
        success=True,
        issue_number=reservation.issue_number,
        claim_id=reservation.claim_id,
        branch=reservation.branch,
        worktree_path=Path(reservation.worktree_path)
        if reservation.worktree_path
        else None,
        base_ref=reservation.base_ref,
        owner_kind=owner_kind,
        reservation_kind=reservation_kind,
        stage=ClaimStage.COMPLETED,
        owner_token=raw_token,
    )


def _transition_labels_and_finalize(
    workspace: ClaimWorkspace,
    reservation: ActiveWorktree,
    run_state: RunState,
    forge: Forge,
    issue: IssueRecord,
    raw_token: str,
    owner_kind: OwnerKind,
    reservation_kind: ReservationKind,
) -> ClaimOutcome:
    """Transition status:in-progress label and mark claim stage as COMPLETED."""
    label_failure = _apply_status_label(forge, reservation.issue_number, issue.labels)
    if label_failure is not None:
        return ClaimOutcome(
            success=False,
            issue_number=reservation.issue_number,
            claim_id=reservation.claim_id,
            branch=reservation.branch,
            worktree_path=Path(reservation.worktree_path)
            if reservation.worktree_path
            else None,
            stage=ClaimStage.ACTIVE_SAVED,
            failure=label_failure,
            owner_token=raw_token,
        )

    reservation.claim_stage = ClaimStage.COMPLETED.value
    save_run_state(run_state, workspace.run_state_path)
    return _build_success_outcome(reservation, raw_token, owner_kind, reservation_kind)


def _initialize_reservation(
    request: ClaimRequest,
    issue: IssueRecord,
    subtask_id: str | None,
    base_ref: str,
    repository_identity: str,
) -> ActiveWorktree:
    """Build and populate the initial reservation record."""
    task_meta = parse_task_from_issue(issue)
    reservation = build_reservation(request, task_meta)
    reservation.branch = build_task_branch_name(issue.number, subtask_id)
    reservation.base_branch = base_ref
    reservation.base_ref = base_ref
    reservation.claim_stage = ClaimStage.RESERVED.value
    reservation.claimed_at = time.time()
    reservation.repository_id = repository_identity
    return reservation


def _handle_request_resume(
    request: ClaimRequest,
    raw_token: str,
    workspace: ClaimWorkspace,
    run_state: RunState,
    forge: Forge,
) -> ClaimOutcome | None:
    """Handle explicit resume via resume_claim_id in ClaimRequest if provided."""
    if not request.resume_claim_id:
        return None
    matching = [
        a
        for a in run_state.active_worktrees.values()
        if a.claim_id == request.resume_claim_id
    ]
    if not matching:
        return ClaimOutcome(
            success=False,
            issue_number=request.issue_number,
            failure=ClaimFailure(
                reason=ClaimFailureReason.INVALID_RESUME,
                message=f"No active claim found with ID {request.resume_claim_id}",
            ),
        )
    active = matching[0]
    if active.owner_token_digest != owner_token_digest(raw_token):
        return ClaimOutcome(
            success=False,
            issue_number=active.issue_number,
            failure=ClaimFailure(
                reason=ClaimFailureReason.INVALID_RESUME,
                message="Owner token does not match active claim.",
            ),
        )
    return _resume_from_active(active, workspace, run_state, forge, raw_token)


def _build_dry_run_outcome(
    issue: IssueRecord,
    preflight: PreflightDecision,
    canonical_branch: str,
    base_ref: str,
    request: ClaimRequest,
    raw_token: str,
) -> ClaimOutcome:
    """Construct a non-mutating preview outcome for dry-run evaluations."""
    return ClaimOutcome(
        success=True,
        issue_number=issue.number,
        claim_id=f"dryrun-{issue.number}",
        branch=canonical_branch,
        base_ref=base_ref,
        owner_kind=request.owner_kind,
        reservation_kind=preflight.reservation_kind,
        stage=ClaimStage.VALIDATING,
        owner_token=raw_token,
    )


def _apply_claim_side_effects(
    workspace: ClaimWorkspace,
    request: ClaimRequest,
    issue: IssueRecord,
    preflight: PreflightDecision,
    canonical_branch: str,
    base_ref: str,
    run_state: RunState,
    forge: Forge,
    raw_token: str,
) -> ClaimOutcome:
    """Apply Git fetch, reservation persistence, worktree creation, and labeling."""
    fetch_failure = _perform_git_fetch(workspace.repository_root, issue.number)
    if fetch_failure is not None:
        return ClaimOutcome(
            success=False, issue_number=issue.number, failure=fetch_failure
        )

    reservation = _initialize_reservation(
        request, issue, preflight.subtask_id, base_ref, workspace.repository_identity
    )
    run_state.active_worktrees[str(issue.number)] = reservation
    save_run_state(run_state, workspace.run_state_path)

    prep_error = _prepare_and_persist_worktree(
        workspace, reservation, canonical_branch, base_ref, run_state, raw_token
    )
    if prep_error is not None:
        return prep_error

    return _transition_labels_and_finalize(
        workspace,
        reservation,
        run_state,
        forge,
        issue,
        raw_token,
        request.owner_kind,
        preflight.reservation_kind,
    )


def _execute_claim_in_lock(
    request: ClaimRequest,
    raw_token: str,
    workspace: ClaimWorkspace,
    forge: Forge,
    apply: bool,
    view: Any,
    default_base: str,
) -> ClaimOutcome:
    """State-locked claim execution sequencing §5 lifecycle steps."""
    run_state = load_run_state(workspace.run_state_path)
    resume_outcome = _handle_request_resume(
        request, raw_token, workspace, run_state, forge
    )
    if resume_outcome is not None:
        return resume_outcome

    preflight, issue, error_outcome = _validate_preflight_and_conflict(
        request, run_state, forge, view, default_base
    )
    if error_outcome is not None or preflight is None or issue is None:
        assert error_outcome is not None
        return error_outcome

    canonical_branch = build_task_branch_name(issue.number, preflight.subtask_id)
    base_ref = preflight.base_ref or default_base
    if not apply:
        return _build_dry_run_outcome(
            issue, preflight, canonical_branch, base_ref, request, raw_token
        )

    return _apply_claim_side_effects(
        workspace,
        request,
        issue,
        preflight,
        canonical_branch,
        base_ref,
        run_state,
        forge,
        raw_token,
    )


def claim_task(
    request: ClaimRequest,
    *,
    apply: bool = True,
    forge: Forge | None = None,
    cwd: str | Path | None = None,
    view: Any = None,
    default_base: str = "origin/main",
) -> ClaimOutcome:
    """Execute the sequential lifecycle to claim an issue."""
    effective_apply = apply and not request.dry_run
    effective_request, raw_token = _resolve_owner_token(request)
    workspace = resolve_claim_workspace(cwd, explicit_state_path=request.state_path)
    active_forge = forge or GitHubForge()

    timeout = request.timeout_seconds if request.timeout_seconds is not None else 0.0
    try:
        with run_state_lock(workspace.lock_path, timeout=timeout):
            return _execute_claim_in_lock(
                effective_request,
                raw_token,
                workspace,
                active_forge,
                effective_apply,
                view,
                default_base,
            )
    except (FileLockContentionError, RuntimeError) as e:
        return ClaimOutcome(
            success=False,
            issue_number=request.issue_number,
            failure=ClaimFailure(
                reason=ClaimFailureReason.STATE_LOCK_FAILED,
                message=f"Could not acquire run_state lock: {e}",
            ),
        )


def _check_resume_identity(
    workspace: ClaimWorkspace, active: ActiveWorktree, owner_token: str
) -> ClaimOutcome | None:
    """Validate that the active reservation belongs to the current repository."""
    if not check_repository_identity_match(
        workspace.repository_identity, active.repository_id
    ):
        return ClaimOutcome(
            success=False,
            issue_number=active.issue_number,
            failure=ClaimFailure(
                reason=ClaimFailureReason.INVALID_RESUME,
                message=(
                    f"Repository identity mismatch for claim {active.claim_id}: "
                    f"expected '{workspace.repository_identity}', got '{active.repository_id}'"
                ),
            ),
            owner_token=owner_token,
        )
    return None


def _resume_from_active(
    active: ActiveWorktree,
    workspace: ClaimWorkspace,
    run_state: RunState,
    forge: Forge,
    owner_token: str,
) -> ClaimOutcome:
    """Resume an active claim from its current progression stage."""
    identity_error = _check_resume_identity(workspace, active, owner_token)
    if identity_error is not None:
        return identity_error

    owner_kind = OwnerKind(active.owner_kind)
    reservation_kind = ReservationKind(active.reservation_kind)

    if active.claim_stage == ClaimStage.COMPLETED.value:
        return _build_success_outcome(active, owner_token, owner_kind, reservation_kind)

    if active.claim_stage == ClaimStage.RESERVED.value:
        prep_error = _prepare_and_persist_worktree(
            workspace,
            active,
            active.branch,
            active.base_ref or "origin/main",
            run_state,
            owner_token,
        )
        if prep_error is not None:
            return prep_error

    issue = forge.get_issue(active.issue_number)
    if issue is None:
        return ClaimOutcome(
            success=False,
            issue_number=active.issue_number,
            failure=ClaimFailure(
                reason=ClaimFailureReason.ISSUE_NOT_FOUND,
                message=f"Issue #{active.issue_number} was not found on resume.",
            ),
            owner_token=owner_token,
        )

    return _transition_labels_and_finalize(
        workspace,
        active,
        run_state,
        forge,
        issue,
        owner_token,
        owner_kind,
        reservation_kind,
    )


def _validate_resume_active(
    claim_id: str,
    owner_token: str,
    run_state: RunState,
) -> tuple[ActiveWorktree | None, ClaimOutcome | None]:
    """Find active reservation matching claim_id and authenticate owner token."""
    matching = [
        a for a in run_state.active_worktrees.values() if a.claim_id == claim_id
    ]
    if not matching:
        return None, ClaimOutcome(
            success=False,
            issue_number=0,
            failure=ClaimFailure(
                reason=ClaimFailureReason.INVALID_RESUME,
                message=f"No active claim found with ID {claim_id}",
            ),
        )

    active = matching[0]
    expected_digest = owner_token_digest(owner_token)
    if active.owner_token_digest != expected_digest:
        return None, ClaimOutcome(
            success=False,
            issue_number=active.issue_number,
            failure=ClaimFailure(
                reason=ClaimFailureReason.INVALID_RESUME,
                message="Owner token does not match active claim.",
            ),
        )
    return active, None


def _normalize_owner_token(owner_token: str | OwnerToken) -> str | None:
    """Extract stripped string representation of owner token, or None if empty."""
    raw = owner_token.value if isinstance(owner_token, OwnerToken) else str(owner_token)
    return raw if raw.strip() else None


def resume_claim(
    claim_id: str,
    owner_token: str | OwnerToken,
    *,
    forge: Forge | None = None,
    cwd: str | Path | None = None,
    state_path: str | Path | None = None,
    view: Any = None,
    default_base: str = "origin/main",
    timeout_seconds: float | None = None,
) -> ClaimOutcome:
    """Resume an interrupted claim session matching a claim_id and owner token."""
    raw_token = _normalize_owner_token(owner_token)
    if raw_token is None:
        return ClaimOutcome(
            success=False,
            issue_number=0,
            failure=ClaimFailure(
                reason=ClaimFailureReason.INVALID_RESUME,
                message="Owner token must not be empty.",
            ),
        )

    workspace = resolve_claim_workspace(cwd, explicit_state_path=state_path)
    active_forge = forge or GitHubForge()
    timeout = timeout_seconds if timeout_seconds is not None else 0.0

    try:
        with run_state_lock(workspace.lock_path, timeout=timeout):
            run_state = load_run_state(workspace.run_state_path)
            active, auth_error = _validate_resume_active(claim_id, raw_token, run_state)
            if auth_error is not None or active is None:
                assert auth_error is not None
                return auth_error
            return _resume_from_active(
                active, workspace, run_state, active_forge, raw_token
            )
    except (FileLockContentionError, RuntimeError) as e:
        return ClaimOutcome(
            success=False,
            issue_number=0,
            failure=ClaimFailure(
                reason=ClaimFailureReason.STATE_LOCK_FAILED,
                message=f"Could not acquire run_state lock: {e}",
            ),
        )


__all__ = ["claim_task", "resume_claim"]
