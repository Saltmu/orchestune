"""Reconciliation Phase コーディネーター。

active worktreeごとの完了検知・CHANGES_REQUESTEDエスカレーション・自動リベース・
footprint逸脱検知(rule chain評価)から、run_state自己修復・依存解決による
status:blocked-recompute自動復帰までの、1サイクル中の「整合性回復」に
関わる処理をまとめる。status修復はcycleのConsistencySupervisorが所有する。

#884: active worktreeごとのrule chainループ本体は`cycle_actions.py`の
`_run_active_worktree_rules`へ移設した。`_process_active_worktrees`は既存の
`CycleContext`ベース呼出し元(`cycle.py`)向けの後方互換wrapperとして残す。
"""

from __future__ import annotations

from typing import Any

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_actions import (
    _EARLY_ACTIVE_WORKTREE_RULES,
    _MAIN_ACTIVE_WORKTREE_RULES,
    _run_active_worktree_rules,
)
from orchestune.dispatch.reconciliation import (
    _handle_base_branch_red_recovery,
    _handle_blocked_recompute_recovery,
)
from orchestune.dispatch.rules import CycleContext, _RuleExecutionContext

# #884: re-exported for backward-compatible imports after the loop/RuleChain
# relocation to `cycle_actions.py`.
__all__ = [
    "_EARLY_ACTIVE_WORKTREE_RULES",
    "_MAIN_ACTIVE_WORKTREE_RULES",
    "_process_active_worktrees",
    "run_post_gc_reconciliation",
]


def _process_active_worktrees(
    ctx: CycleContext,
) -> tuple[list[dict], list[dict], bool, set[int]]:
    """#192/#193/#200/#884: active worktreeごとの完了検知・footprint逸脱処理。

    実体は`cycle_actions._run_active_worktree_rules`(#884で移設)。この関数は
    既存の`CycleContext`ベース呼出し元(`cycle.py`)向けの薄いwrapperとして、
    シグネチャ・戻り値の形を変えずに残す。
    """
    return _run_active_worktree_rules(_RuleExecutionContext.from_cycle_context(ctx))


def run_post_gc_reconciliation(
    issues: Any,
    run_state: Any,
    ctx: CycleContext,
    completed_issue_numbers: set[int],
    config: DispatcherConfig,
) -> list[dict]:
    """Maintenance GC Phaseの直後に行う非status自動復帰処理。

    #212: dirty worktreeの完了保留判定を同一サイクル内のゾンビGCが上書き
    しないよう、GCはこの関数より必ず先に(`_process_active_worktrees`の
    completion_eventsを参照して)実行される。そのため、この関数自体はGCの
    結果を必要とせず`completed_issue_numbers`のみを参照する。

    blocked promotionはこの関数より先にConsistencySupervisorが実行する。
    ここではstatus:blocked-recomputeとbase-branch-redの既存復帰処理だけを
    維持し、status commandの判断・実行は行わない。
    """
    # 決定論的な自動復帰（ブロック解除）処理
    promotion_events = _handle_blocked_recompute_recovery(
        issues, run_state, ctx, completed_issue_numbers, config
    )

    # #555: ci:base-branch-red の自動復帰（base_sha前進による再キュー）
    base_branch_red_promoted_events = _handle_base_branch_red_recovery(
        issues, ctx, completed_issue_numbers, config
    )
    promotion_events.extend(base_branch_red_promoted_events)

    return promotion_events
