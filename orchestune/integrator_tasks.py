from __future__ import annotations

import sys

from orchestune import github
from orchestune.dag import SubTask, build_dag
from orchestune.dispatcher import Task, parse_task_from_issue


def build_issue_to_subtask_id_map(issues: list[github.IssueRecord]) -> dict[int, str]:
    import yaml

    from orchestune.dispatch_scoring import _FOOTPRINT_BLOCK_PATTERN

    issue_to_subtask_id = {}
    for issue in issues:
        match = _FOOTPRINT_BLOCK_PATTERN.search(issue.body)
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


def get_sorted_done_tasks(
    parent_issue_number: int | None,
) -> tuple[list[Task], list[Task]]:
    """`status:done`タスクを依存関係のトポロジカル順に並べる。

    戻り値は`(sorted_done_tasks, unparsable_done_tasks)`。後者はFootprint YAMLから
    `subtask_id`を抽出できなかった`status:done`タスクで、マージ対象のIDに紐付けられない
    ため統合できない。
    """
    done_issues = github.list_issues_by_label("status:done", state="all")
    if not done_issues:
        return [], []

    all_issues = []
    for label in [
        "status:queued",
        "status:in-progress",
        "status:blocked",
        "status:external-lock",
        "status:done",
    ]:
        state = "all" if label == "status:done" else "open"
        all_issues.extend(github.list_issues_by_label(label, state=state))

    # parent_issue_number が指定されている場合、親Issueが一致する子Issueのみにフィルタリングする
    if parent_issue_number is not None:
        done_issues = [
            i
            for i in done_issues
            if i.parent and i.parent.get("number") == parent_issue_number
        ]
        all_issues = [
            i
            for i in all_issues
            if i.parent and i.parent.get("number") == parent_issue_number
        ]

    seen_numbers = set()
    unique_issues = []
    for issue in all_issues:
        if issue.number not in seen_numbers:
            seen_numbers.add(issue.number)
            unique_issues.append(issue)

    # すべてのIssueについて、YAML内の subtask_id を事前スキャンしてマッピングを構築する
    issue_to_subtask_id = build_issue_to_subtask_id_map(unique_issues + done_issues)

    tasks = [
        parse_task_from_issue(issue, issue_to_subtask_id) for issue in unique_issues
    ]
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
        dag = build_dag(subtasks)
        topological_order = dag.topological_order
    except Exception as e:
        print(f"Warning: Failed to build DAG: {e}", file=sys.stderr)
        topological_order = [t.id for t in subtasks]

    done_tasks = [
        parse_task_from_issue(issue, issue_to_subtask_id) for issue in done_issues
    ]
    unparsable_done_tasks = [t for t in done_tasks if not t.subtask_id]
    done_task_map = {t.subtask_id: t for t in done_tasks if t.subtask_id}

    sorted_done_tasks = []
    for subtask_id in topological_order:
        if subtask_id in done_task_map:
            sorted_done_tasks.append(done_task_map[subtask_id])

    for t in done_tasks:
        if t.subtask_id and t.subtask_id not in [
            x.subtask_id for x in sorted_done_tasks
        ]:
            sorted_done_tasks.append(t)

    return sorted_done_tasks, unparsable_done_tasks
