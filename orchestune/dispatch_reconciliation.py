from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from orchestune.dag_graph import recompute_dag_for_footprint_change
from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_labels import transition_status_label
from orchestune.dispatch_locks import check_footprint_deviation
from orchestune.dispatch_rebase import SubTask, _build_subtasks_for_recompute
from orchestune.dispatch_recovery import (
    _reconcile_stale_recovery_counters,
    recover_run_state,
)
from orchestune.dispatch_rules import CycleContext
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import RunState, save_run_state
from orchestune.issue_parsing import launch_history_from_body
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
                threshold=config.dag_similarity_threshold,
                ignore_patterns=config.dag_ignore_patterns,
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


def _restore_launch_history(
    run_state: RunState,
    config: DispatcherConfig,
    now: float,
) -> bool:
    """#514: 親Issue本文へ永続化された`launch_history`を復元する。
    変更した場合`True`を返す（呼び出し元が保存要否を判断する）。

    `run_state.json`が消えるステートレスCIランナーでは`launch_history`が
    毎回空になり、`max_launches_per_window`（既定1回/3600秒）が実質無効に
    なる。`orchestune dispatch`に常駐モードは無く毎回が別プロセスのため、
    この上限は元々「実行をまたぐ」束縛として設計されており、永続状態を要する。

    スコープ（Issue #514の決定）:
    - `--parent-issue`未指定（フラットモード）では永続化先の親Issueが無いため
      何もしない
    - **永続化ストアだけが親ごと**（親Issue本文が唯一の置き場所のため）。
      実行時のクオータ判定（`quota_available`）は`run_state.launch_history`を
      グローバルに数える既存挙動のままで、本PRはそこを変更しない
      （#519レビュー指摘(P2)）。したがって永続ディスクで複数の親を運用する
      場合、ここで復元した親Bの履歴は親Aのローカル履歴と合算されて数えられる。
      逆にステートレス環境では現在の親の分しか復元されないため、実効的な束縛は
      親ごとに近づく——この近似は意図的で、真の親ごとクオータには
      `quota_available`側の変更（既存利用者の束縛を緩める挙動変更）が要る

    復元は**片方向**（和集合）で行う: 本文側が古い場合にローカルの進捗
    （より多くの起動）を巻き戻すと、上限が緩む方向へ壊れるため。
    ウィンドウ外のタイムスタンプは`prune_run_state`と同じ意味論で除外する。
    """
    if not config.apply or config.parent_issue_number is None:
        return False
    issue = config.resolved_forge.get_issue(config.parent_issue_number)
    if issue is None:
        return False
    persisted = launch_history_from_body(issue.body)
    if not persisted:
        return False
    min_time = now - config.window_seconds
    in_window = [t for t in persisted if t >= min_time]
    # #519レビュー指摘(P1): 集合和ではなく**多重集合**として、各値の出現回数の
    # 大きい方を採る。同一サイクルで複数タスクが起動するといずれもサイクル共通の
    # `now`をappendするため、重複タイムスタンプは「別々の起動」を表す正当な
    # データであり、畳むと起動数を過少に数えて上限に余剰スロットが生まれる。
    local_counts = Counter(run_state.launch_history)
    persisted_counts = Counter(in_window)
    merged_counts = Counter(
        {
            timestamp: max(local_counts[timestamp], persisted_counts[timestamp])
            for timestamp in set(local_counts) | set(persisted_counts)
        }
    )
    merged = sorted(merged_counts.elements())
    if merged == sorted(run_state.launch_history):
        return False
    run_state.launch_history = merged
    return True


def _self_heal_launch_history(
    run_state: RunState,
    config: DispatcherConfig,
    now: float,
) -> None:
    """#514: 親Issue本文から`launch_history`を復元し、変更があれば永続化する。

    PR #516の3巡目レビューで学んだ通り、`_self_heal_run_state`は
    `run_state.json`欠落時にしか動作しないため、そこへ相乗りさせると
    「ファイルは存在するが`launch_history`だけ古い」ケースへ到達できない。
    ファイル有無を問わず毎サイクル呼び出す。

    保存時に`open_prs`を渡す理由は`_reconcile_recovery_counters`と同じ
    （渡さないと`prune_run_state`がopen PR紐付きの完了履歴保護を適用せず、
    30日超の`last_completed`を無条件に刈り込んでしまう）。
    """
    if not _restore_launch_history(run_state, config, now):
        return
    open_prs = config.resolved_forge.list_open_prs()
    save_run_state(
        run_state,
        config.run_state_path,
        launch_window_seconds=config.window_seconds,
        open_prs=open_prs,
    )


def _reconcile_recovery_counters(
    run_state: RunState,
    config: DispatcherConfig,
) -> None:
    """#516再3巡目・再4巡目レビュー指摘: `_reconcile_stale_recovery_counters`
    （`dispatch_recovery.py`）は`recover_run_state`経由でのみ呼ばれていたが、
    `recover_run_state`は`_self_heal_run_state`が`run_state.json`欠落時にしか
    呼ばない。そのときの`run_state.active_worktrees`は常に空（新規ロード）
    のため、「既存だがstaleなエントリ」の再照合は生産コードから一度も
    到達し得なかった——`_persist_recovery_counters`が本文への書き込みに
    成功した直後、サイクル終端の`save_run_state`前にプロセスが停止すると、
    既存の（ファイルが存在する）`run_state.json`が古い値のまま残り続ける。
    ファイル有無に関わらず毎サイクル呼び出す。

    `_self_heal_run_state`の`#156`コメントと同じ理由により、
    `parent_issue_number`指定時にスコープが絞られたIssue一覧は使わず、
    常にリポジトリ全体のstatus:in-progress Issueを独自に読み直す:
    `run_state.active_worktrees`は複数の親Issue（big rock）にまたがって
    共有されうるため、スコープを絞った一覧を渡すと他の親Issue配下の
    active worktreeが再照合対象から漏れる。

    保存が必要になった場合のみ`open_prs`を取得して`save_run_state`へ渡す:
    `open_prs`無しで保存すると、`prune_run_state`は30日超の
    `completed_worktrees`保護（open PRの重複判定に使う`last_completed`）を
    一切適用せず無条件に刈り込んでしまい、これが通常のサイクル終端保存
    より先に実行され、かつ直後にプロセスが停止すると永続化されてしまう。
    """
    if not config.apply:
        return
    in_progress_issues = config.resolved_forge.list_issues_by_label(
        "status:in-progress"
    )
    if _reconcile_stale_recovery_counters(run_state, in_progress_issues):
        open_prs = config.resolved_forge.list_open_prs()
        save_run_state(
            run_state,
            config.run_state_path,
            launch_window_seconds=config.window_seconds,
            open_prs=open_prs,
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
