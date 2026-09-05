from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from dataclasses import replace

from orchestune.dag.graph import build_dag
from orchestune.dag.models import SubTask
from orchestune.dag.similarity import DEFAULT_SIMILARITY_THRESHOLD
from orchestune.forge import Forge, GitHubForge
from orchestune.issue_parsing import (
    FOOTPRINT_BLOCK_PATTERN,
    effective_parent_number,
    parse_task_from_issue,
)
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord, Task


def build_issue_to_subtask_id_map(issues: list[IssueRecord]) -> dict[int, str]:
    import yaml

    issue_to_subtask_id = {}
    for issue in issues:
        match = FOOTPRINT_BLOCK_PATTERN.search(issue.body)
        if match:
            try:
                data = yaml.safe_load(match.group(1))
                if isinstance(data, dict):
                    sub_id = data.get("subtask_id")
                    if sub_id:
                        issue_to_subtask_id[issue.number] = str(sub_id)
            except Exception:
                pass
    return issue_to_subtask_id


def _with_merged_depends_on(task: Task, issue_to_subtask_id: dict[int, str]) -> Task:
    """#799: `Task.depends_on`は本文由来の文字列のみを保持するようになった
    （ネイティブ`blocked_by`は`Task.native_depends_on`にIssue番号のまま残る）。

    このモジュールの`_topological_order`/`_check_task_blocking`はいずれも
    `task.depends_on`のsubtask_id文字列だけを見て統合順序・ブロック伝播を
    決めており、この統合(integrator)処理は`parent_issue_number`で常に単一
    EPICへスコープされる（`_load_integration_issues`参照）ため、ここでは
    従来通りネイティブ依存をsubtask_id文字列へ変換して合成してよい
    （`dependency_resolution`が防ぐ別EPIC横断の衝突はそもそも起こらない）。
    """
    native = tuple(
        issue_to_subtask_id[num]
        for num in task.native_depends_on
        if num in issue_to_subtask_id
    )
    merged = native
    for dep in task.depends_on:
        if dep not in merged:
            merged += (dep,)
    return replace(task, depends_on=merged) if merged != task.depends_on else task


def get_sorted_done_tasks(
    parent_issue_number: int | None,
    forge: Forge | None = None,
    ignore_patterns: Iterable[re.Pattern[str]] = (),
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> tuple[list[Task], list[Task]]:
    """`status:done`タスクを依存関係のトポロジカル順に並べる。

    戻り値は`(sorted_done_tasks, unparsable_done_tasks)`。後者はFootprint YAMLから
    `subtask_id`を抽出できなかった`status:done`タスクで、マージ対象のIDに紐付けられない
    ため統合できない。

    `ignore_patterns`と`threshold`は既存呼び出しとの互換性を維持し、
    `orchestune-dag`/`orchestune-dispatch`と同じConflict Graphを構築するために
    `build_dag`へ渡す。統合順序（`topological_order`）自体は明示的な
    `depends_on`だけで決まり、類似度設定には左右されない。
    """
    forge = forge or GitHubForge()
    done_issues = forge.list_issues_by_label(StatusLabel.DONE, state="all")
    if not done_issues:
        return [], []
    done_issues, all_issues = _load_integration_issues(
        forge, done_issues, parent_issue_number
    )
    issue_to_subtask_id = build_issue_to_subtask_id_map(
        _unique_issues(all_issues) + done_issues
    )
    tasks = [
        _with_merged_depends_on(
            parse_task_from_issue(issue, issue_to_subtask_id), issue_to_subtask_id
        )
        for issue in _unique_issues(all_issues)
    ]
    order = _topological_order(tasks, threshold, ignore_patterns)
    return _order_done_tasks(done_issues, issue_to_subtask_id, order)


def _load_integration_issues(
    forge: Forge, done: list[IssueRecord], parent_number: int | None
) -> tuple[list[IssueRecord], list[IssueRecord]]:
    labels = (
        StatusLabel.QUEUED,
        StatusLabel.IN_PROGRESS,
        StatusLabel.BLOCKED,
        StatusLabel.EXTERNAL_LOCK,
    )
    issues = [
        issue
        for label in labels
        for issue in forge.list_issues_by_label(label, state="open")
    ]
    issues.extend(done)
    if parent_number is None:
        return done, issues
    return (
        [issue for issue in done if effective_parent_number(issue) == parent_number],
        [issue for issue in issues if effective_parent_number(issue) == parent_number],
    )


def _unique_issues(issues: list[IssueRecord]) -> list[IssueRecord]:
    seen: set[int] = set()
    unique: list[IssueRecord] = []
    for issue in issues:
        if issue.number not in seen:
            seen.add(issue.number)
            unique.append(issue)
    return unique


def _topological_order(
    tasks: list[Task], threshold: float, ignore_patterns: Iterable[re.Pattern[str]]
) -> list[str]:
    subtasks = [
        SubTask(
            id=task.subtask_id,
            description="",
            footprint=task.footprint,
            symbols=task.symbols,
            depends_on=task.depends_on,
            risk=task.risk,
            risk_reasons=(),
        )
        for task in tasks
        if task.subtask_id
    ]
    try:
        return build_dag(
            subtasks, threshold=threshold, ignore_patterns=ignore_patterns
        ).topological_order
    except Exception as error:
        print(f"Warning: Failed to build DAG: {error}", file=sys.stderr)
        return [task.id for task in subtasks]


def _order_done_tasks(
    done_issues: list[IssueRecord], identifiers: dict[int, str], order: list[str]
) -> tuple[list[Task], list[Task]]:
    done = [
        _with_merged_depends_on(parse_task_from_issue(issue, identifiers), identifiers)
        for issue in done_issues
    ]
    unparsable = [task for task in done if not task.subtask_id]
    by_id = {task.subtask_id: task for task in done if task.subtask_id}
    sorted_done = [by_id.pop(task_id) for task_id in order if task_id in by_id]
    return sorted_done + list(by_id.values()), unparsable
