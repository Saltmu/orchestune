from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestune.dag_graph import recompute_dag_for_footprint_change
from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_labels import transition_status_label
from orchestune.dispatch_locks import check_footprint_deviation
from orchestune.dispatch_rebase import SubTask, _build_subtasks_for_recompute
from orchestune.dispatch_recovery import recover_run_state
from orchestune.dispatch_rules import CycleContext
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import RunState, save_run_state
from orchestune.models import IssueRecord


def _collect_active_conflict_subtask_ids(
    run_state: RunState,
    ctx: CycleContext,
    subtasks_for_recompute: dict[str, SubTask],
    config: DispatcherConfig,
) -> set[str]:
    """アクティブなワークツリーが持つフットプリントと競合するサブタスクIDの集合を収集する。"""
    active_conflict_subtask_ids = set()
    for active in run_state.active_worktrees.values():
        active_task = ctx.tasks_by_issue.get(active.issue_number)
        if not active_task or not active_task.subtask_id:
            continue

        deviated = check_footprint_deviation(
            active.worktree_path,
            active.declared_footprint,
            base=active.base_branch,
            min_changed_lines=config.deviation_buffer_lines,
        )
        if deviated is None:
            # 検出不能なエラー時は fail-closed とし、自動復帰させない（＝全てのサブタスクが競合中とする）
            for subtask_id in subtasks_for_recompute:
                active_conflict_subtask_ids.add(subtask_id)
            continue
        merged_footprint = tuple(dict.fromkeys([*active.declared_footprint, *deviated]))
        try:
            _, conflicts = recompute_dag_for_footprint_change(
                subtasks_for_recompute,
                active_task.subtask_id,
                updated_footprint=merged_footprint,
            )
            for conflict in conflicts:
                if conflict.blocked_subtask_id:
                    active_conflict_subtask_ids.add(conflict.blocked_subtask_id)
        except Exception:
            # DAG再計算中の例外発生時も fail-closed とし、自動復帰させない（＝全てのサブタスクを競合中とする）
            for subtask_id in subtasks_for_recompute:
                active_conflict_subtask_ids.add(subtask_id)
    return active_conflict_subtask_ids


def _decide_blocked_promotions(
    blocked_issues: list[IssueRecord],
    done_issues: list[IssueRecord],
    completed_subtask_ids: set[str],
    tasks_by_issue: dict[int, Task],
) -> list[Task]:
    """#193: 依存先が全て解決したstatus:blockedタスクを副作用なしで判定する。

    #280: `done_issues`には`status:done`と`status:not-needed`の両方を
    呼び出し側で合流させて渡すことで、対応不要と判定された依存先も
    「解決済み」として扱われる（このタスク自体は依存先の状態を区別しない）。
    """
    done_subtask_ids = {
        tasks_by_issue[issue.number].subtask_id
        for issue in done_issues
        if issue.number in tasks_by_issue and tasks_by_issue[issue.number].subtask_id
    } | completed_subtask_ids

    promotable = []
    for issue in blocked_issues:
        if "status:blocked-recompute" in issue.labels:
            continue
        task = tasks_by_issue.get(issue.number)
        if task is None or not task.depends_on:
            continue
        if not all(dep in done_subtask_ids for dep in task.depends_on):
            continue
        promotable.append(task)
    return promotable


def _apply_blocked_promotions(
    promotable: list[Task], config: DispatcherConfig
) -> list[dict]:
    events: list[dict] = []
    for task in promotable:
        if config.apply:
            transition_status_label(
                config.resolved_forge,
                task.issue_number,
                "status:queued",
                ("status:blocked",),
            )
        events.append(
            {"issue_number": task.issue_number, "subtask_id": task.subtask_id}
        )
    return events


def _promote_blocked_tasks(
    blocked_issues: list[IssueRecord],
    done_issues: list[IssueRecord],
    completed_subtask_ids: set[str],
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
) -> list[dict]:
    """decide+applyの薄いラッパー（呼び出し互換のため維持）。"""
    promotable = _decide_blocked_promotions(
        blocked_issues, done_issues, completed_subtask_ids, tasks_by_issue
    )
    return _apply_blocked_promotions(promotable, config)


