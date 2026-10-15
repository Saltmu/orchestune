"""Shared merge/Issue identity evidence, independent of lifecycle orchestration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from orchestune.branch_naming import branch_matches_task, parse_task_branch_name
from orchestune.models import PrRecord


def is_after_reopen(merged_at: str, reopened_at: str | None) -> bool | None:
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


def identity_status(pr: PrRecord, issue_number: int, subtask_id: str) -> str:
    """Return ``match``, ``none``, ``conflict`` or ``unknown`` for strict Issue identity.

    ``unknown`` (#1336) means the record cannot confirm its closing references,
    so neither a match nor its absence is proven. A record without the full list
    still decides once it proves that the Issue is closed by the pull request
    (#1341): the branch name alone then tells a match from a conflict.
    """
    explicit = issue_number in pr.closes_issue_numbers
    if not pr.closing_references_known and not explicit:
        return "unknown"
    canonical = branch_matches_task(pr.head_ref, issue_number, subtask_id)
    parsed = parse_task_branch_name(pr.head_ref)
    if parsed is not None and parsed.issue_number != issue_number:
        return "conflict" if explicit else "none"
    if canonical and pr.closes_issue_numbers and not explicit:
        return "conflict"
    return "match" if canonical or explicit else "none"


def merged_pr_problem(
    pr: PrRecord,
    *,
    pr_number: int,
    branch: str,
    base: str,
    reachable: Callable[[str, str], bool],
) -> str | None:
    if (
        pr.number != pr_number
        or pr.head_ref != branch
        or pr.base_ref != base
        or pr.is_cross_repository is not False
    ):
        return "pr_mismatch"
    if pr.state.upper() != "MERGED":
        return "pr_not_merged"
    if not pr.merged_at or not pr.merge_commit_oid:
        return "merge_unverified"
    try:
        return None if reachable(pr.merge_commit_oid, base) else "merge_unverified"
    except Exception:
        return "merge_unverified"
