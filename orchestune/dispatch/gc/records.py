"""Completion history construction shared by legacy and journal collectors."""

import time

from orchestune.dispatch.cycle_events import CompletionEvent, WorktreeCompletion
from orchestune.ledger.run_state import ActiveWorktree, CompletedWorktree
from orchestune.models import Usage
from orchestune.task_metadata import TaskMetadata


def _completed_worktree_record(
    completion_active: ActiveWorktree,
    active_task: TaskMetadata | None,
    completion_event: CompletionEvent,
) -> CompletedWorktree:
    usage_obj = (
        completion_event.usage
        if isinstance(completion_event, WorktreeCompletion)
        and isinstance(completion_event.usage, Usage)
        else None
    )
    core = completion_active.core
    launch = completion_active.launch
    return CompletedWorktree(
        issue_number=core.issue_number,
        subtask_id=active_task.subtask_id if active_task else "",
        branch=core.branch,
        started_at=launch.started_at,
        completed_at=time.time(),
        recompute_count=launch.recompute_count,
        forced_serial=launch.forced_serial,
        commit_sha=(
            completion_event.commit_sha
            if isinstance(completion_event, WorktreeCompletion)
            else None
        ),
        base_branch=core.base_branch,
        usage=usage_obj,
        profile=launch.profile,
        model=launch.model,
        reasoning_effort=launch.reasoning_effort,
        selection_reason=launch.selection_reason,
    )
