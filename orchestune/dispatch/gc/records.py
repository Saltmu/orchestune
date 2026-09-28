"""Completion history construction shared by legacy and journal collectors."""

import time

from orchestune.ledger.run_state import ActiveWorktree, CompletedWorktree
from orchestune.models import Usage
from orchestune.task_metadata import TaskMetadata


def _completed_worktree_record(
    completion_active: ActiveWorktree,
    active_task: TaskMetadata | None,
    completion_event: dict,
) -> CompletedWorktree:
    raw_usage = completion_event.get("usage")
    usage_obj = Usage(**raw_usage) if raw_usage else None
    return CompletedWorktree(
        issue_number=completion_active.issue_number,
        subtask_id=active_task.subtask_id if active_task else "",
        branch=completion_active.branch,
        started_at=completion_active.started_at,
        completed_at=time.time(),
        recompute_count=completion_active.recompute_count,
        forced_serial=completion_active.forced_serial,
        commit_sha=completion_event.get("commit_sha"),
        base_branch=completion_active.base_branch,
        usage=usage_obj,
        profile=completion_active.profile,
        model=completion_active.model,
        reasoning_effort=completion_active.reasoning_effort,
        selection_reason=completion_active.selection_reason,
    )
