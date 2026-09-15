"""Build dispatcher conflict constraints from Issue-derived task metadata."""

from __future__ import annotations

import re
from collections.abc import Iterable

from orchestune.dag.graph import build_conflict_graph
from orchestune.dag.models import ConflictEdge, ConflictGraph, SubTask
from orchestune.dispatch.dependency_resolution import legacy_merged_depends_on
from orchestune.models import Task


def subtasks_from_tasks(tasks: Iterable[Task]) -> dict[str, SubTask]:
    """Convert dispatcher tasks to the shared DAG/conflict domain model.

    #799レビュー指摘(Codex P2): `Task.depends_on`は本文由来の文字列のみを
    保持するため、ネイティブ`blocked_by`しか宣言していない依存は
    `legacy_merged_depends_on`で復元してから`SubTask.depends_on`へ渡す
    （渡ってきた`tasks`集合内でだけ解決すれば足りる——このConflict
    Graph/critical-pathは元々1回のディスパッチサイクルが見るタスク集合で
    完結しており、cross-EPICの衝突安全性は対象外）。
    """
    materialized = list(tasks)
    issue_to_subtask_id = {
        task.issue_number: task.subtask_id for task in materialized if task.subtask_id
    }
    return {
        task.subtask_id: SubTask(
            id=task.subtask_id,
            description="",
            footprint=task.footprint,
            symbols=task.symbols,
            depends_on=legacy_merged_depends_on(task, issue_to_subtask_id),
            risk=task.risk,
            risk_reasons=(),
            priority=task.priority,
            shared_contract=task.shared_contract,
            writes_shared_contract=task.writes_shared_contract,
            issue_number=task.issue_number,
        )
        for task in materialized
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


def _subtasks_by_id(derived_inputs: Iterable[SubTask]) -> dict[str, SubTask]:
    """Collapse a possibly-duplicate `SubTask` sequence to one per id.

    Later entries win on a duplicate id, matching `subtasks_from_tasks`'s own
    dict-comprehension semantics (#888).
    """
    return {subtask.id: subtask for subtask in derived_inputs if subtask.id}


def build_task_conflict_graph(
    tasks: Iterable[Task],
    *,
    threshold: float,
    ignore_patterns: Iterable[re.Pattern[str]] = (),
    derived_inputs: Iterable[SubTask] | None = None,
) -> ConflictGraph:
    """Build scheduling exclusions, serializing all tasks if metadata is invalid.

    `derived_inputs` (#888) lets a caller pass a precomputed `SubTask` sequence
    (e.g. from `dependency_resolution.build_legacy_dag_inputs`) instead of
    having this function re-derive it from `tasks` via `subtasks_from_tasks`.
    `tasks` is still required in that case: the `ValueError` fail-closed path
    below needs the raw `Task.subtask_id`s, which `derived_inputs` alone does
    not guarantee reflects the same population. Omitting `derived_inputs`
    (the existing call sites) is unchanged by this PR.
    """
    task_list = list(tasks)
    try:
        subtasks = (
            _subtasks_by_id(derived_inputs)
            if derived_inputs is not None
            else subtasks_from_tasks(task_list)
        )
        return build_conflict_graph(
            list(subtasks.values()),
            threshold=threshold,
            ignore_patterns=ignore_patterns,
        )
    except ValueError:
        return _fail_closed_graph(task_list)
