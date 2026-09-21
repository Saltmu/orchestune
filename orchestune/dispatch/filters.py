from __future__ import annotations

from typing import Protocol, TypeVar

from orchestune.claim.contracts import ClaimStage, ReservationKind
from orchestune.dispatch.dependency_resolution import (
    EMPTY_DEPENDENCIES,
    TaskDependencies,
)
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.task_metadata import TaskMetadata

TTask = TypeVar("TTask", bound=TaskMetadata)


class ForcedSerialDependencyView(Protocol):
    def task(self, issue_number: int) -> TaskMetadata | None: ...

    def dependencies_of(self, issue_number: int) -> TaskDependencies | None: ...


def _candidate_conflicts_with_forced_serial_active(
    candidate: TaskMetadata,
    active: ActiveWorktree,
    active_task: TaskMetadata | None,
    view: ForcedSerialDependencyView,
) -> bool:
    """#799: タスク間依存判定はsubtask_idの文字列一致ではなく、親Issueで
    スコープ済みに解決されたIssue番号（`view.dependencies_of`）で行う。
    `active_task`が特定できない場合は、従来通り依存関係による判定は行わず
    footprintの重なりのみで判定する。
    """
    if active.reservation_kind == ReservationKind.REPOSITORY:
        return True

    active_footprint = active.declared_footprint
    if active_task is not None:
        active_footprint = active_task.footprint or active.declared_footprint

    if set(candidate.footprint) & set(active_footprint):
        return True

    if active_task is None:
        return False

    active_deps = view.dependencies_of(active_task.issue_number) or EMPTY_DEPENDENCIES
    if candidate.issue_number in active_deps.resolved:
        return True

    candidate_deps = view.dependencies_of(candidate.issue_number) or EMPTY_DEPENDENCIES
    return active_task.issue_number in candidate_deps.resolved


def _is_serializing_active(active: ActiveWorktree) -> bool:
    """他候補の起動を止める対象として扱うactiveかどうか。"""
    return (
        active.forced_serial
        or active.reservation_kind == ReservationKind.REPOSITORY
        or active.claim_stage == ClaimStage.RESERVED
    )


def _filter_candidates_for_forced_serial(
    candidate_tasks: list[TTask],
    run_state: RunState,
    view: ForcedSerialDependencyView,
) -> list[TTask]:
    serializing_actives = [
        (active, view.task(active.issue_number))
        for active in run_state.active_worktrees.values()
        if _is_serializing_active(active)
    ]
    if not serializing_actives:
        return candidate_tasks

    return [
        candidate
        for candidate in candidate_tasks
        if not any(
            _candidate_conflicts_with_forced_serial_active(
                candidate, active, active_task, view
            )
            for active, active_task in serializing_actives
        )
    ]


def _filter_deviation_blocked_candidates(
    candidate_tasks: list[TTask],
    deviation_events: list[dict],
    issue_number_by_subtask_id: dict[str, int],
) -> list[TTask]:
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
