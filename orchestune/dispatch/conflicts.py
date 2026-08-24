"""Build dispatcher conflict constraints from Issue-derived task metadata."""

from __future__ import annotations

import re
from collections.abc import Iterable

from orchestune.dag.graph import build_conflict_graph
from orchestune.dag.models import ConflictEdge, ConflictGraph, SubTask
from orchestune.models import Task


def subtasks_from_tasks(tasks: Iterable[Task]) -> dict[str, SubTask]:
    """Convert dispatcher tasks to the shared DAG/conflict domain model."""
    return {
        task.subtask_id: SubTask(
            id=task.subtask_id,
            description="",
            footprint=task.footprint,
            symbols=task.symbols,
            depends_on=task.depends_on,
            risk=task.risk,
            risk_reasons=(),
            priority=task.priority,
            shared_contract=task.shared_contract,
            writes_shared_contract=task.writes_shared_contract,
            issue_number=task.issue_number,
        )
        for task in tasks
        if task.subtask_id
    }


def _fail_closed_graph(tasks: list[Task]) -> ConflictGraph:
    ids = sorted({task.subtask_id for task in tasks if task.subtask_id})
    edges = tuple(
        ConflictEdge(
            left,
            right,
            reason="invalid-task-metadata",
            resources=("task-metadata",),
        )
        for index, left in enumerate(ids)
        for right in ids[index + 1 :]
    )
    return ConflictGraph(edges)


def build_task_conflict_graph(
    tasks: Iterable[Task],
    *,
    threshold: float,
    ignore_patterns: Iterable[re.Pattern[str]] = (),
) -> ConflictGraph:
    """Build scheduling exclusions, serializing all tasks if metadata is invalid."""
    task_list = list(tasks)
    try:
        subtasks = subtasks_from_tasks(task_list)
        return build_conflict_graph(
            list(subtasks.values()),
            threshold=threshold,
            ignore_patterns=ignore_patterns,
        )
    except ValueError:
        return _fail_closed_graph(task_list)
