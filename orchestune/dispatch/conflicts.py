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


def _subtasks_by_id(
    derived_inputs: Iterable[SubTask], tasks: list[Task]
) -> dict[str, SubTask]:
    """Collapse a possibly-duplicate `SubTask` sequence to one per id.

    Later entries win on a duplicate id, matching `subtasks_from_tasks`'s own
    dict-comprehension semantics (#888).

    #905レビュー指摘(Codex P2): 呼び出し側が渡す`derived_inputs`が`tasks`に対して
    stale／filteredでも黙って受け入れると、欠落したタスクには競合辺が張られず、
    `build_task_conflict_graph`のfail-closed契約（メタデータが信用できないなら
    全タスクを直列化する）を無言で無効化してしまう——スケジューラが競合する作業を
    並行起動し得る。ここでID母集団を突き合わせ、差分があれば`ValueError`を送出して
    既存のfail-closed経路へ倒す（fail-closedの出口は`build_task_conflict_graph`の
    1箇所のまま）。

    比較はsetの粒度で行う。`build_legacy_dag_inputs`は同名`subtask_id`をtuple段階で
    潰さないが、ここでのdict化と`subtasks_from_tasks`のdict化はどちらも1件へ潰れるため、
    setが両経路で一致する唯一の粒度になる。`derived_inputs`側は空idも除外せずに数える:
    レガシー経路は空idの`SubTask`を生成しないので、混入は母集団の不一致そのものである。
    """
    subtasks = {subtask.id: subtask for subtask in derived_inputs}
    expected_ids = {task.subtask_id for task in tasks if task.subtask_id}
    if subtasks.keys() != expected_ids:
        raise ValueError(
            "derived_inputs does not cover the same subtask population as tasks: "
            f"missing={sorted(expected_ids - subtasks.keys())}, "
            f"unexpected={sorted(subtasks.keys() - expected_ids)}"
        )
    return subtasks


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
    `tasks` is still required in that case, and stays the authority on the
    population: `_subtasks_by_id` rejects a `derived_inputs` whose ids do not
    match the `tasks` ids, so a stale or filtered sequence fails closed here
    instead of silently dropping the omitted tasks' conflict edges
    (#905レビュー指摘). Omitting `derived_inputs` (the existing call sites) is
    unchanged by this PR.
    """
    task_list = list(tasks)
    try:
        subtasks = (
            _subtasks_by_id(derived_inputs, task_list)
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
