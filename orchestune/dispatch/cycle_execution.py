"""Cycle orchestration with observable phase boundaries.

The facade is passed explicitly to preserve the cycle module's existing patch
surfaces without duplicating business decisions or introducing import cycles.
"""

# ruff: noqa: SLF001 - explicit internal cycle facade preserves existing patch surfaces

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from types import ModuleType
from typing import cast

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_report import CycleReport
from orchestune.dispatch.progress import progress_phase


def execute_pipeline(
    api: ModuleType, ctx, issues, run_state, config, now, repair_cycle, prior_events
) -> CycleReport:
    sink = config.progress
    with progress_phase(sink, "active_worktrees"):
        active = ctx.process_active_worktrees()
        completion_events = [*prior_events, *active.completion_events]
        api._notify_pr_links(ctx, config)
    with progress_phase(sink, "gc_reclaim"):
        completion_events = api._run_gc_reclaim_phase(
            ctx, config, completion_events, repair_cycle
        )
    with progress_phase(sink, "reconciliation_external_locks"):
        promotion_events, lock_result = api._run_pre_scheduling_reconciliation(
            ctx=ctx,
            issues=issues,
            run_state=run_state,
            config=config,
            repair_cycle=repair_cycle,
        )
    with progress_phase(sink, "scheduling"):
        scheduling = api.run_scheduling_phase(
            ctx, lock_result, list(active.deviation_events)
        )
    return cast(
        CycleReport,
        api._pipeline_report(
            scheduling,
            lock_result,
            deviation_events=list(active.deviation_events),
            completion_events=completion_events,
            promotion_events=promotion_events,
            applied=config.apply,
        ),
    )


def execute_locked_cycle(api: ModuleType, config: DispatcherConfig) -> CycleReport:
    sink = config.progress
    with progress_phase(sink, "state_load"):
        run_state = api.load_run_state(config.run_state_path)
        now = api.time.time()
    issues, ctx, recovery_report, prior_merges = api._prepare_cycle_context(
        run_state, config, now
    )
    with progress_phase(sink, "consistency"):
        runtime = api._start_consistency_runtime(config, run_state, issues, ctx)
    repair_cycle = api._RepairCycleState()
    repair_cycle.add_report(recovery_report)
    report = api._execute_cycle_pipeline(
        ctx, issues, run_state, config, now, repair_cycle, prior_merges.events
    )
    with progress_phase(sink, "consistency_postprocessing"):
        api._finish_consistency_runtime(runtime, report, ctx, now, config, repair_cycle)
    if config.apply:
        with progress_phase(sink, "events_record"):
            api.append_event_log(
                api.build_event_log_entry(report, now), config.events_log_path
            )
    else:
        sink.emit("events_record", "skipped", reason="dry_run")
    return cast(CycleReport, report)


def execute_cycle(api: ModuleType, config: DispatcherConfig) -> CycleReport:
    sink = config.progress
    with progress_phase(sink, "cycle"):
        with ExitStack() as stack:
            with progress_phase(sink, "state_lock"):
                stack.enter_context(
                    api.run_state_lock(Path(config.run_state_path).with_suffix(".lock"))
                )
            return execute_locked_cycle(api, config)
