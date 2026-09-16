"""Build dispatcher conflict constraints from Issue-derived task metadata."""

from __future__ import annotations

import re
from collections.abc import Iterable

from orchestune.dag.graph import build_conflict_graph
from orchestune.dag.models import ConflictEdge, ConflictGraph, SubTask
from orchestune.dispatch.dependency_resolution import legacy_merged_depends_on
from orchestune.models import Task
from orchestune.task_metadata import TaskMetadata


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
    """Key a caller-supplied `SubTask` sequence by id, the way `subtasks_from_tasks` would.

    `derived_inputs`は`tasks`と1対1でなければならない。突合は`(issue_number, subtask_id)`
    というタスク識別子で行い、辞書は**`tasks`の順で**組み立てる。これにより、
    同名`subtask_id`が複数あるとき（`--parent-issue`を指定しないサイクルでは別EPICの
    同名タスクが同居し得る）にlast-winsで勝つエントリが`subtasks_from_tasks`と必ず一致する。

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
    - `subtask_id`を持たない`Task`は`subtasks_from_tasks`／`_fail_closed_graph`と同じく
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
    ignore_patterns: Iterable[re.Pattern[str]] = (),
    derived_inputs: Iterable[SubTask] | None = None,
) -> ConflictGraph:
    """Build scheduling exclusions, serializing all tasks if metadata is invalid.

    `derived_inputs` (#888) lets a caller pass a precomputed `SubTask` sequence
    (e.g. from `dependency_resolution.build_legacy_dag_inputs`) instead of
    having this function re-derive it from `tasks` via `subtasks_from_tasks`.
    `tasks` is still required in that case, and stays the authority on the
    population: `_subtasks_by_id` pairs each `SubTask` with its `Task` by
    `(issue_number, subtask_id)` and keys the result in `tasks` order, so a
    stale, filtered, duplicate-padded or reordered sequence fails closed (or,
    for reordering, resolves to the same winner) instead of silently dropping
    the affected tasks' conflict edges (#905レビュー指摘). Omitting
    `derived_inputs` (the existing call sites) is unchanged by this PR.
    """
    task_list = list(tasks)
    try:
        if derived_inputs is not None:
            subtasks = _subtasks_by_id(derived_inputs, task_list)
        else:
            legacy_tasks: list[Task] = []
            for task in task_list:
                if not isinstance(task, Task):
                    raise ValueError("derived_inputs is required for TaskMetadata")
                legacy_tasks.append(task)
            subtasks = subtasks_from_tasks(legacy_tasks)
        return build_conflict_graph(
            list(subtasks.values()),
            threshold=threshold,
            ignore_patterns=ignore_patterns,
        )
    except ValueError:
        return _fail_closed_graph(task_list)
