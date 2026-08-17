from __future__ import annotations

from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree, RunState
from orchestune.issue_parsing import effective_parent_number
from orchestune.models import IssueRecord


def _candidate_conflicts_with_forced_serial_active(
    candidate: Task,
    active: ActiveWorktree,
    active_task: Task | None,
) -> bool:
    active_footprint = active.declared_footprint
    active_subtask_id = ""
    active_depends_on: tuple[str, ...] = ()
    if active_task is not None:
        active_footprint = active_task.footprint or active.declared_footprint
        active_subtask_id = active_task.subtask_id
        active_depends_on = active_task.depends_on

    if set(candidate.footprint) & set(active_footprint):
        return True

    if active_subtask_id and active_subtask_id in candidate.depends_on:
        return True

    if candidate.subtask_id and candidate.subtask_id in active_depends_on:
        return True

    return False


def _filter_candidates_for_forced_serial(
    candidate_tasks: list[Task],
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
) -> list[Task]:
    forced_serial_actives = [
        (active, tasks_by_issue.get(active.issue_number))
        for active in run_state.active_worktrees.values()
        if active.forced_serial
    ]
    if not forced_serial_actives:
        return candidate_tasks

    return [
        candidate
        for candidate in candidate_tasks
        if not any(
            _candidate_conflicts_with_forced_serial_active(
                candidate, active, active_task
            )
            for active, active_task in forced_serial_actives
        )
    ]


def _filter_deviation_blocked_candidates(
    candidate_tasks: list[Task],
    deviation_events: list[dict],
    issue_number_by_subtask_id: dict[str, int],
) -> list[Task]:
    """同一サイクルのfootprint逸脱でブロックされた候補を除外する。"""
    newly_blocked_recompute_issues = set()
    for event in deviation_events:
        if event.get("action") == "recomputed":
            for conflict in event.get("conflicts", []):
                blocked_id = conflict.get("blocked_subtask_id")
                if blocked_id:
                    issue_number = issue_number_by_subtask_id.get(blocked_id)
                    if issue_number is not None:
                        newly_blocked_recompute_issues.add(issue_number)

    if not newly_blocked_recompute_issues:
        return candidate_tasks

    return [
        task
        for task in candidate_tasks
        if task.issue_number not in newly_blocked_recompute_issues
    ]


def _filter_by_parent(
    issues: list[IssueRecord], parent_issue_number: int | None
) -> list[IssueRecord]:
    """`parent_issue_number`が指定されている場合、親Issueが一致するものだけに絞る。"""
    if parent_issue_number is None:
        return issues
    return [i for i in issues if effective_parent_number(i) == parent_issue_number]
