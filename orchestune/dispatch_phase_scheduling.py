"""Scheduling/Launch Phase コーディネーター。

外部ロック・actor権限・スタッキング可否・重複起動・強制直列化の各観点で
起動候補タスクを絞り込み、クオータ判定の上で実際にタスクを起動する。
"""

from __future__ import annotations

from orchestune.dispatch_actor_verification import (
    _apply_actor_verification,
    _decide_actor_verification,
)
from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_cycle_context import IssuesByStatus
from orchestune.dispatch_filters import (
    _filter_candidates_for_forced_serial,
    _filter_deviation_blocked_candidates,
)
from orchestune.dispatch_launch import (
    LaunchContext,
    _apply_duplicate_skip,
    _decide_duplicate_candidates,
    _get_stack_eligible_tasks,
    _launch_selected_tasks,
)
from orchestune.dispatch_locks import ExternalLockScanResult
from orchestune.dispatch_rules import CycleContext
from orchestune.dispatch_scoring import Task, quota_available, select_next_tasks
from orchestune.dispatch_state import save_run_state


def _determine_candidate_tasks(
    ctx: CycleContext,
    issues: IssuesByStatus,
    lock_result: ExternalLockScanResult,
    completed_subtask_ids: set[str],
    any_forced_serial: bool,
) -> tuple[list[Task], dict[int, str]]:
    """起動候補タスクを、外部ロック・actor権限・スタッキング可否・重複起動・
    強制直列化の各観点で絞り込んで確定させる。"""
    newly_locked = {t.issue_number for t in lock_result.to_lock}
    queued_candidates = [
        ctx.tasks_by_issue[issue.number]
        for issue in issues.queued
        if issue.number not in newly_locked
        # #254レビュー対応(#275 Codex P1): handle_merge_failureがadd(queued)
        # 成功後にremove(done)で失敗すると、Issueがstatus:done/
        # status:queuedを同時に持つ中断状態のまま残りうる。これを通常の
        # 起動候補として扱うと、Integratorの通常のdoneタスク処理（再merge
        # 試行等）と、新たに起動されるエージェントセッションが同じbranchへ
        # 同時に作用しうる。dual-statusのタスクはstatus:doneの除去が
        # 完了する（_reconcile_dual_status_tasksが対応する）まで起動候補
        # から除外する。
        and "status:done" not in ctx.tasks_by_issue[issue.number].status_labels
        # #381レビュー対応(Codex P2): transition_status_labelはadd(新ラベル)を
        # remove(旧ラベル)より先に行うため、removeが失敗/クラッシュすると
        # Issueがstatus:queued/status:in-progressを同時に持つ中断状態のまま
        # 残りうる（launch成功時・abandoned PR再キュー・GC回収の各経路）。
        # status:in-progressは「実際にactive_worktreesへ実起動済み」の確定
        # シグナルであり、この状態のIssueを新規の起動候補として扱うと、
        # 稼働中セッションが既に開いたPRを重複起動と誤認し
        # status:blocked-human-reviewへ誤ってエスカレーションしてしまう
        # （#382: エスカレーション自体もrun_stateの後始末を伴わない）。
        # status:in-progressの除去が完了するまで起動候補から除外する。
        and "status:in-progress" not in ctx.tasks_by_issue[issue.number].status_labels
    ]

    # #119: status:queuedラベルを付与したactorのリポジトリ権限を検証し、
    # 権限不足のタスクを起動候補から除外する（status:blockedからのスタッキング
    # 起動であるstack_eligible_tasksは対象外）。
    actor_decisions = _decide_actor_verification(
        queued_candidates, forge=ctx.config.resolved_forge
    )
    queued_candidates = _apply_actor_verification(actor_decisions, ctx.config)

    stack_eligible_tasks, task_to_base_branch = _get_stack_eligible_tasks(
        issues.blocked,
        ctx.tasks_by_issue,
        ctx.done_subtask_ids,
        ctx.ci_passed_pr_subtask_ids,
        ctx.subtask_branch_map,
        completed_subtask_ids=completed_subtask_ids,
    )

    candidate_tasks = queued_candidates + stack_eligible_tasks

    # 重複起動の防止: 既にオープンなPRが存在するcandidate_tasksを検知し、
    # 起動対象から除外して status:blocked-human-review へ移行させる。
    duplicate_decisions = _decide_duplicate_candidates(candidate_tasks, ctx)
    candidate_tasks = _apply_duplicate_skip(duplicate_decisions, ctx)

    if any_forced_serial:
        candidate_tasks = _filter_candidates_for_forced_serial(
            candidate_tasks,
            ctx.run_state,
            ctx.tasks_by_issue,
        )

    return candidate_tasks, task_to_base_branch


def _finalize_launch(
    selected: list[Task],
    task_to_base_branch: dict[int, str],
    candidate_tasks: list[Task],
    ctx: CycleContext,
    now: float,
    config: DispatcherConfig,
) -> list[Task]:
    """apply時のみ、選出タスクを実起動しrun_stateを永続化する。"""
    if not config.apply:
        return selected
    selected = _launch_selected_tasks(
        LaunchContext(
            selected,
            task_to_base_branch,
            candidate_tasks,
            ctx.run_state,
            now,
            config,
            open_prs=ctx.prs,
        )
    )
    ctx.run_state.last_reconciled_at = now
    save_run_state(
        ctx.run_state,
        config.run_state_path,
        now=now,
        launch_window_seconds=config.window_seconds,
        open_prs=ctx.prs,
    )
    return selected


def run_scheduling_phase(
    ctx: CycleContext,
    issues: IssuesByStatus,
    lock_result: ExternalLockScanResult,
    completed_subtask_ids: set[str],
    any_forced_serial: bool,
    deviation_events: list[dict],
    now: float,
    config: DispatcherConfig,
) -> tuple[list[Task], int]:
    """起動候補の確定からクオータ判定・実起動までの一連を行う。

    `_filter_deviation_blocked_candidates`（deviation_eventsによる絞り込み）
    はdispatch_filtersに定義済みのため、ここではそれを呼び出す。
    """
    candidate_tasks, task_to_base_branch = _determine_candidate_tasks(
        ctx, issues, lock_result, completed_subtask_ids, any_forced_serial
    )

    candidate_tasks = _filter_deviation_blocked_candidates(
        candidate_tasks,
        deviation_events,
        ctx.issue_number_by_subtask_id,
    )

    quota_slots = quota_available(
        ctx.run_state,
        now,
        config.max_concurrent,
        config.max_launches_per_window,
        config.window_seconds,
        max_tokens_per_window=config.max_tokens_per_window,
    )
    selected = select_next_tasks(
        candidate_tasks,
        ctx.run_state,
        now,
        config.max_concurrent,
        config.max_launches_per_window,
        config.window_seconds,
        max_tokens_per_window=config.max_tokens_per_window,
    )
    selected = _finalize_launch(
        selected, task_to_base_branch, candidate_tasks, ctx, now, config
    )
    return selected, quota_slots
