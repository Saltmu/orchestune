"""Cycle orchestration with observable phase boundaries."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_execution_contracts import (
    CycleExecutionDependencies,
    RepairCycleAccumulator,
)
from orchestune.dispatch.cycle_report import CycleReport
from orchestune.dispatch.phase_scheduling import SchedulingPhaseResult
from orchestune.dispatch.progress import progress_phase
from orchestune.dispatch.promotion_reporting import confirm_promotion_reports
from orchestune.dispatch.timings import (
    CycleTimingCollector,
    TimingProgress,
    timing_snapshot,
)
from orchestune.infra.command_metrics import command_observer_scope
from orchestune.lock_contracts import ExternalLockScanResult


class CycleStage(str, Enum):
    FULL = "full"
    PRE_INTEGRATION = "pre_integration"


if TYPE_CHECKING:
    from orchestune.dispatch.cycle_context import IssuesByStatus
    from orchestune.dispatch.cycle_events import PriorMergeEvidenceCompletion
    from orchestune.dispatch.rules import CycleContext
    from orchestune.ledger.run_state import RunState


RuntimeT = TypeVar("RuntimeT")
RepairT = TypeVar("RepairT", bound=RepairCycleAccumulator)


def _build_pre_integration_report(
    dependencies: CycleExecutionDependencies[RuntimeT, RepairT],
    config: DispatcherConfig,
    active_deviation_events: list[Any],
    completion_events: list[Any],
) -> CycleReport:
    lock_result = ExternalLockScanResult(to_lock=[], to_unlock=[], conflicts={})
    scheduling = SchedulingPhaseResult(
        selected=[],
        quota_slots_available=0,
        decisions=[],
        execution_selections={},
        skips=[],
    )
    report = dependencies.build_report(
        scheduling,
        lock_result,
        deviation_events=active_deviation_events,
        completion_events=completion_events,
        promotion_events=[],
        applied=config.apply,
    )
    report.stage = "pre_integration"
    report.scheduling_deferred_reason = "pre_integration_stage"
    return report


def _execute_scheduling_pipeline(
    dependencies: CycleExecutionDependencies[RuntimeT, RepairT],
    ctx: CycleContext,
    issues: IssuesByStatus,
    run_state: RunState,
    config: DispatcherConfig,
    repair_cycle: RepairT,
    active_deviation_events: list[Any],
    completion_events: list[Any],
) -> CycleReport:
    sink = config.progress
    with progress_phase(sink, "reconciliation_external_locks"):
        promotion_events, lock_result = dependencies.reconcile_before_scheduling(
            ctx=ctx,
            issues=issues,
            run_state=run_state,
            config=config,
            repair_cycle=repair_cycle,
        )
    with progress_phase(sink, "scheduling"):
        scheduling = dependencies.schedule(ctx, lock_result, active_deviation_events)
    report = dependencies.build_report(
        scheduling,
        lock_result,
        deviation_events=active_deviation_events,
        completion_events=completion_events,
        promotion_events=promotion_events,
        applied=config.apply,
    )
    report.stage = "scheduling"
    return report


def execute_pipeline(
    dependencies: CycleExecutionDependencies[RuntimeT, RepairT],
    ctx: CycleContext,
    issues: IssuesByStatus,
    run_state: RunState,
    config: DispatcherConfig,
    now: float,
    repair_cycle: RepairT,
    prior_events: tuple[PriorMergeEvidenceCompletion, ...] = (),
    stage: CycleStage = CycleStage.FULL,
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
    active_deviations = list(active.deviation_events)
    if stage == CycleStage.PRE_INTEGRATION or stage == "pre_integration":
        return _build_pre_integration_report(
            dependencies, config, active_deviations, completion_events
        )
    return _execute_scheduling_pipeline(
        dependencies,
        ctx,
        issues,
        run_state,
        config,
        repair_cycle,
        active_deviations,
        completion_events,
    )


def execute_locked_cycle(
    dependencies: CycleExecutionDependencies[RuntimeT, RepairT],
    config: DispatcherConfig,
    collector: CycleTimingCollector | None = None,
    stage: CycleStage = CycleStage.FULL,
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
    try:
        report = dependencies.run_pipeline(
            ctx,
            issues,
            run_state,
            config,
            now,
            repair_cycle,
            prior_merges.events,
            stage=stage,
        )
    except TypeError:
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
        if report.stage != "pre_integration":
            confirm_promotion_reports(config, report.promotion_events)


def execute_cycle(
    dependencies: CycleExecutionDependencies[RuntimeT, RepairT],
    config: DispatcherConfig,
    stage: CycleStage = CycleStage.FULL,
) -> CycleReport:
    collector = None
    try:
        candidate = CycleTimingCollector()
        config = replace(config, progress=TimingProgress(config.progress, candidate))
        collector = candidate
    except Exception:
        pass
    sink = config.progress
    phase_name = (
        "pre_integration_cycle"
        if stage == CycleStage.PRE_INTEGRATION or stage == "pre_integration"
        else "cycle"
    )
    with command_observer_scope(collector), progress_phase(sink, phase_name):
        with ExitStack() as stack:
            with progress_phase(sink, "state_lock"):
                stack.enter_context(
                    dependencies.state_lock(
                        Path(config.run_state_path).with_suffix(".lock")
                    )
                )
            try:
                return execute_locked_cycle(
                    dependencies, config, collector, stage=stage
                )
            except TypeError:
                return execute_locked_cycle(dependencies, config, collector)
