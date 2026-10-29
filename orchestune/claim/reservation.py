"""Build initial claim reservations and read-only previews."""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path

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
from orchestune.claim.ownership import build_reservation, with_claim
from orchestune.claim.preflight import PreflightDecision
from orchestune.issue_parsing import parse_task_from_issue
from orchestune.ledger.active_records import ActiveCompletionJournal
from orchestune.ledger.run_state import ActiveWorktree, RunState
from orchestune.models import IssueRecord
from orchestune.targets.completion_policy import snapshot_publication_policy


def _claim_failure(issue_number: int, failure: ClaimFailure) -> ClaimOutcome:
    return ClaimOutcome(success=False, issue_number=issue_number, failure=failure)


def _resume_preview(
    active: ActiveWorktree, request: ClaimRequest, default_base: str, token: str
) -> ClaimOutcome:
    return ClaimOutcome(
        success=True,
        issue_number=active.core.issue_number,
        claim_id=active.claim.claim_id,
        branch=active.core.branch,
        base_ref=active.claim.base_ref or default_base,
        owner_kind=request.owner_kind,
        reservation_kind=ReservationKind(active.claim.reservation_kind),
        stage=ClaimStage(active.claim.claim_stage),
        owner_token=token,
    )


def _initialize_reservation(
    request: ClaimRequest,
    issue: IssueRecord,
    subtask_id: str | None,
    base_ref: str,
    repository_identity: str,
    adopted_head: str | None = None,
) -> ActiveWorktree:
    """Build and populate the initial reservation record."""
    task_meta = parse_task_from_issue(issue)
    reservation = build_reservation(request, task_meta)
    reservation = reservation.with_core(
        dataclasses.replace(
            reservation.core,
            branch=build_task_branch_name(issue.number, subtask_id),
            base_branch=base_ref,
        )
    )
    reservation = with_claim(
        reservation,
        dataclasses.replace(
            reservation.claim,
            base_ref=base_ref,
            claimed_at=time.time(),
            repository_id=repository_identity,
            adopted_pr_number=request.adopt_pr if adopted_head else None,
            adopted_pr_head_sha=adopted_head,
        ),
    )
    return ActiveWorktree.from_records(
        core=reservation.core,
        launch=reservation.launch,
        claim=reservation.claim,
        completion=ActiveCompletionJournal(
            completion_policy_config=snapshot_publication_policy(Path.cwd())
        ),
    )


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


def _make_active_saved_failure(
    reservation: ActiveWorktree,
    failure: ClaimFailure,
    raw_token: str,
) -> ClaimOutcome:
    """Construct a standardized active-saved failure outcome with owner token."""
    return ClaimOutcome(
        success=False,
        issue_number=reservation.core.issue_number,
        claim_id=reservation.claim.claim_id,
        branch=reservation.core.branch,
        worktree_path=Path(reservation.core.worktree_path)
        if reservation.core.worktree_path
        else None,
        stage=ClaimStage.ACTIVE_SAVED,
        failure=failure,
        owner_token=raw_token,
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
        issue_number=reservation.core.issue_number,
        claim_id=reservation.claim.claim_id,
        branch=reservation.core.branch,
        worktree_path=Path(reservation.core.worktree_path)
        if reservation.core.worktree_path
        else None,
        base_ref=reservation.claim.base_ref,
        owner_kind=owner_kind,
        reservation_kind=reservation_kind,
        stage=ClaimStage.COMPLETED,
        owner_token=raw_token,
    )


def _validate_resume_active(
    claim_id: str,
    owner_token: str,
    run_state: RunState,
    fallback_issue_number: int = 0,
) -> tuple[ActiveWorktree | None, ClaimOutcome | None]:
    """Find active reservation matching claim_id and authenticate owner token."""
    matching = [
        a for a in run_state.active_worktrees.values() if a.claim.claim_id == claim_id
    ]
    if not matching:
        return None, _claim_failure(
            fallback_issue_number,
            ClaimFailure(
                reason=ClaimFailureReason.INVALID_RESUME,
                message=f"No active claim found with ID {claim_id}",
            ),
        )

    active = matching[0]
    if len(matching) != 1 or (
        fallback_issue_number and active.core.issue_number != fallback_issue_number
    ):
        return None, _claim_failure(
            active.core.issue_number,
            ClaimFailure(
                reason=ClaimFailureReason.INVALID_RESUME,
                message="Claim ID does not uniquely match the requested Issue.",
            ),
        )
    return active, None
