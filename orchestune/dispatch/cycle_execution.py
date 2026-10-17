"""Cycle orchestration with observable phase boundaries."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_execution_contracts import (
    CycleExecutionDependencies,
    RepairCycleAccumulator,
)
from orchestune.dispatch.cycle_report import CycleReport
from orchestune.dispatch.progress import progress_phase
from orchestune.dispatch.promotion_reporting import confirm_promotion_reports
from orchestune.dispatch.timings import (
    CycleTimingCollector,
    TimingProgress,
    timing_snapshot,
)
from orchestune.infra.command_metrics import command_observer_scope

if TYPE_CHECKING:
    from orchestune.dispatch.cycle_context import IssuesByStatus
    from orchestune.dispatch.cycle_events import PriorMergeEvidenceCompletion
    from orchestune.dispatch.rules import CycleContext
    from orchestune.ledger.run_state import RunState


RuntimeT = TypeVar("RuntimeT")
RepairT = TypeVar("RepairT", bound=RepairCycleAccumulator)


def execute_pipeline(
    dependencies: CycleExecutionDependencies[RuntimeT, RepairT],
    ctx: CycleContext,
    issues: IssuesByStatus,
    run_state: RunState,
    config: DispatcherConfig,
    now: float,
    repair_cycle: RepairT,
    prior_events: tuple[PriorMergeEvidenceCompletion, ...],
) -> CycleReport:
    sink = config.progress
    with progress_phase(sink, "active_worktrees"):
        active = ctx.process_active_worktrees()
        completion_events = [*prior_events, *active.completion_events]
        dependencies.notify_pr_links(ctx, config)
    with progress_phase(sink, "gc_reclaim"):
        completion_events = dependencies.run_gc(
            ctx, config, completion_events, repair_cycle
        )
    with progress_phase(sink, "reconciliation_external_locks"):
        promotion_events, lock_result = dependencies.reconcile_before_scheduling(
            ctx=ctx,
            issues=issues,
            run_state=run_state,
            config=config,
            repair_cycle=repair_cycle,
        )
    with progress_phase(sink, "scheduling"):
        scheduling = dependencies.schedule(
            ctx, lock_result, list(active.deviation_events)
        )
    return dependencies.build_report(
        scheduling,
        lock_result,
        deviation_events=list(active.deviation_events),
        completion_events=completion_events,
        promotion_events=promotion_events,
        applied=config.apply,
    )


def execute_locked_cycle(
    dependencies: CycleExecutionDependencies[RuntimeT, RepairT],
    config: DispatcherConfig,
    collector: CycleTimingCollector | None = None,
) -> CycleReport:
    sink = config.progress
    with progress_phase(sink, "state_load"):
        run_state = dependencies.load_state(config.run_state_path)
        now = dependencies.now()
    issues, ctx, recovery_report, prior_merges = dependencies.prepare_context(
        run_state, config, now
    )
    with progress_phase(sink, "consistency"):
        runtime = dependencies.start_consistency(config, run_state, issues, ctx)
    repair_cycle = dependencies.new_repair_state()
    repair_cycle.add_report(recovery_report)
    report = dependencies.run_pipeline(
        ctx, issues, run_state, config, now, repair_cycle, prior_merges.events
    )
    with progress_phase(sink, "consistency_postprocessing"):
        dependencies.finish_consistency(runtime, report, ctx, now, config, repair_cycle)
    record_cycle_events(dependencies, report, now, config, collector)
    return report


def record_cycle_events(
    dependencies: CycleExecutionDependencies[RuntimeT, RepairT],
    report: CycleReport,
    now: float,
    config: DispatcherConfig,
    collector: CycleTimingCollector | None = None,
) -> None:
    """Append the cycle to the confirmed event log, then settle promotion reports.

    The unreported promotion records are consumed only after the append
    succeeded; an interrupted cycle is re-delivered (#1296).
    """
    sink = config.progress
    if not config.apply:
        sink.emit("events_record", "skipped", reason="dry_run")
        return
    with progress_phase(sink, "events_record"):
        entry = dependencies.build_event_log(report, now)
        entry["timings"] = timing_snapshot(collector)
        dependencies.append_event_log(entry, config.events_log_path)
        confirm_promotion_reports(config, report.promotion_events)


def execute_cycle(
    dependencies: CycleExecutionDependencies[RuntimeT, RepairT],
    config: DispatcherConfig,
) -> CycleReport:
    collector = None
    try:
        candidate = CycleTimingCollector()
        config = replace(config, progress=TimingProgress(config.progress, candidate))
        collector = candidate
    except Exception:
        pass
    sink = config.progress
    with command_observer_scope(collector), progress_phase(sink, "cycle"):
        with ExitStack() as stack:
            with progress_phase(sink, "state_lock"):
                stack.enter_context(
                    dependencies.state_lock(
                        Path(config.run_state_path).with_suffix(".lock")
                    )
                )
            return execute_locked_cycle(dependencies, config, collector)
