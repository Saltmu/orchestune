"""Fail-closed evidence evaluation for PRs merged into a child Issue's parent.

This module deliberately does not reuse :func:`pr_matches_issue`: that helper
also accepts ordinary title/body mentions, which are useful for discovery but
are never authority to close an Issue.  Callers must supply only PR history
obtained from a successful, parent-base-scoped Forge query.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from orchestune.branch_naming import branch_matches_task, parse_task_branch_name
from orchestune.dispatch.labels import PRIMARY_STATUS_LABELS, transition_status_label
from orchestune.dispatch.scoring import Task
from orchestune.issue_parsing import effective_parent_number
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord, PrRecord
from orchestune.pr_link_notice import ensure_pr_merged_notice


class PriorParentMergeStatus(StrEnum):
    """The three outcomes required before a lifecycle mutation is considered."""

    ALREADY_MERGED = "already_merged"
    NOT_FOUND = "not_found"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class PriorParentMergeEvidence:
    status: PriorParentMergeStatus
    pr_number: int | None = None
    base_ref: str = ""
    merged_at: str = ""
    reason: str = ""


MergeReachabilityProbe = Callable[[str, str], bool | None]


@dataclass(frozen=True, slots=True)
class PriorParentMergeReconciliation:
    """Cycle-bound evidence and side-effect audit for prior parent merges."""

    evidence_by_issue: dict[int, PriorParentMergeEvidence]
    held_issue_numbers: frozenset[int]
    completed_subtask_ids: frozenset[str]
    events: tuple[dict[str, object], ...]


def _is_after_reopen(merged_at: str, reopened_at: str | None) -> bool | None:
    """Return whether the merge is provably later than the latest reopen.

    GitHub timestamps used here are ISO-8601 UTC strings with second precision.
    Lexicographic comparison is valid only after checking their normal shape;
    unknown formats are intentionally an indeterminate result rather than a
    reason to close an Issue.
    """
    if not merged_at:
        return None
    if reopened_at is None:
        return True
    if not reopened_at:
        return None
    if not (merged_at.endswith("Z") and reopened_at.endswith("Z")):
        return None
    try:
        merged = datetime.fromisoformat(merged_at.removesuffix("Z") + "+00:00")
        reopened = datetime.fromisoformat(reopened_at.removesuffix("Z") + "+00:00")
    except ValueError:
        return None
    return merged > reopened


def _identity_status(pr: PrRecord, issue_number: int, subtask_id: str) -> str:
    """Return ``match``, ``none``, or ``conflict`` for strict Issue identity."""
    explicit = issue_number in pr.closes_issue_numbers
    canonical = branch_matches_task(pr.head_ref, issue_number, subtask_id)
    parsed = parse_task_branch_name(pr.head_ref)
    if parsed is not None and parsed.issue_number != issue_number:
        return "conflict" if explicit else "none"
    if canonical and pr.closes_issue_numbers and not explicit:
        return "conflict"
    return "match" if canonical or explicit else "none"


def _validated_candidate(
    pr: PrRecord,
    *,
    issue_number: int,
    subtask_id: str,
    expected_base: str,
    last_reopened_at: str | None,
    merge_commit_is_reachable: MergeReachabilityProbe,
) -> tuple[PrRecord | None, str]:
    """Return a verified PR or the reason why a plausible candidate is unsafe."""
    if pr.state != "MERGED" or pr.base_ref != expected_base:
        return None, ""
    identity = _identity_status(pr, issue_number, subtask_id)
    if identity == "none":
        return None, ""
    if identity == "conflict":
        return None, "conflicting PR branch and closing identity"
    if pr.is_cross_repository is not False:
        return (
            (None, "missing repository identity metadata")
            if pr.is_cross_repository is None
            else (None, "")
        )
    if not pr.merged_at or not pr.merge_commit_oid:
        return None, "missing merged timestamp or merge commit metadata"
    after_reopen = _is_after_reopen(pr.merged_at, last_reopened_at)
    if after_reopen is None:
        return None, "unparseable reopen or merge timestamp"
    if not after_reopen:
        return None, ""
    try:
        reachable = merge_commit_is_reachable(pr.merge_commit_oid, expected_base)
    except Exception:  # pragma: no cover - contract treats Forge failures uniformly
        reachable = None
    if reachable is None:
        return None, "could not verify merge commit in current parent tip"
    return (pr, "") if reachable else (None, "")


def evaluate_prior_parent_merge(
    *,
    issue_number: int,
    parent_issue_number: int | None,
    subtask_id: str,
    prs: Iterable[PrRecord],
    last_reopened_at: str | None,
    merge_commit_is_reachable: MergeReachabilityProbe,
) -> PriorParentMergeEvidence:
    """Evaluate historical PR records without allowing broad association.

    A verified result requires all of: a real parent, exact parent base, merged
    state, upstream repository identity, canonical task branch or explicit
    closing reference, complete merge metadata, a post-reopen merge time, and
    proof that the recorded merge commit remains in the current parent tip.
    """
    if parent_issue_number is None or not subtask_id:
        return PriorParentMergeEvidence(
            PriorParentMergeStatus.INDETERMINATE,
            reason="missing real child parent or subtask identity",
        )
    expected_base = f"parent/issue-{parent_issue_number}"
    candidates = [
        _validated_candidate(
            pr,
            issue_number=issue_number,
            subtask_id=subtask_id,
            expected_base=expected_base,
            last_reopened_at=last_reopened_at,
            merge_commit_is_reachable=merge_commit_is_reachable,
        )
        for pr in prs
    ]
    verified = [pr for pr, _ in candidates if pr is not None]
    indeterminate_reason = next((reason for _, reason in candidates if reason), "")

    if verified:
        pr = max(verified, key=lambda candidate: candidate.merged_at)
        return PriorParentMergeEvidence(
            PriorParentMergeStatus.ALREADY_MERGED,
            pr_number=pr.number,
            base_ref=pr.base_ref,
            merged_at=pr.merged_at,
        )
    if indeterminate_reason:
        return PriorParentMergeEvidence(
            PriorParentMergeStatus.INDETERMINATE, reason=indeterminate_reason
        )
    return PriorParentMergeEvidence(PriorParentMergeStatus.NOT_FOUND)


def inspect_prior_parent_merge(
    forge, issue_number: int, task: Task, issue: IssueRecord | None = None
) -> tuple[PriorParentMergeEvidence, IssueRecord | None]:
    """Read a current Issue and its parent-scoped PR history without mutation."""
    try:
        issue = issue or forge.get_issue(issue_number)
        if issue is None:
            return (
                PriorParentMergeEvidence(
                    PriorParentMergeStatus.INDETERMINATE,
                    reason="current Issue metadata was unavailable",
                ),
                None,
            )
        actual_parent = effective_parent_number(issue)
        if actual_parent != task.parent_number:
            return (
                PriorParentMergeEvidence(
                    PriorParentMergeStatus.INDETERMINATE,
                    reason="current Issue parent differs from dispatch task parent",
                ),
                issue,
            )
        if actual_parent is None:
            return (
                PriorParentMergeEvidence(
                    PriorParentMergeStatus.INDETERMINATE,
                    reason="current Issue has no verifiable parent",
                ),
                issue,
            )
        prs = forge.list_merged_prs_for_base(f"parent/issue-{actual_parent}")
        reopened_at = forge.get_issue_last_reopened_at(issue_number)
        evidence = evaluate_prior_parent_merge(
            issue_number=issue_number,
            parent_issue_number=actual_parent,
            subtask_id=task.subtask_id,
            prs=prs,
            last_reopened_at=reopened_at,
            merge_commit_is_reachable=forge.is_merge_commit_reachable_from,
        )
        return evidence, issue
    except Exception as error:  # noqa: BLE001 - evidence collection fails closed
        return (
            PriorParentMergeEvidence(
                PriorParentMergeStatus.INDETERMINATE,
                reason=f"prior merge evidence lookup failed: {type(error).__name__}",
            ),
            None,
        )


def _apply_verified_repair(
    forge, issue: IssueRecord, evidence: PriorParentMergeEvidence
) -> None:
    """Apply repair in restart-safe order after a second fresh evidence check."""
    stale_statuses = tuple(
        label for label in PRIMARY_STATUS_LABELS if label in issue.labels
    )
    transition_status_label(forge, issue.number, StatusLabel.DONE, stale_statuses)
    assert evidence.pr_number is not None
    ensure_pr_merged_notice(forge, issue.number, evidence.pr_number, evidence.base_ref)
    forge.close_issue(issue.number, "completed")


def _evidence_event(
    issue_number: int, evidence: PriorParentMergeEvidence
) -> dict[str, object]:
    return {
        "issue_number": issue_number,
        "action": evidence.status.value,
        "pr_number": evidence.pr_number,
        "base_ref": evidence.base_ref,
        "merged_at": evidence.merged_at,
        "reason": evidence.reason,
    }


def _apply_or_preview_verified_repair(
    forge, issue_number: int, task: Task, *, apply: bool
) -> tuple[dict[str, object], bool]:
    """Re-verify a successful scan, then apply its idempotent repair."""
    fresh, fresh_issue = inspect_prior_parent_merge(forge, issue_number, task)
    event = _evidence_event(issue_number, fresh)
    if fresh.status is not PriorParentMergeStatus.ALREADY_MERGED or fresh_issue is None:
        event["action"] = "prior_merge_changed_before_repair"
        return event, False
    if not apply:
        event["action"] = "already_merged_dry_run"
        return event, True
    if fresh_issue.state.upper() != "OPEN":
        return event, True
    try:
        _apply_verified_repair(forge, fresh_issue, fresh)
    except Exception as error:  # noqa: BLE001 - retry idempotently next cycle
        event["action"] = "already_merged_repair_pending"
        event["reason"] = f"repair failed: {type(error).__name__}"
        return event, False
    return event, True


def reconcile_prior_parent_merges(
    forge,
    tasks_by_issue: dict[int, Task],
    *,
    apply: bool,
    issues_by_number: dict[int, IssueRecord] | None = None,
    active_issue_numbers: frozenset[int] = frozenset(),
) -> PriorParentMergeReconciliation:
    """Repair only verified historical merges and hold only indeterminate tasks.

    The evaluator is intentionally repeated immediately before mutation: a
    reopen, reparent, or branch recreation observed between the initial scan
    and the close must invalidate the repair rather than be overwritten.
    """
    evidence_by_issue: dict[int, PriorParentMergeEvidence] = {}
    held: set[int] = set()
    completed: set[str] = set()
    events: list[dict[str, object]] = []
    for issue_number, task in tasks_by_issue.items():
        if task.parent_number is None or not task.subtask_id:
            continue
        # A living local/cloud execution owns its worktree until GC observes a
        # natural completion.  Closing it here could strand that agent while it
        # still has unsaved work; GC re-evaluates the same evidence afterward.
        if issue_number in active_issue_numbers:
            continue
        evidence, issue = inspect_prior_parent_merge(
            forge,
            issue_number,
            task,
            (issues_by_number or {}).get(issue_number),
        )
        evidence_by_issue[issue_number] = evidence
        if evidence.status is PriorParentMergeStatus.NOT_FOUND:
            continue
        held.add(issue_number)
        event = _evidence_event(issue_number, evidence)
        if evidence.status is PriorParentMergeStatus.INDETERMINATE:
            events.append(event)
            continue
        event, repair_completed = _apply_or_preview_verified_repair(
            forge, issue_number, task, apply=apply
        )
        if repair_completed:
            completed.add(task.subtask_id)
        events.append(event)
    return PriorParentMergeReconciliation(
        evidence_by_issue=evidence_by_issue,
        held_issue_numbers=frozenset(held),
        completed_subtask_ids=frozenset(completed),
        events=tuple(events),
    )


__all__ = [
    "PriorParentMergeEvidence",
    "PriorParentMergeReconciliation",
    "PriorParentMergeStatus",
    "evaluate_prior_parent_merge",
    "inspect_prior_parent_merge",
    "reconcile_prior_parent_merges",
]
