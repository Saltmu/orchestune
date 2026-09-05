"""GC-specific completion decision for verified historical parent merges."""

from __future__ import annotations

from dataclasses import dataclass

from orchestune.dispatch.prior_parent_merge import (
    PriorParentMergeStatus,
    inspect_prior_parent_merge,
)
from orchestune.dispatch.scoring import Task
from orchestune.forge import Forge
from orchestune.models import IssueRecord


@dataclass(frozen=True, slots=True)
class PriorMergeCompletion:
    action: str
    error: str = ""


def decide_prior_parent_merge_completion(
    active_issue_number: int,
    active_task: Task | None,
    forge: Forge | None,
    issue: IssueRecord | None,
) -> PriorMergeCompletion | None:
    if forge is None or active_task is None or active_task.parent_number is None:
        return None
    prior_merge, _ = inspect_prior_parent_merge(
        forge, active_issue_number, active_task, issue
    )
    if prior_merge.status is PriorParentMergeStatus.INDETERMINATE:
        return PriorMergeCompletion(
            "completion_skipped_prior_merge_indeterminate", prior_merge.reason
        )
    if prior_merge.status is not PriorParentMergeStatus.ALREADY_MERGED:
        return None
    fresh_merge, _ = inspect_prior_parent_merge(forge, active_issue_number, active_task)
    if fresh_merge.status is PriorParentMergeStatus.ALREADY_MERGED:
        return PriorMergeCompletion("already_merged")
    return PriorMergeCompletion(
        "completion_skipped_prior_merge_indeterminate",
        fresh_merge.reason or "prior merge evidence changed",
    )


__all__ = ["PriorMergeCompletion", "decide_prior_parent_merge_completion"]
