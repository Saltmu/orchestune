"""Typed callbacks shared by the dispatch cycle execution boundary."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

if TYPE_CHECKING:
    from orchestune.consistency.supervisor import ConsistencyCycleReport
    from orchestune.dispatch.config import DispatcherConfig
    from orchestune.dispatch.cycle_context import IssuesByStatus
    from orchestune.dispatch.cycle_events import (
        CompletionEvent,
        DeviationEvent,
        PromotionEvent,
    )
    from orchestune.dispatch.cycle_report import CycleReport
    from orchestune.dispatch.phase_scheduling import SchedulingPhaseResult
    from orchestune.dispatch.prior_parent_merge import PriorParentMergeReconciliation
    from orchestune.dispatch.rules import CycleContext
    from orchestune.ledger.run_state import RunState
    from orchestune.lock_contracts import ExternalLockScanResult


class RepairCycleAccumulator(Protocol):
    """The repair state operations cycle execution is allowed to use."""

    def add_report(self, report: ConsistencyCycleReport) -> None: ...


RepairT = TypeVar("RepairT", bound=RepairCycleAccumulator)
RuntimeT = TypeVar("RuntimeT")
RepairT_contra = TypeVar(
    "RepairT_contra", bound=RepairCycleAccumulator, contravariant=True
)


class ReconcileBeforeScheduling(Protocol[RepairT_contra]):
    """Keyword-only reconciliation callback for the cycle pipeline."""

    def __call__(
        self,
        *,
        ctx: CycleContext,
        issues: IssuesByStatus,
        run_state: RunState,
        config: DispatcherConfig,
        repair_cycle: RepairT_contra,
    ) -> tuple[list[PromotionEvent], ExternalLockScanResult]: ...


class BuildCycleReport(Protocol):
    """Keyword-aware report builder callback used after scheduling."""

    def __call__(
        self,
        scheduling: SchedulingPhaseResult,
        lock_result: ExternalLockScanResult,
        *,
        deviation_events: list[DeviationEvent],
        completion_events: list[CompletionEvent],
        promotion_events: list[PromotionEvent],
        applied: bool,
    ) -> CycleReport: ...


@dataclass(frozen=True, slots=True)
class CycleExecutionDependencies(Generic[RuntimeT, RepairT]):
    """Only the typed callbacks needed to run and persist one cycle."""

    load_state: Callable[[Path], RunState]
    now: Callable[[], float]
    state_lock: Callable[[Path], AbstractContextManager[None]]
    prepare_context: Callable[
        [RunState, DispatcherConfig, float],
        tuple[
            IssuesByStatus,
            CycleContext,
            ConsistencyCycleReport,
            PriorParentMergeReconciliation,
        ],
    ]
    start_consistency: Callable[
        [DispatcherConfig, RunState, IssuesByStatus, CycleContext], RuntimeT
    ]
    new_repair_state: Callable[[], RepairT]
    run_pipeline: Callable[..., CycleReport]
    finish_consistency: Callable[
        [RuntimeT, CycleReport, CycleContext, float, DispatcherConfig, RepairT],
        None,
    ]
    notify_pr_links: Callable[[CycleContext, DispatcherConfig], None]
    run_gc: Callable[
        [CycleContext, DispatcherConfig, list[CompletionEvent], RepairT],
        list[CompletionEvent],
    ]
    reconcile_before_scheduling: ReconcileBeforeScheduling[RepairT]
    schedule: Callable[
        [CycleContext, ExternalLockScanResult, list[DeviationEvent]],
        SchedulingPhaseResult,
    ]
    build_report: BuildCycleReport
    build_event_log: Callable[[CycleReport, float], dict[str, object]]
    append_event_log: Callable[[dict[str, object], Path], None]