def _self_heal_run_state(
    run_state: RunState,
    config: DispatcherConfig,
) -> None:
    """自己修復（ステート復元・不整合修復）。

    run_state.json が存在しない場合、かつ apply=True の場合のみ復元処理を実行する。

    #156: `run_state.json`は複数の親Issue（big rock）にまたがって共有されうる
    ため、`parent_issue_number`指定時のfast pathでスコープが絞られた
    `IssuesByStatus`は使わず、常にリポジトリ全体のstatus:in-progress Issueを
    読み直す。範囲を絞ってしまうと、他の親Issue配下のactive worktreeが
    復元されないまま`run_state.json`が新規保存され、以後永遠に復元機会を
    失うおそれがある。
    """
    if not (config.apply and not Path(config.run_state_path).exists()):
        return
    in_progress_issues = config.resolved_forge.list_issues_by_label(
        "status:in-progress"
    )
    if recover_run_state(run_state, in_progress_issues, config):
        save_run_state(
            run_state,
            config.run_state_path,
            launch_window_seconds=config.window_seconds,
        )


def _decide_dual_status_reconciliation(
    tasks_by_issue: dict[int, Task],
) -> list[Task]:
    """#254レビュー対応(#275 Codex P1): `handle_merge_failure`がadd(queued)
    成功後にremove(done)で失敗すると、Issueが`status:done`/`status:queued`
    を同時に持つ中断状態のまま残りうる。この関数はそうしたdual-status
    タスクを副作用なしで検出する（`_determine_candidate_tasks`が起動候補
    から既に除外しているため、これは中断していた遷移を完了させるための
    自己修復であり、安全性そのものはこの関数の実行有無に依存しない）。"""
    return [
        task
        for task in tasks_by_issue.values()
        if "status:done" in task.status_labels and "status:queued" in task.status_labels
    ]


def _apply_dual_status_reconciliation(
    tasks: list[Task], config: DispatcherConfig
) -> list[dict]:
    events: list[dict] = []
    for task in tasks:
        if config.apply:
            config.resolved_forge.remove_label(task.issue_number, "status:done")
        events.append(
            {"issue_number": task.issue_number, "subtask_id": task.subtask_id}
        )
    return events


def _reconcile_dual_status_tasks(
    tasks_by_issue: dict[int, Task], config: DispatcherConfig
) -> list[dict]:
    """decide+applyの薄いラッパー（呼び出し互換のため維持）。"""
    dual_status_tasks = _decide_dual_status_reconciliation(tasks_by_issue)
    return _apply_dual_status_reconciliation(dual_status_tasks, config)


def _handle_blocked_recompute_recovery(
    issues: Any,
    run_state: RunState,
    ctx: CycleContext,
    completed_subtask_ids: set[str],
    config: DispatcherConfig,
) -> list[dict]:
    """フットプリント逸脱によるブロック（status:blocked-recompute）の自動復帰（解除）処理を行う。"""
    recompute_resolved_promoted_events: list[dict] = []
    blocked_recompute_issues = [
        issue for issue in issues.all() if "status:blocked-recompute" in issue.labels
    ]

    if not blocked_recompute_issues:
        return recompute_resolved_promoted_events

    subtasks_for_recompute = _build_subtasks_for_recompute(ctx.tasks_by_issue)
    active_conflict_subtask_ids = _collect_active_conflict_subtask_ids(
        run_state, ctx, subtasks_for_recompute, config
    )

    for issue in blocked_recompute_issues:
        task = ctx.tasks_by_issue.get(issue.number)
        if not task or not task.subtask_id:
            continue

        if task.subtask_id not in active_conflict_subtask_ids:
            if config.apply:
                config.resolved_forge.remove_label(
                    issue.number, "status:blocked-recompute"
                )

            done_subtask_ids = ctx.done_subtask_ids | completed_subtask_ids
            has_pending_deps = any(
                dep not in done_subtask_ids for dep in task.depends_on
            )

            if not has_pending_deps:
                if config.apply:
                    transition_status_label(
                        config.resolved_forge,
                        issue.number,
                        "status:queued",
                        ("status:blocked",),
                    )
                recompute_resolved_promoted_events.append(
                    {"issue_number": issue.number, "subtask_id": task.subtask_id}
                )

    return recompute_resolved_promoted_events
