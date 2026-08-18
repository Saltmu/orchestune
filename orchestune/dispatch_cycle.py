"""1サイクル分のディスパッチオーケストレーション本体。

各フェーズの実処理は対応するフェーズコーディネーターモジュール
(`dispatch_cycle_context`/`dispatch_phase_reconciliation`/`dispatch_phase_gc`/
`dispatch_phase_scheduling`/`dispatch_phase_rebase`)に委譲し、
`run_dispatch_cycle`自体はそれらを決まった順序で呼び出すパイプライン制御に
特化する（#477）。
"""

from __future__ import annotations

import time
from pathlib import Path

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_cycle_context import _build_cycle_context, _fetch_issues
from orchestune.dispatch_cycle_report import (
    CycleReport,
    append_event_log,
    build_event_log_entry,
)
from orchestune.dispatch_phase_gc import run_gc_phase
from orchestune.dispatch_phase_rebase import (
    _sync_external_locks,
    ensure_parent_branch_ready,
)
from orchestune.dispatch_phase_reconciliation import (
    _process_active_worktrees,
    run_blocked_promotion_phase,
    run_dual_status_reconciliation,
    run_self_heal_phase,
)
from orchestune.dispatch_phase_scheduling import run_scheduling_phase
from orchestune.dispatch_state import load_run_state
from orchestune.dispatch_worktree import file_lock

__all__ = ["CycleReport", "run_dispatch_cycle"]


def run_dispatch_cycle(config: DispatcherConfig) -> CycleReport:
    lock_path = Path(config.run_state_path).with_suffix(".lock")
    with file_lock(lock_path):
        run_state = load_run_state(config.run_state_path)
        now = time.time()

        ensure_parent_branch_ready(config)

        issues = _fetch_issues(config)
        run_self_heal_phase(run_state, issues.in_progress, config)
        issues = issues.filtered_by_parent(config.parent_issue_number)

        ctx = _build_cycle_context(issues, run_state, config)

        (
            completion_events,
            deviation_events,
            any_forced_serial,
            completed_subtask_ids,
        ) = _process_active_worktrees(ctx)

        completion_events = run_gc_phase(
            ctx.run_state, ctx.tasks_by_issue, config, completion_events
        )

        promotion_events = run_blocked_promotion_phase(
            issues, run_state, ctx, completed_subtask_ids, config
        )

        lock_result = _sync_external_locks(
            ctx.tasks_by_issue, ctx.prs, ctx.run_state, config
        )

        run_dual_status_reconciliation(ctx, config)

        selected, quota_slots = run_scheduling_phase(
            ctx,
            issues,
            lock_result,
            completed_subtask_ids,
            any_forced_serial,
            deviation_events,
            now,
            config,
        )

        report = CycleReport(
            selected=selected,
            quota_slots_available=quota_slots,
            lock_changes={
                "to_lock": lock_result.to_lock,
                "to_unlock": lock_result.to_unlock,
            },
            deviation_events=deviation_events,
            completion_events=completion_events,
            promotion_events=promotion_events,
            applied=config.apply,
        )

        if config.apply:
            append_event_log(build_event_log_entry(report, now), config.events_log_path)

        return report
