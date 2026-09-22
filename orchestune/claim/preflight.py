"""Preflight validation and base resolution for task claim operations."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import yaml

from orchestune.claim.contracts import (
    ClaimFailure,
    ClaimFailureReason,
    ReservationKind,
)
from orchestune.dispatch.dependency_assessment import (
    DependencyAssessment,
    DependencyState,
)
from orchestune.dispatch.dependency_policy import (
    DependencyPolicyView,
    decide_stack_target,
    has_pending_dependencies,
)
from orchestune.dispatch.labels import (
    PRIMARY_STATUS_LABELS,
    TERMINAL_ESCALATION_LABELS,
)
from orchestune.dispatch.locks import (
    KIND_BRANCH,
    KIND_PR,
    ExternalLockConflict,
    ExternalLockScanResult,
)
from orchestune.issue_parsing import (
    FOOTPRINT_BLOCK_PATTERN,
    effective_parent_number,
)
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord


@runtime_checkable
class ClaimBaseResolutionView(DependencyPolicyView, Protocol):
    """Protocol for resolving stack target, dependencies, and parent issue bases."""

    default_base: str

    def assess_dependencies(self, issue_number: int) -> DependencyAssessment | None: ...

    def canonical_branch(self, issue_number: int) -> str | None: ...

    def parent_issue_number(self, issue_number: int) -> int | None: ...


@dataclass(frozen=True)
class ClaimBaseDecision:
    """Outcome of resolving the base branch and kind for a claim attempt."""

    allowed: bool
    base_ref: str | None = None
    kind: str | None = None  # "stack" | "normal"
    target_issue_number: int | None = None
    failure: ClaimFailure | None = None


@dataclass(frozen=True)
class PreflightDecision:
    """Evaluation result of preflight claim requirements."""

    allowed: bool
    issue_number: int
    subtask_id: str | None = None
    base_ref: str | None = None
    reservation_kind: ReservationKind = ReservationKind.FOOTPRINT
    failure: ClaimFailure | None = None
    stack_target_issue_number: int | None = None


def resolve_claim_subtask_id(issue: IssueRecord) -> str:
    """Resolve a stable subtask identifier for branch naming and tracking.

    Prioritizes subtask_id declared in Footprint YAML. If absent, empty,
    or if the YAML block is missing/malformed, generates a stable ID
    derived from the issue number (independent of issue title changes).
    """
    fallback_id = f"task-{issue.number}"
    match = FOOTPRINT_BLOCK_PATTERN.search(issue.body)
    if not match:
        return fallback_id

    try:
        data = yaml.safe_load(match.group(1))
        if isinstance(data, dict):
            subtask_id = str(data.get("subtask_id") or "").strip()
            if subtask_id:
                return subtask_id
    except yaml.YAMLError:
        pass

    return fallback_id


def resolve_claim_base(
    issue_number: int,
    view: ClaimBaseResolutionView,
    *,
    default_base: str = "origin/main",
) -> ClaimBaseDecision:
    """Resolve base branch and stack/normal mode, rejecting unresolvable dependencies."""
    decision = decide_stack_target(issue_number, view)

    if decision.target is not None:
        return ClaimBaseDecision(
            allowed=True,
            base_ref=decision.target.branch,
            kind="stack",
            target_issue_number=decision.target.issue_number,
        )

    if decision.reason == "no-stack-dependency":
        parent_num = view.parent_issue_number(issue_number)
        base_ref = (
            f"parent/issue-{parent_num}"
            if parent_num is not None
            else getattr(view, "default_base", default_base) or default_base
        )
        return ClaimBaseDecision(allowed=True, base_ref=base_ref, kind="normal")

    return ClaimBaseDecision(
        allowed=False,
        failure=ClaimFailure(
            reason=ClaimFailureReason.UNRESOLVED_DEPENDENCIES,
            message=f"Dependency policy rejected claim for issue #{issue_number}: {decision.reason}",
            conflicting_issue_number=decision.blocking_issue_number,
        ),
    )


def _check_external_lock_conflicts(
    issue_number: int,
    labels: set[str],
    external_locks: ExternalLockScanResult | None,
) -> ClaimFailure | None:
    conflicts: tuple[ExternalLockConflict, ...] = ()
    if external_locks and issue_number in external_locks.conflicts:
        conflicts = external_locks.conflicts[issue_number]

    if StatusLabel.EXTERNAL_LOCK in labels or conflicts:
        conflicting_branch: str | None = None
        conflicting_issue: int | None = None
        reasons: list[str] = []

        for conflict in conflicts:
            reasons.append(f"{conflict.kind}:{conflict.source}")
            if conflict.kind == KIND_BRANCH and conflicting_branch is None:
                conflicting_branch = conflict.source
            elif (
                conflict.kind == KIND_PR
                and conflicting_issue is None
                and conflict.source.startswith("#")
            ):
                try:
                    conflicting_issue = int(conflict.source[1:])
                except ValueError:
                    pass

        msg = f"Issue #{issue_number} is locked against external branches/PRs."
        if reasons:
            msg += f" (conflicts: {', '.join(reasons)})"

        return ClaimFailure(
            reason=ClaimFailureReason.EXTERNAL_LOCK_CONFLICT,
            message=msg,
            conflicting_branch=conflicting_branch,
            conflicting_issue_number=conflicting_issue,
        )

    return None


def _check_status_labels(
    issue_number: int,
    label_set: set[str],
) -> ClaimFailure | None:
    terminal = [lbl for lbl in label_set if lbl in TERMINAL_ESCALATION_LABELS]
    if terminal:
        return ClaimFailure(
            reason=ClaimFailureReason.TERMINAL_ESCALATION,
            message=f"Issue #{issue_number} has terminal escalation: {', '.join(terminal)}.",
        )

    if StatusLabel.IN_PROGRESS in label_set:
        return ClaimFailure(
            reason=ClaimFailureReason.ALREADY_IN_PROGRESS,
            message=f"Issue #{issue_number} is already in progress.",
        )

    primary_present = [lbl for lbl in label_set if lbl in PRIMARY_STATUS_LABELS]
    if len(primary_present) > 1:
        return ClaimFailure(
            reason=ClaimFailureReason.CLAIM_CONFLICT,
            message=f"Issue #{issue_number} has conflicting primary status labels: {', '.join(primary_present)}.",
        )

    if StatusLabel.DONE in label_set or StatusLabel.NOT_NEEDED in label_set:
        return ClaimFailure(
            reason=ClaimFailureReason.CLAIM_CONFLICT,
            message=f"Issue #{issue_number} is already marked done/not-needed.",
        )

    return None


def _resolve_reservation_kind(issue: IssueRecord) -> ReservationKind:
    match = FOOTPRINT_BLOCK_PATTERN.search(issue.body)
    if match:
        try:
            data = yaml.safe_load(match.group(1))
            if isinstance(data, dict) and bool(data.get("footprint")):
                return ReservationKind.FOOTPRINT
        except yaml.YAMLError:
            pass
    return ReservationKind.REPOSITORY


def _check_dependencies_without_view(
    issue_number: int,
    label_set: set[str],
    assessment: DependencyAssessment | None,
) -> ClaimFailure | None:
    if StatusLabel.BLOCKED in label_set:
        return ClaimFailure(
            reason=ClaimFailureReason.UNRESOLVED_DEPENDENCIES,
            message=f"Issue #{issue_number} is status:blocked but no resolution view is provided to verify stack eligibility.",
        )

    if assessment is not None and has_pending_dependencies(assessment):
        unresolved = [
            d.issue_number
            for d in assessment.resolved
            if d.state is not DependencyState.COMPLETED
        ]
        return ClaimFailure(
            reason=ClaimFailureReason.UNRESOLVED_DEPENDENCIES,
            message=f"Issue #{issue_number} has pending dependencies.",
            conflicting_issue_number=unresolved[0] if unresolved else None,
        )
    return None


def _resolve_base_without_view(
    default_base: str,
    parent_issue_number: int | None,
) -> str:
    if default_base != "origin/main":
        return default_base
    if parent_issue_number is not None:
        return f"parent/issue-{parent_issue_number}"
    return default_base


def _resolve_dependencies_and_base(
    issue_number: int,
    label_set: set[str],
    assessment: DependencyAssessment | None,
    view: ClaimBaseResolutionView | None,
    default_base: str,
    *,
    parent_issue_number: int | None = None,
) -> tuple[ClaimFailure | None, str | None, int | None]:
    if view is not None:
        base_decision = resolve_claim_base(
            issue_number, view, default_base=default_base
        )
        if not base_decision.allowed:
            return base_decision.failure, None, None

        if StatusLabel.BLOCKED in label_set and base_decision.kind != "stack":
            return (
                ClaimFailure(
                    reason=ClaimFailureReason.UNRESOLVED_DEPENDENCIES,
                    message=f"Issue #{issue_number} is status:blocked but not stack-eligible.",
                ),
                None,
                None,
            )
        return None, base_decision.base_ref, base_decision.target_issue_number

    failure = _check_dependencies_without_view(issue_number, label_set, assessment)
    if failure is not None:
        return failure, None, None

    base_ref = _resolve_base_without_view(default_base, parent_issue_number)
    return None, base_ref, None


def _validate_issue_and_labels(
    issue: IssueRecord | None,
    labels: Sequence[str] | Iterable[str] | None,
    external_locks: ExternalLockScanResult | None,
) -> tuple[int, set[str], ClaimFailure | None]:
    if issue is None:
        return (
            0,
            set(),
            ClaimFailure(
                reason=ClaimFailureReason.ISSUE_NOT_FOUND,
                message="Issue was not found.",
            ),
        )
    if issue.state.upper() != "OPEN":
        return (
            issue.number,
            set(),
            ClaimFailure(
                reason=ClaimFailureReason.ISSUE_CLOSED,
                message=f"Issue #{issue.number} is closed.",
            ),
        )

    label_set = set(labels if labels is not None else issue.labels)
    failure = _check_status_labels(issue.number, label_set)
    if failure is None:
        failure = _check_external_lock_conflicts(
            issue.number, label_set, external_locks
        )
    return issue.number, label_set, failure


def evaluate_claim_preflight(
    issue: IssueRecord | None,
    labels: Sequence[str] | Iterable[str] | None = None,
    assessment: DependencyAssessment | None = None,
    external_locks: ExternalLockScanResult | None = None,
    *,
    view: ClaimBaseResolutionView | None = None,
    default_base: str = "origin/main",
) -> PreflightDecision:
    """Validate issue status, labels, and dependencies before attempting claim."""
    issue_num, label_set, failure = _validate_issue_and_labels(
        issue, labels, external_locks
    )
    if failure is not None or issue is None:
        return PreflightDecision(allowed=False, issue_number=issue_num, failure=failure)

    reservation_kind = _resolve_reservation_kind(issue)
    subtask_id = resolve_claim_subtask_id(issue)
    parent_number = effective_parent_number(issue)

    dep_failure, base_ref, stack_target = _resolve_dependencies_and_base(
        issue.number,
        label_set,
        assessment,
        view,
        default_base,
        parent_issue_number=parent_number,
    )
    if dep_failure is not None:
        return PreflightDecision(
            allowed=False,
            issue_number=issue.number,
            subtask_id=subtask_id,
            reservation_kind=reservation_kind,
            failure=dep_failure,
        )

    return PreflightDecision(
        allowed=True,
        issue_number=issue.number,
        subtask_id=subtask_id,
        base_ref=base_ref,
        reservation_kind=reservation_kind,
        stack_target_issue_number=stack_target,
    )


__all__ = [
    "ClaimBaseDecision",
    "ClaimBaseResolutionView",
    "PreflightDecision",
    "evaluate_claim_preflight",
    "resolve_claim_base",
    "resolve_claim_subtask_id",
]
