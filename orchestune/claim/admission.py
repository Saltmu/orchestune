"""Decide whether a claim request is admitted, and build its failure.

``service`` imports the helpers the claim and resume flow still calls.
This module does not import ``service``.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from orchestune.claim.contracts import (
    ClaimFailure,
    ClaimFailureReason,
    ClaimOutcome,
    ClaimOverlapWarning,
    ClaimRequest,
    ClaimStage,
)
from orchestune.claim.local_identity import validate_local_claim
from orchestune.claim.ownership import (
    ClaimConflict,
    ClaimConflictReason,
    assess_claim_conflicts,
    build_reservation,
    held_claim_next_actions,
)
from orchestune.claim.preflight import (
    ClaimBaseResolutionView,
    PreflightDecision,
    evaluate_claim_preflight,
)
from orchestune.claim.resume_validation import (
    _preflight_labels,
)
from orchestune.claim.workspace import (
    ClaimWorkspace,
    check_repository_identity_match,
)
from orchestune.forge import Forge, ForgeGraphQLUnavailableError
from orchestune.issue_parsing import parse_task_from_issue
from orchestune.ledger.run_state import (
    ActiveWorktree,
    RunState,
)
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
        issue = self._forge.get_issue(issue_number)
        if issue is not None:
            parsed = parse_task_from_issue(issue)
            self._cache[issue_number] = parsed
            return parsed
        return None


def _conflict_to_outcome(issue_number: int, conflict: ClaimConflict) -> ClaimOutcome:
    """Format a conflict decision into a structured rejection outcome."""
    reason = ClaimFailureReason.CLAIM_CONFLICT
    next_actions: tuple[str, ...] = ()
    if conflict.reason == ClaimConflictReason.SAME_ISSUE:
        reason = ClaimFailureReason.EXISTING_CLAIM_UNRECOVERED
        next_actions = held_claim_next_actions(conflict.active)
    msg = f"Claim conflict with issue #{conflict.active.core.issue_number}: {conflict.reason.value}"
    failure = ClaimFailure(
        reason=reason,
        message=msg,
        conflicting_issue_number=conflict.active.core.issue_number,
        conflicting_branch=conflict.active.core.branch or None,
        conflicting_path=Path(conflict.active.core.worktree_path)
        if conflict.active.core.worktree_path
        else None,
        next_actions=next_actions,
    )
    return ClaimOutcome(success=False, issue_number=issue_number, failure=failure)


def _evaluate_conflict_step(
    reservation: ActiveWorktree,
    run_state: RunState,
    conflict_view: Any,
    issue_number: int,
) -> tuple[ClaimOutcome | None, tuple[ClaimOverlapWarning, ...]]:
    """Evaluate conflict rules, returning a rejection outcome or non-blocking warnings."""
    try:
        assessment = assess_claim_conflicts(reservation, run_state, conflict_view)
    except ForgeGraphQLUnavailableError:
        # #1334: the CLI reports this as forge_unavailable, not as a conflict.
        raise
    except Exception as e:
        return _claim_failure(
            issue_number,
            ClaimFailure(
                reason=ClaimFailureReason.CLAIM_CONFLICT,
                message=f"Failed to evaluate claim conflicts due to metadata lookup failure: {e}",
            ),
        ), ()
    if assessment.conflict is not None:
        return _conflict_to_outcome(issue_number, assessment.conflict), ()
    return None, assessment.warnings


def _with_warnings(
    outcome: ClaimOutcome, warnings: tuple[ClaimOverlapWarning, ...]
) -> ClaimOutcome:
    return dataclasses.replace(outcome, warnings=warnings) if warnings else outcome


def _claim_failure(issue_number: int, failure: ClaimFailure | None) -> ClaimOutcome:
    return ClaimOutcome(success=False, issue_number=issue_number, failure=failure)


def _validate_preflight_and_conflict(
    request: ClaimRequest,
    run_state: RunState,
    forge: Forge,
    view: Any,
    default_base: str,
    *,
    adoption: bool = False,
) -> tuple[
    PreflightDecision | None,
    IssueRecord | None,
    ClaimOutcome | None,
    tuple[ClaimOverlapWarning, ...],
]:
    """Execute preflight and conflict checks in the current run_state snapshot."""
    issue = forge.get_issue(request.issue_number)
    if issue is None:
        failure = ClaimFailure(
            reason=ClaimFailureReason.ISSUE_NOT_FOUND,
            message=f"Issue #{request.issue_number} was not found.",
        )
        return None, None, _claim_failure(request.issue_number, failure), ()

    preflight = evaluate_claim_preflight(
        issue,
        labels=_preflight_labels(issue, adoption),
        view=view if isinstance(view, ClaimBaseResolutionView) else None,
        default_base=default_base,
        forge=forge,
    )
    if not preflight.allowed:
        return None, issue, _claim_failure(request.issue_number, preflight.failure), ()

    task_meta = preflight.task or parse_task_from_issue(issue)
    reservation = build_reservation(request, task_meta)
    conflict_view = (
        view
        if (view is not None and hasattr(view, "task"))
        else _DefaultConflictView(forge, cache={issue.number: task_meta})
    )
    conflict_outcome, warnings = _evaluate_conflict_step(
        reservation, run_state, conflict_view, request.issue_number
    )
    if conflict_outcome is not None:
        return None, issue, conflict_outcome, ()

    return preflight, issue, None, warnings


def _worktree_reservation_failure(
    reason: ClaimFailureReason,
    message: str,
    *,
    issue_number: int,
    claim_id: str | None,
    canonical_branch: str,
    raw_token: str,
) -> ClaimOutcome:
    return ClaimOutcome(
        success=False,
        issue_number=issue_number,
        claim_id=claim_id,
        branch=canonical_branch,
        stage=ClaimStage.RESERVED,
        failure=ClaimFailure(reason=reason, message=message),
        owner_token=raw_token,
    )


def _unfinished_completion_failure(issue_number: int) -> ClaimOutcome:
    return _claim_failure(
        issue_number,
        ClaimFailure(
            reason=ClaimFailureReason.EXISTING_CLAIM_UNRECOVERED,
            message="Issue has an unfinished or invalid completion reservation; resume complete first.",
        ),
    )


def _check_resume_identity(
    workspace: ClaimWorkspace, active: ActiveWorktree, owner_token: str
) -> ClaimOutcome | None:
    """Validate that the active reservation belongs to the current repository."""
    if not check_repository_identity_match(
        workspace.repository_identity, active.claim.repository_id
    ):
        return ClaimOutcome(
            success=False,
            issue_number=active.core.issue_number,
            failure=ClaimFailure(
                reason=ClaimFailureReason.INVALID_RESUME,
                message=(
                    f"Repository identity mismatch for claim {active.claim.claim_id}: "
                    f"expected '{workspace.repository_identity}', got '{active.claim.repository_id}'"
                ),
            ),
            owner_token=owner_token,
        )
    try:
        validate_local_claim(
            active,
            active.claim.claim_id,
            cwd=workspace.repository_root,
            state_path=workspace.run_state_path,
            allow_primary=True,
            allow_unprepared=True,
        )
    except ValueError as error:
        return _claim_failure(
            active.core.issue_number,
            ClaimFailure(
                reason=ClaimFailureReason.INVALID_RESUME,
                message=str(error),
            ),
        )
    return None
