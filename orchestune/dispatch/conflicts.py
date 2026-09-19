"""Build dispatcher conflict constraints from Issue-derived task metadata."""

from __future__ import annotations

import re
from collections.abc import Iterable

from orchestune.dag.graph import build_conflict_graph
from orchestune.dag.models import ConflictEdge, ConflictGraph, SubTask
from orchestune.task_metadata import TaskMetadata


def _fail_closed_graph(tasks: list[TaskMetadata]) -> ConflictGraph:
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
    derived_inputs: Iterable[SubTask], tasks: list[TaskMetadata]
) -> dict[str, SubTask]:
    """Key a caller-supplied `SubTask` sequence by id in task-population order.

    `derived_inputs`は`tasks`と1対1でなければならない。突合は`(issue_number, subtask_id)`
    というタスク識別子で行い、辞書は**`tasks`の順で**組み立てる。これにより、
    同名`subtask_id`が複数あるとき（`--parent-issue`を指定しないサイクルでは別EPICの
    同名タスクが同居し得る）にlast-winsで勝つエントリを決定論的に選ぶ。

    #905レビュー指摘(Codex): 呼び出し側が渡す`derived_inputs`が`tasks`に対して
    stale／filtered／重複水増し／並び替えのいずれであっても黙って受け入れると、
    影響を受けたタスクに競合辺が張られず、`build_task_conflict_graph`のfail-closed契約
    （メタデータが信用できないなら全タスクを直列化する）を無言で無効化してしまう
    ——スケジューラが競合する作業を並行起動し得る。ここで1対1を検証し、崩れていれば
    `ValueError`を送出して既存のfail-closed経路へ倒す（fail-closedの出口は
    `build_task_conflict_graph`の1箇所のまま）。

    - **欠落／余剰**（母集団の不一致）: 対応するタスク識別子が見つからない、または
      `derived_inputs`の件数が`subtask_id`を持つ`Task`の件数と合わない → fail closed。
    - **重複**: `derived_inputs`に同一タスク識別子が2件以上 → fail closed。
      `build_legacy_dag_inputs`は`Task`1件につき`SubTask`1件しか出さないため、
      重複はレガシー経路が生成し得ない入力である。
    - **並び順**: 同名`subtask_id`同士の順序違いは、`tasks`の順で組み立てることで
      そもそも結果に影響しない（fail closedにするまでもなく整合する）。
    - `subtask_id`を持たない`Task`は`_fail_closed_graph`と同じく
      母集団外。空idの`SubTask`は余剰として弾かれる（レガシー経路は生成しない）。

    `depends_on`やfootprintの**値**までは検証しない——それは変換のやり直しであり、
    `derived_inputs`を受け取る意味自体が失われる。値の導出責務は呼び出し側
    （`dependency_resolution.build_legacy_dag_inputs`）に残す。
    """
    named_tasks = [task for task in tasks if task.subtask_id]
    by_identity: dict[tuple[int | None, str], SubTask] = {}
    for subtask in derived_inputs:
        identity = (subtask.issue_number, subtask.id)
        if identity in by_identity:
            raise ValueError(
                f"derived_inputs repeats the task identity {identity}; "
                "build_legacy_dag_inputs emits one SubTask per Task"
            )
        by_identity[identity] = subtask
    if len(by_identity) != len(named_tasks):
        raise ValueError(
            "derived_inputs does not cover the same task population as tasks: "
            f"{len(by_identity)} derived input(s) for {len(named_tasks)} task(s)"
        )
    subtasks: dict[str, SubTask] = {}
    for task in named_tasks:
        identity = (task.issue_number, task.subtask_id)
        matched = by_identity.get(identity)
        if matched is None:
            raise ValueError(f"derived_inputs has no SubTask for task {identity}")
        subtasks[task.subtask_id] = matched
    return subtasks


def build_task_conflict_graph(
    tasks: Iterable[TaskMetadata],
    *,
    threshold: float,
    derived_inputs: Iterable[SubTask],
    ignore_patterns: Iterable[re.Pattern[str]] = (),
) -> ConflictGraph:
    """Build scheduling exclusions, serializing all tasks if metadata is invalid.

    `derived_inputs` is the precomputed `SubTask` sequence from the identity
    boundary. `tasks` remains the authority on the
    population: `_subtasks_by_id` pairs each `SubTask` with its `Task` by
    `(issue_number, subtask_id)` and keys the result in `tasks` order, so a
    stale, filtered, duplicate-padded or reordered sequence fails closed (or,
    for reordering, resolves to the same winner) instead of silently dropping
    the affected tasks' conflict edges (#905レビュー指摘).
    """
    task_list = list(tasks)
    try:
        subtasks = _subtasks_by_id(derived_inputs, task_list)
        return build_conflict_graph(
            list(subtasks.values()),
            threshold=threshold,
            ignore_patterns=ignore_patterns,
        )
    except (TypeError, ValueError):
        return _fail_closed_graph(task_list)
