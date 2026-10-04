"""Pure dispatch report projections onto consistency state changes."""

from __future__ import annotations

from collections.abc import Sequence
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
from orchestune.dispatch.cycle_events import (
    CompletionEvent,
    DeviationEvent,
    PromotionEvent,
)

if TYPE_CHECKING:
    from orchestune.dispatch.cycle_report import CycleReport


def _event_changes(
    events: Sequence[CompletionEvent | DeviationEvent | PromotionEvent],
    fields: tuple[str, ...],
    source: str,
    occurred_at: datetime,
) -> list[StateChanged]:
    return [
        StateChanged(
            scope=ConsistencyScope.TASK,
            subject_id=str(event.issue_number),
            fields=fields,
            source=source,
            occurred_at=occurred_at,
        )
        for event in events
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
    report: CycleReport, now: float
) -> tuple[StateChanged, ...]:
    if not report.applied:
        return ()
    occurred_at = datetime.fromtimestamp(now, UTC)
    changes = _event_changes(
        report.promotion_events,
        (FACT_ISSUE_LABELS,),
        "dispatch.promotion",
        occurred_at,
    )
    changes.extend(
        _event_changes(
            report.completion_events,
            (FACT_EXECUTION_KIND, FACT_ISSUE_LABELS, FACT_PULL_REQUEST_STATE),
            "dispatch.completion",
            occurred_at,
        )
    )
    changes.extend(
        _event_changes(
            report.deviation_events,
            (FACT_BRANCH_NAME, FACT_ISSUE_LABELS),
            "dispatch.deviation",
            occurred_at,
        )
    )
    changes.extend(_lock_state_changes(report, occurred_at))
    changes.extend(_scheduling_state_changes(report, occurred_at))
    return tuple(changes)
