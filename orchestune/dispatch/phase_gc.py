"""Supervisor-owned maintenance GC for zombie and timed-out executions."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from orchestune.consistency.engine import ConsistencyEngine
from orchestune.consistency.invariants.execution import execution_invariants
from orchestune.consistency.models import (
    ConsistencyReport,
    DesiredRepositoryState,
    ObservedRepositoryState,
    RepairCommand,
    RepairResult,
    RepairStatus,
)
from orchestune.consistency.repairs.execution import (
    COMMAND_RECLAIM,
    plan_execution_repairs,
)
from orchestune.consistency.supervisor import (
    ConsistencyCycleReport,
    ConsistencyMode,
    ConsistencySupervisor,
)
from orchestune.dispatch.config import (
    DEFAULT_SELF_HEALING_REPAIR_ALLOWLIST,
    DispatcherConfig,
)
from orchestune.dispatch.execution_repair import (
    DispatchRepairExecutorAdapter,
    collect_execution_observed_state,
    derive_execution_desired_state,
)
from orchestune.dispatch.gc.completion import is_completion_hold_event
from orchestune.dispatch.gc.zombies import (
    ZombieOrTimeoutReclaim,
    _preview_reclaim_event,
    _reclaim_candidate_from_command,
    execute_reclaim_repair_command,
)
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import RunState
from orchestune.models import PrRecord


@dataclass(frozen=True, slots=True)
class GcPhaseResult:
    """Events and typed repair audit data produced at the GC boundary."""

    completion_events: list[dict]
    consistency: ConsistencyCycleReport


@dataclass(frozen=True, slots=True)
class _FunctionPlanner:
    function: Callable[[ConsistencyReport], tuple[RepairCommand, ...]]

    def plan(self, report: ConsistencyReport) -> tuple[RepairCommand, ...]:
        return self.function(report)


@dataclass(frozen=True, slots=True)
class _GcReclaimAdapter:
    run_state: RunState
    tasks_by_issue: dict[int, Task]
    config: DispatcherConfig
    open_prs: tuple[PrRecord, ...]
    now: float | None

    def observe(self) -> ObservedRepositoryState:
        observed_at = (
            datetime.now(UTC)
            if self.now is None
            else datetime.fromtimestamp(self.now, UTC)
        )
        return collect_execution_observed_state(
            self.run_state,
            self.tasks_by_issue,
            self.config,
            self.open_prs,
            branches_by_issue=None,
            repository_id=_repository_id(),
            observed_at=observed_at,
        )

    def derive(self, observed: ObservedRepositoryState) -> DesiredRepositoryState:
        return derive_execution_desired_state(
            self.tasks_by_issue,
            self.config,
            observed.repository_id,
            observed.observed_at,
        )


def _repository_id() -> str:
    return os.environ.get("GITHUB_REPOSITORY") or "orchestune-repository"


def _gc_supervisor() -> ConsistencySupervisor:
    return ConsistencySupervisor(
        repository_id=_repository_id(),
        engine=ConsistencyEngine(execution_invariants()),
        repair_planners=(_FunctionPlanner(plan_execution_repairs),),
    )


def _held_worktree_paths(completion_events: Sequence[dict]) -> frozenset[str]:
    return frozenset(
        event["worktree_path"]
        for event in completion_events
        if is_completion_hold_event(event) and event.get("worktree_path")
    )


def _planned_reclaims(
    commands: Sequence[RepairCommand],
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
    now: float | None,
) -> dict[str, ZombieOrTimeoutReclaim]:
    active_by_subject = {
        str(active.issue_number): (key, replace(active))
        for key, active in run_state.active_worktrees.items()
    }
    observed_at = time.time() if now is None else now
    planned = (
        _reclaim_candidate_from_command(
            command,
            active_by_subject,
            tasks_by_issue,
            run_state,
            config.max_task_reclaims,
            observed_at,
        )
        for command in commands
    )
    return {
        str(reclaim.active.issue_number): reclaim
        for reclaim in planned
        if reclaim is not None
    }


def _reclaim_handler(
    planned: dict[str, ZombieOrTimeoutReclaim],
    run_state: RunState,
    config: DispatcherConfig,
    open_prs: tuple[PrRecord, ...],
    held_paths: frozenset[str],
    events: list[dict],
    now: float | None,
) -> Callable[[RepairCommand], RepairResult]:
    def execute(command: RepairCommand) -> RepairResult:
        reclaim = planned.get(command.subject_id or "")
        if reclaim is None:
            return RepairResult(
                command=command,
                status=RepairStatus.SKIPPED,
                diagnostics=("no planned GC reclaim matches the typed command",),
            )
        if not config.apply and reclaim.active.worktree_path not in held_paths:
            events.append(_preview_reclaim_event(reclaim))
        return execute_reclaim_repair_command(
            command,
            run_state,
            reclaim,
            config,
            open_prs,
            held_worktree_paths=held_paths,
            now=now,
            event_sink=events.append,
        )

    return execute


def run_gc_phase(
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
    completion_events: list[dict],
    open_prs: Sequence[PrRecord] | None = None,
    *,
    now: float | None = None,
) -> GcPhaseResult:
    observed_now = time.time() if now is None else now
    prs = tuple(open_prs or ())
    adapter = _GcReclaimAdapter(run_state, tasks_by_issue, config, prs, observed_now)
    supervisor = _gc_supervisor()
    initial_scan = supervisor.full_scan("gc-reclaim", observer=adapter, deriver=adapter)
    events: list[dict] = []
    planned = _planned_reclaims(
        initial_scan.repair_candidates,
        run_state,
        tasks_by_issue,
        config,
        observed_now,
    )
    executor = DispatchRepairExecutorAdapter(
        {
            COMMAND_RECLAIM: _reclaim_handler(
                planned,
                run_state,
                config,
                prs,
                _held_worktree_paths(completion_events),
                events,
                observed_now,
            )
        }
    )
    supervisor.repair_until_stable(
        initial_scan,
        observer=adapter,
        deriver=adapter,
        executor=executor,
        allowlist=DEFAULT_SELF_HEALING_REPAIR_ALLOWLIST & {COMMAND_RECLAIM},
        max_passes=1,
    )
    return GcPhaseResult(
        completion_events=[*completion_events, *events],
        consistency=supervisor.cycle_report(mode=ConsistencyMode.REPAIR),
    )


__all__ = ["GcPhaseResult", "run_gc_phase"]
