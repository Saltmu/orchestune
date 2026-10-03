"""Pure dispatch report projections onto consistency state changes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from orchestune.consistency.models import ConsistencyScope, StateChanged
from orchestune.consistency.observation import (
    FACT_BRANCH_NAME,
    FACT_EXECUTION_KIND,
    FACT_ISSUE_LABELS,
    FACT_PULL_REQUEST_STATE,
    FACT_WORKTREE_PATH,
)

if TYPE_CHECKING:
    from orchestune.dispatch.cycle_report import CycleReport
    from orchestune.dispatch.rules import CycleContext


def _event_issue_number(event: dict[str, object], ctx: CycleContext) -> int | None:
    issue_number = event.get("issue_number")
    if isinstance(issue_number, int) and not isinstance(issue_number, bool):
        return issue_number
    subtask_id = event.get("subtask_id")
    if isinstance(subtask_id, str):
        matches: list[int] = [
            task.issue_number for task in ctx.tasks() if task.subtask_id == subtask_id
        ]
        if len(matches) == 1:
            return matches[0]
    return None


def _event_changes(
    events: list[dict[str, object]],
    ctx: CycleContext,
    fields: tuple[str, ...],
    source: str,
    occurred_at: datetime,
) -> list[StateChanged]:
    return [
        StateChanged(
            scope=ConsistencyScope.TASK,
            subject_id=str(issue_number),
            fields=fields,
            source=source,
            occurred_at=occurred_at,
        )
        for event in events
        if (issue_number := _event_issue_number(event, ctx)) is not None
    ]


def _lock_state_changes(
    report: CycleReport, occurred_at: datetime
) -> list[StateChanged]:
    return [
        StateChanged(
            scope=ConsistencyScope.TASK,
            subject_id=str(task.issue_number),
            fields=(FACT_ISSUE_LABELS,),
            source=f"dispatch.external-lock.{action}",
            occurred_at=occurred_at,
        )
        for action, tasks in report.lock_changes.items()
        for task in tasks
    ]


def _scheduling_state_changes(
    report: CycleReport, occurred_at: datetime
) -> list[StateChanged]:
    if not report.applied:
        return []
    return [
        StateChanged(
            scope=ConsistencyScope.TASK,
            subject_id=str(task.issue_number),
            fields=(
                FACT_BRANCH_NAME,
                FACT_EXECUTION_KIND,
                FACT_ISSUE_LABELS,
                FACT_WORKTREE_PATH,
            ),
            source="dispatch.scheduling",
            occurred_at=occurred_at,
        )
        for task in report.selected
    ]


def _pipeline_state_changes(
    report: CycleReport, ctx: CycleContext, now: float
) -> tuple[StateChanged, ...]:
    if not report.applied:
        return ()
    occurred_at = datetime.fromtimestamp(now, UTC)
    changes = _event_changes(
        report.promotion_events,
        ctx,
        (FACT_ISSUE_LABELS,),
        "dispatch.promotion",
        occurred_at,
    )
    changes.extend(
        _event_changes(
            report.completion_events,
            ctx,
            (FACT_EXECUTION_KIND, FACT_ISSUE_LABELS, FACT_PULL_REQUEST_STATE),
            "dispatch.completion",
            occurred_at,
        )
    )
    changes.extend(
        _event_changes(
            report.deviation_events,
            ctx,
            (FACT_BRANCH_NAME, FACT_ISSUE_LABELS),
            "dispatch.deviation",
            occurred_at,
        )
    )
    changes.extend(_lock_state_changes(report, occurred_at))
    changes.extend(_scheduling_state_changes(report, occurred_at))
    return tuple(changes)
