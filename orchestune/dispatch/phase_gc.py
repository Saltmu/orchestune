"""Supervisor-owned maintenance GC for stale, zombie, and timed-out executions."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from orchestune.consistency.engine import ConsistencyEngine
from orchestune.consistency.invariants.execution import (
    RUN_STATE_STALE,
    execution_invariants,
)
from orchestune.consistency.models import (
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
    FunctionRepairPlanner,
)
from orchestune.dispatch.config import (
    DEFAULT_SELF_HEALING_REPAIR_ALLOWLIST,
    DispatcherConfig,
)
from orchestune.dispatch.execution_repair import (
    DispatchRepairExecutorAdapter,
    RepairCommandHandler,
    collect_execution_observed_state,
    command_finding_codes,
    derive_execution_desired_state,
)
from orchestune.dispatch.gc import _apply_stale_active_entry_discard
from orchestune.dispatch.gc.completion import is_completion_hold_event
from orchestune.dispatch.gc.zombies import (
    ZombieOrTimeoutReclaim,
    _preview_reclaim_event,
    _reclaim_candidate_from_command,
    execute_reclaim_repair_command,
)
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.labels import StatusLabel
from orchestune.models import PrRecord


@dataclass(frozen=True, slots=True)
class GcPhaseResult:
    """Events and typed repair audit data produced at the GC boundary."""

    completion_events: list[dict]
    consistency: ConsistencyCycleReport


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
        repair_planners=(FunctionRepairPlanner(plan_execution_repairs),),
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


def _stale_active_entry(
    command: RepairCommand,
    run_state: RunState,
) -> tuple[str, ActiveWorktree] | None:
    if (
        command.code != COMMAND_RECLAIM
        or command.subject_id is None
        or RUN_STATE_STALE not in command_finding_codes(command)
    ):
        return None
    return next(
        (
            (key, active)
            for key, active in run_state.active_worktrees.items()
            if str(active.issue_number) == command.subject_id
        ),
        None,
    )


def _stale_discard_event(active: ActiveWorktree, task: Task, reason: str) -> dict:
    return {
        "issue_number": active.issue_number,
        "subtask_id": task.subtask_id,
        "action": "stale_active_entry_discarded",
        "reason": reason,
    }


def _execute_stale_reclaim(
    command: RepairCommand,
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
    events: list[dict],
) -> RepairResult | None:
    resolved = _stale_active_entry(command, run_state)
    if resolved is None:
        return None
    key, active = resolved
    task = tasks_by_issue.get(active.issue_number)
    if task is None:
        return RepairResult(
            command=command,
            status=RepairStatus.SKIPPED,
            diagnostics=("stale cleanup subject is not an observed task",),
        )

    issue_state = config.resolved_forge.get_issue_state(active.issue_number)
    live_labels = tuple(config.resolved_forge.get_issue_labels(active.issue_number))
    if issue_state.upper() == "OPEN" and StatusLabel.IN_PROGRESS in live_labels:
        return RepairResult(
            command=command,
            status=RepairStatus.SKIPPED,
            diagnostics=(
                "stale cleanup precondition no longer holds: "
                "status:in-progress is live",
            ),
        )

    reason = (
        f"issue label is no longer status:in-progress (labels={sorted(live_labels)})"
    )
    discarded = _apply_stale_active_entry_discard(
        run_state, key, active, reason, config
    )
    if not discarded:
        return RepairResult(
            command=command,
            status=RepairStatus.SKIPPED,
            diagnostics=("stale cleanup deferred by existing safety boundary",),
        )
    events.append(_stale_discard_event(active, task, reason))
    return RepairResult(
        command=command,
        status=RepairStatus.APPLIED if config.apply else RepairStatus.SKIPPED,
    )


def build_gc_reclaim_handler(
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
    completion_events: list[dict],
    open_prs: Sequence[PrRecord] | None = None,
    *,
    event_sink: list[dict] | None = None,
    now: float | None = None,
) -> RepairCommandHandler:
    """Bind the shared typed reclaim command to the guarded GC lifecycle."""
    observed_now = time.time() if now is None else now
    prs = tuple(open_prs or ())
    held_paths = _held_worktree_paths(completion_events)
    events = completion_events if event_sink is None else event_sink

    def execute(command: RepairCommand) -> RepairResult:
        stale_result = _execute_stale_reclaim(
            command, run_state, tasks_by_issue, config, events
        )
        if stale_result is not None:
            return stale_result
        planned = _planned_reclaims(
            (command,), run_state, tasks_by_issue, config, observed_now
        )
        return _reclaim_handler(
            planned,
            run_state,
            config,
            prs,
            held_paths,
            events,
            observed_now,
        )(command)

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
    executor = DispatchRepairExecutorAdapter(
        {
            COMMAND_RECLAIM: build_gc_reclaim_handler(
                run_state,
                tasks_by_issue,
                config,
                completion_events,
                prs,
                event_sink=events,
                now=observed_now,
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


__all__ = ["GcPhaseResult", "build_gc_reclaim_handler", "run_gc_phase"]
