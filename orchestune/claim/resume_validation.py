"""Issue validation shared by claim finalization and resume."""

from __future__ import annotations

from typing import Any

from orchestune.claim.contracts import (
    ClaimFailure,
    ClaimFailureReason,
    ClaimOutcome,
    ClaimStage,
)
from orchestune.claim.preflight import ClaimBaseResolutionView, resolve_claim_base
from orchestune.forge import Forge
from orchestune.labels import StatusLabel
from orchestune.ledger.run_state import ActiveWorktree
from orchestune.ledger.status_labels import TERMINAL_ESCALATION_LABELS
from orchestune.models import IssueRecord


def _preflight_labels(issue: IssueRecord, adoption: bool) -> tuple[str, ...]:
    return tuple(
        label
        for label in issue.labels
        if not (adoption and label == StatusLabel.BLOCKED_HUMAN_REVIEW)
    )


def _validate_blocked_dependency(
    issue_number: int,
    view: Any,
    default_base: str,
) -> ClaimFailure | None:
    """Verify that a status:blocked issue remains stack-eligible."""
    preflight_view = view if isinstance(view, ClaimBaseResolutionView) else None
    if preflight_view is not None:
        base_decision = resolve_claim_base(
            issue_number, preflight_view, default_base=default_base
        )
        if not base_decision.allowed:
            return base_decision.failure
        if base_decision.kind != "stack":
            return ClaimFailure(
                reason=ClaimFailureReason.UNRESOLVED_DEPENDENCIES,
                message=f"Issue #{issue_number} is status:blocked but not stack-eligible.",
            )
        return None
    return ClaimFailure(
        reason=ClaimFailureReason.UNRESOLVED_DEPENDENCIES,
        message=f"Issue #{issue_number} is status:blocked but no resolution view is provided to verify stack eligibility.",
    )


def _validate_issue_for_resume(
    issue: IssueRecord,
    view: Any = None,
    default_base: str = "origin/main",
    *,
    adopted: bool = False,
) -> ClaimFailure | None:
    """Revalidate issue state (open, non-terminal, no external lock, dependencies) on resume/finalize."""
    if issue.state.upper() != "OPEN":
        return ClaimFailure(
            reason=ClaimFailureReason.ISSUE_CLOSED,
            message=f"Issue #{issue.number} is closed.",
        )
    label_set = set(issue.labels)
    statuses = {lbl for lbl in label_set if lbl.startswith("status:")}
    if adopted and (
        not statuses
        or not statuses
        <= {
            StatusLabel.BLOCKED_HUMAN_REVIEW,
            StatusLabel.IN_PROGRESS,
        }
    ):
        return ClaimFailure(
            reason=ClaimFailureReason.CLAIM_CONFLICT,
            message="Adopted claim status changed during finalization",
        )
    terminal = [
        lbl
        for lbl in label_set
        if lbl in TERMINAL_ESCALATION_LABELS
        and not (adopted and lbl == StatusLabel.BLOCKED_HUMAN_REVIEW)
    ]
    if terminal:
        return ClaimFailure(
            reason=ClaimFailureReason.TERMINAL_ESCALATION,
            message=f"Issue #{issue.number} has terminal escalation: {', '.join(terminal)}.",
        )
    if StatusLabel.DONE in label_set or StatusLabel.NOT_NEEDED in label_set:
        return ClaimFailure(
            reason=ClaimFailureReason.CLAIM_CONFLICT,
            message=f"Issue #{issue.number} is already marked done/not-needed.",
        )
    if StatusLabel.EXTERNAL_LOCK in label_set:
        return ClaimFailure(
            reason=ClaimFailureReason.EXTERNAL_LOCK_CONFLICT,
            message=f"Issue #{issue.number} is locked against external branches/PRs.",
        )
    if StatusLabel.BLOCKED in label_set:
        return _validate_blocked_dependency(issue.number, view, default_base)
    return None


def _fetch_and_validate_resume_issue(
    forge: Forge,
    active: ActiveWorktree,
    owner_token: str,
    view: Any = None,
    default_base: str = "origin/main",
) -> tuple[IssueRecord | None, ClaimOutcome | None]:
    """Fetch issue and validate it is eligible for resume."""
    issue = forge.get_issue(active.core.issue_number)
    if issue is None:
        return None, ClaimOutcome(
            success=False,
            issue_number=active.core.issue_number,
            failure=ClaimFailure(
                reason=ClaimFailureReason.ISSUE_NOT_FOUND,
                message=f"Issue #{active.core.issue_number} was not found on resume.",
            ),
            owner_token=owner_token,
        )

    resume_issue_error = _validate_issue_for_resume(
        issue,
        view=view,
        default_base=default_base,
        adopted=active.claim.adopted_pr_number is not None,
    )
    if resume_issue_error is not None:
        return None, ClaimOutcome(
            success=False,
            issue_number=active.core.issue_number,
            claim_id=active.claim.claim_id,
            branch=active.core.branch,
            stage=ClaimStage(active.claim.claim_stage),
            failure=resume_issue_error,
            owner_token=owner_token,
        )
    return issue, None
