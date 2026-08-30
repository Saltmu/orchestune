"""Reconciliation Phase コーディネーター。

active worktreeごとの完了検知・CHANGES_REQUESTEDエスカレーション・自動リベース・
footprint逸脱検知(rule chain評価)から、run_state自己修復・依存解決による
status:blocked-recompute自動復帰までの、1サイクル中の「整合性回復」に
関わる処理をまとめる。status修復はcycleのConsistencySupervisorが所有する。
"""

from __future__ import annotations

import time
from typing import Any

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.escalation import _rule_changes_requested
from orchestune.dispatch.gc import (
    _rule_completed,
    _rule_not_needed,
    _rule_stale_entry,
)
from orchestune.dispatch.rebase import (
    _rule_auto_rebase,
    _rule_footprint_deviation,
)
from orchestune.dispatch.reconciliation import (
    _handle_base_branch_red_recovery,
    _handle_blocked_recompute_recovery,
    _reconcile_recovery_counters,
    _self_heal_launch_history,
    _self_heal_run_state,
)
from orchestune.dispatch.rules import CycleContext, RuleChain, _ActiveWorktreeAggregates
from orchestune.dispatch.targets import ClaudeCodeCloudRoutineDispatchTarget
from orchestune.integrator.coordinator import (
    IntegrationCoordinator,
    record_pending_not_needed_review,
)


def _dispatch_not_needed_review(
    issue_number: int, subtask_id: str, config: DispatcherConfig
) -> None:
    dispatch_target = config.dispatch_target
    if not isinstance(dispatch_target, ClaudeCodeCloudRoutineDispatchTarget):
        raise RuntimeError("not-needed review requires a cloud routine dispatch target")
    coordinator = IntegrationCoordinator(dispatch_target)
    handle = coordinator.dispatch_not_needed_review(issue_number, subtask_id)
    record_pending_not_needed_review(
        config.not_needed_review_state_path,
        issue_number=issue_number,
        subtask_id=subtask_id,
        session_handle=handle,
    )


# active worktreeごとの判定は、それぞれ対応するact側モジュールに定義された
# ruleとして実装されている(#86, dispatch_cycle.pyは条件判定そのものを持たない)。
# ここでは、それらをどの優先順位で評価するか(early/mainの2つのRuleChain)の
# 組み立てのみを行う。
#
# - status:not-needed / staleな帳簿エントリの検知は、他のどの判定よりも
#   先に評価する必要があるため「早期チェーン」として分離している
#   (該当すればそのactive worktreeへの以後の判定はすべてスキップする)。
# - 完了検知・CHANGES_REQUESTEDエスカレーション・自動リベース・footprint逸脱
#   検知は「主チェーン」として、この優先順位で評価する。
_EARLY_ACTIVE_WORKTREE_RULES = RuleChain(
    rules=[
        _rule_not_needed,
        _rule_stale_entry,
    ]
)

_MAIN_ACTIVE_WORKTREE_RULES = RuleChain(
    rules=[
        _rule_completed,
        _rule_changes_requested,
        _rule_auto_rebase,
        _rule_footprint_deviation,
    ]
)


def _process_active_worktrees(
    ctx: CycleContext,
) -> tuple[list[dict], list[dict], bool, set[str]]:
    """#192/#193/#200: active worktreeごとの完了検知・footprint逸脱処理。

    完了と判定したエントリは（apply時）run_state.active_worktreesから
    除去してクオータを解放し、以後のfootprint逸脱チェックはスキップする。

    新しい判断パターンを追加する場合、このループ自体は変更せず、対応する
    ruleを対応するact側モジュールに書いて、`_EARLY_ACTIVE_WORKTREE_RULES`/
    `_MAIN_ACTIVE_WORKTREE_RULES`に追加するだけでよい（#86）。
    """
    aggregates = _ActiveWorktreeAggregates()

    for key, active in list(ctx.run_state.active_worktrees.items()):
        active_task = ctx.tasks_by_issue.get(active.issue_number)

        if _EARLY_ACTIVE_WORKTREE_RULES.run(ctx, key, active, active_task, aggregates):
            continue

        if active.forced_serial:
            aggregates.any_forced_serial = True

        # _MAIN_ACTIVE_WORKTREE_RULESの末尾(_rule_footprint_deviation)は必ず
        # 非Noneかつterminalな結果を返すため、戻り値を見る必要はない。
        _MAIN_ACTIVE_WORKTREE_RULES.run(ctx, key, active, active_task, aggregates)

    return (
        aggregates.completion_events,
        aggregates.deviation_events,
        aggregates.any_forced_serial,
        aggregates.completed_subtask_ids,
    )


def run_self_heal_phase(
    run_state: Any, config: DispatcherConfig, now: float | None = None
) -> None:
    """run_state自己修復（薄いラッパー、呼び出し互換のため維持）。

    Issue取得直後・`_build_cycle_context`より前に呼び出す必要があるため、
    Reconciliation Phase本体（`run_post_gc_reconciliation`）とは別の
    エントリポイントとして公開する。

    #516再3巡目レビュー指摘: `_self_heal_run_state`は`run_state.json`欠落時
    にしか動作しないため、`_reconcile_recovery_counters`（ファイル有無を
    問わず毎サイクル、既存active_worktreesエントリのstaleなrecompute_count/
    forced_serialを本文と再照合する）を別途呼び出す。再4巡目レビュー指摘
    により、`_reconcile_recovery_counters`は`_self_heal_run_state`と同じく
    リポジトリ全体のIssue一覧を独自に読み直すため、ここでは呼び出し元から
    スコープ済みIssue一覧を受け取らない。
    """
    _self_heal_run_state(run_state, config)
    _reconcile_recovery_counters(run_state, config)
    _self_heal_launch_history(run_state, config, time.time() if now is None else now)


def run_post_gc_reconciliation(
    issues: Any,
    run_state: Any,
    ctx: CycleContext,
    completed_subtask_ids: set[str],
    config: DispatcherConfig,
) -> list[dict]:
    """Maintenance GC Phaseの直後に行う非status自動復帰処理。

    #212: dirty worktreeの完了保留判定を同一サイクル内のゾンビGCが上書き
    しないよう、GCはこの関数より必ず先に(`_process_active_worktrees`の
    completion_eventsを参照して)実行される。そのため、この関数自体はGCの
    結果を必要とせず`completed_subtask_ids`のみを参照する。

    blocked promotionはこの関数より先にConsistencySupervisorが実行する。
    ここではstatus:blocked-recomputeとbase-branch-redの既存復帰処理だけを
    維持し、status commandの判断・実行は行わない。
    """
    # 決定論的な自動復帰（ブロック解除）処理
    promotion_events = _handle_blocked_recompute_recovery(
        issues, run_state, ctx, completed_subtask_ids, config
    )

    # #555: ci:base-branch-red の自動復帰（base_sha前進による再キュー）
    base_branch_red_promoted_events = _handle_base_branch_red_recovery(
        issues, ctx, completed_subtask_ids, config
    )
    promotion_events.extend(base_branch_red_promoted_events)

    return promotion_events
