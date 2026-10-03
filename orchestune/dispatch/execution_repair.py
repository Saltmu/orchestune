"""Dispatch adapter for consistency-kernel execution repair plans.

The adapter normalizes one repository-wide cycle snapshot, asks the pure
execution invariants and planner for typed commands, and performs no mutation.
Recovery and GC remain the only side-effect boundaries that execute the plan.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from orchestune.claim.contracts import ClaimStage
from orchestune.consistency.desired import (
    DesiredTaskInput,
    DispatchPolicy,
    TaskLifecycle,
    derive_desired_repository_state,
)
from orchestune.consistency.invariants.execution import (
    DISPATCH_PRELAUNCH_ORPHAN,
    EXECUTION_TIMED_OUT,
    HANDLELESS_EXECUTION_ORPHAN,
    LOCAL_PROCESS_DEAD,
)
from orchestune.consistency.models import (
    ConsistencyScope,
    DesiredRepositoryState,
    ObservedRepositoryState,
    RepairCommand,
    RepairResult,
    RepairStatus,
)
from orchestune.consistency.observation import (
    EXECUTION_KIND_CLOUD,
    EXECUTION_KIND_LOCAL,
    ExecutionRecord,
    ForgeSnapshot,
    ObservationCollector,
)
from orchestune.consistency.repairs.execution import (
    COMMAND_BOOKKEEPING,
    COMMAND_RECLAIM,
    COMMAND_REQUEUE,
)
from orchestune.consistency.repairs.status import (
    COMMAND_ADD_LABEL,
    COMMAND_REMOVE_LABEL,
    COMMAND_TRANSITION_LABEL,
)
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.external_execution import probe_runtime_status
from orchestune.infra.process_utils import is_process_alive
from orchestune.labels import StatusLabel
from orchestune.ledger.completion_reservations import completion_mutation_blocked_fresh
from orchestune.ledger.run_state import ActiveWorktree, RunState
from orchestune.models import IssueRecord, PrRecord
from orchestune.task_metadata import TaskMetadata


class RepairCommandDomain(StrEnum):
    """Side-effect boundary that owns one typed repair command."""

    STATUS = "status"
    EXECUTION = "execution"
    RECOVERY = "recovery"
    GC = "gc"


class RepairCommandOperation(StrEnum):
    """Existing low-level operation reused by one typed command."""

    FORGE_ADD_LABEL = "forge.add-label"
    FORGE_REMOVE_LABEL = "forge.remove-label"
    FORGE_TRANSITION_LABEL = "forge.transition-label"
    GC_RECLAIM_LIFECYCLE = "gc.reclaim-lifecycle"
    GC_REQUEUE_NOTIFICATION = "gc.requeue-notification"
    RECOVERY_BOOKKEEPING = "recovery.update-bookkeeping"


@dataclass(frozen=True, slots=True)
class RepairCommandBinding:
    """Stable ownership metadata for one automatic repair command."""

    domain: RepairCommandDomain
    operation: RepairCommandOperation


REPAIR_COMMAND_BINDINGS: Mapping[str, RepairCommandBinding] = MappingProxyType(
    {
        COMMAND_ADD_LABEL: RepairCommandBinding(
            RepairCommandDomain.STATUS,
            RepairCommandOperation.FORGE_ADD_LABEL,
        ),
        COMMAND_REMOVE_LABEL: RepairCommandBinding(
            RepairCommandDomain.STATUS,
            RepairCommandOperation.FORGE_REMOVE_LABEL,
        ),
        COMMAND_TRANSITION_LABEL: RepairCommandBinding(
            RepairCommandDomain.STATUS,
            RepairCommandOperation.FORGE_TRANSITION_LABEL,
        ),
        COMMAND_RECLAIM: RepairCommandBinding(
            RepairCommandDomain.GC,
            RepairCommandOperation.GC_RECLAIM_LIFECYCLE,
        ),
        COMMAND_REQUEUE: RepairCommandBinding(
            RepairCommandDomain.EXECUTION,
            RepairCommandOperation.GC_REQUEUE_NOTIFICATION,
        ),
        COMMAND_BOOKKEEPING: RepairCommandBinding(
            RepairCommandDomain.RECOVERY,
            RepairCommandOperation.RECOVERY_BOOKKEEPING,
        ),
    }
)

type RepairCommandHandler = Callable[[RepairCommand], RepairResult]


def _failed_result(command: RepairCommand, diagnostic: str) -> RepairResult:
    return RepairResult(
        command=command,
        status=RepairStatus.FAILED,
        diagnostics=(diagnostic,),
    )


def _describe_exception(exc: Exception) -> str:
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


@dataclass(frozen=True, slots=True)
class DispatchRepairExecutorAdapter:
    """Route typed commands to bound domain handlers and fail closed."""

    handlers: Mapping[str, RepairCommandHandler]

    def __post_init__(self) -> None:
        object.__setattr__(self, "handlers", MappingProxyType(dict(self.handlers)))

    def execute(self, command: RepairCommand) -> RepairResult:
        binding = REPAIR_COMMAND_BINDINGS.get(command.code)
        if binding is None:
            return _failed_result(
                command, f"unsupported repair command: {command.code}"
            )
        handler = self.handlers.get(command.code)
        if handler is None:
            return _failed_result(
                command,
                f"unbound {binding.domain.value} repair command: {command.code}",
            )
        try:
            result = handler(command)
        except Exception as exc:  # noqa: BLE001 - report through RepairResult
            return _failed_result(command, _describe_exception(exc))
        if result.command != command:
            return _failed_result(
                command, "repair handler returned a result for another command"
            )
        return result


class _WorktreeProbe:
    def worktree_exists(self, path: str) -> bool:
        return Path(path).exists()


class _ProcessProbe:
    def is_alive(self, pid: int) -> bool:
        return is_process_alive(pid)


@dataclass(frozen=True, slots=True)
class _ExternalExecutionProbe:
    config: DispatcherConfig

    def status(self, external_id: str) -> str:
        """外部実行の実行状態（running/stopped/unknown）。成果物の完了判定ではない。"""
        return probe_runtime_status(self.config, external_id)


def _repository_id() -> str:
    return os.environ.get("GITHUB_REPOSITORY") or "orchestune-repository"


def _task_issue(task: TaskMetadata) -> IssueRecord:
    parent: dict[str, int | str] | None = None
    if task.parent_number is not None:
        parent = {"number": task.parent_number}
        if task.parent_state is not None:
            parent["state"] = task.parent_state
    return IssueRecord(
        number=task.issue_number,
        title=task.subtask_id or f"Issue {task.issue_number}",
        body="",
        labels=task.status_labels,
        created_at=task.created_at,
        state=task.issue_state,
        parent=parent,
    )


def _task_lifecycle(task: TaskMetadata) -> TaskLifecycle:
    if StatusLabel.DONE in task.status_labels:
        return TaskLifecycle.DONE
    if StatusLabel.NOT_NEEDED in task.status_labels:
        return TaskLifecycle.NOT_NEEDED
    if any(
        label in task.status_labels
        for label in (
            StatusLabel.BLOCKED_HUMAN_REVIEW,
            StatusLabel.MANUAL_MERGE_REQUIRED,
        )
    ):
        return TaskLifecycle.HUMAN_REVIEW
    return TaskLifecycle.OPEN


def _desired_tasks(
    tasks_by_issue: Mapping[int, TaskMetadata],
) -> tuple[DesiredTaskInput, ...]:
    return tuple(
        DesiredTaskInput(
            task_id=f"issue-{task.issue_number}",
            subject_id=str(task.issue_number),
            footprint=task.footprint,
            lifecycle=_task_lifecycle(task),
        )
        for task in sorted(tasks_by_issue.values(), key=lambda item: item.issue_number)
    )


def _active_task_ids(tasks_by_issue: Mapping[int, TaskMetadata]) -> tuple[str, ...]:
    return tuple(
        f"issue-{task.issue_number}"
        for task in sorted(tasks_by_issue.values(), key=lambda item: item.issue_number)
        if StatusLabel.IN_PROGRESS in task.status_labels
    )


def execution_record_from_active(active: ActiveWorktree) -> ExecutionRecord:
    """Project an ActiveWorktree onto the consistency ExecutionRecord boundary."""
    core = active.core
    launch = active.launch
    claim = active.claim
    return ExecutionRecord(
        issue_number=core.issue_number,
        branch=core.branch,
        worktree_path=core.worktree_path,
        pid=launch.pid,
        external_id=launch.external_id,
        started_at=launch.started_at,
        kind=(
            EXECUTION_KIND_CLOUD
            if launch.external_id is not None
            else EXECUTION_KIND_LOCAL
            if launch.pid is not None
            else None
        ),
        owner_kind=claim.owner_kind,
        claim_id=claim.claim_id,
        claim_stage=claim.claim_stage,
        launch_phase=launch.launch_phase,
    )


def _execution_records(run_state: RunState) -> tuple[ExecutionRecord, ...]:
    return tuple(
        execution_record_from_active(active)
        for _, active in sorted(run_state.active_worktrees.items())
    )


def _branches_by_issue(
    run_state: RunState, declared: Mapping[int, str] | None
) -> dict[int, str]:
    branches = {
        active.core.issue_number: active.core.branch
        for active in run_state.active_worktrees.values()
    }
    for issue_number, branch in (declared or {}).items():
        branches.setdefault(issue_number, branch)
    return branches


def command_finding_codes(command: RepairCommand) -> tuple[str, ...]:
    """Return the stable finding codes carried by a typed command."""
    values = dict(command.parameters).get("finding_codes", ())
    if not isinstance(values, tuple):
        return ()
    return tuple(code for code in values if isinstance(code, str))


@dataclass(frozen=True, slots=True)
class ReclaimPrecondition:
    """Fresh execution facts retained for the low-level reclaim lifecycle."""

    active: ActiveWorktree
    observed_at: float
    process_alive: bool
    timed_out: bool


def _timed_out(
    active: ActiveWorktree,
    finding_codes: frozenset[str],
    config: DispatcherConfig,
    observed_at: float,
) -> bool:
    started_at = active.launch.started_at
    return bool(
        EXECUTION_TIMED_OUT in finding_codes
        and started_at is not None
        and config.task_timeout_seconds > 0
        and observed_at - started_at > config.task_timeout_seconds
    )


def _dead_local_process(
    active: ActiveWorktree,
    finding_codes: frozenset[str],
    config: DispatcherConfig,
    process_alive: bool,
) -> bool:
    launch = active.launch
    return bool(
        LOCAL_PROCESS_DEAD in finding_codes
        and launch.external_id is None
        and launch.pid is not None
        and not process_alive
        and config.zombie_gc
    )


def _handleless_orphan(
    active: ActiveWorktree,
    finding_codes: frozenset[str],
    config: DispatcherConfig,
) -> bool:
    launch = active.launch
    core = active.core
    return bool(
        HANDLELESS_EXECUTION_ORPHAN in finding_codes
        and launch.pid is None
        and launch.external_id is None
        and launch.started_at is None
        and not os.path.exists(core.worktree_path)
        and config.zombie_gc
    )


def _dispatch_prelaunch_orphan(
    active: ActiveWorktree,
    finding_codes: frozenset[str],
    config: DispatcherConfig,
) -> bool:
    launch = active.launch
    claim = active.claim
    core = active.core
    return bool(
        DISPATCH_PRELAUNCH_ORPHAN in finding_codes
        and config.zombie_gc
        and launch.pid is None
        and launch.external_id is None
        and launch.started_at is None
        and os.path.exists(core.worktree_path)
        and claim.owner_kind == "dispatch"
        and bool(claim.claim_id and claim.claim_id.strip())
        and isinstance(claim.claim_stage, str)
        and claim.claim_stage.lower()
        in {ClaimStage.ACTIVE_SAVED.value, ClaimStage.COMPLETED.value}
        and launch.launch_phase in {None, "failed"}
    )


def revalidate_reclaim_preconditions(
    command: RepairCommand,
    run_state: RunState,
    *,
    key: str,
    expected_active: ActiveWorktree,
    expected_finding_codes: tuple[str, ...],
    config: DispatcherConfig,
    held_worktree_paths: frozenset[str],
    now: float | None = None,
) -> ReclaimPrecondition | None:
    """Return fresh known facts only while the typed command is still safe."""
    if completion_mutation_blocked_fresh(
        run_state, expected_active.core.issue_number, config.run_state_path
    ):
        return None
    active = run_state.active_worktrees.get(key)
    finding_codes = frozenset(command_finding_codes(command))
    if (
        active is None
        or command.subject_id != str(expected_active.core.issue_number)
        or active != expected_active
        or not finding_codes.intersection(expected_finding_codes)
        or active.core.worktree_path in held_worktree_paths
    ):
        return None
    observed_at = time.time() if now is None else now
    pid = active.launch.pid
    process_alive = bool(pid is not None and is_process_alive(pid))
    timed_out = _timed_out(active, finding_codes, config, observed_at)
    if not (
        timed_out
        or _dead_local_process(active, finding_codes, config, process_alive)
        or _handleless_orphan(active, finding_codes, config)
        or _dispatch_prelaunch_orphan(active, finding_codes, config)
    ):
        return None
    return ReclaimPrecondition(active, observed_at, process_alive, timed_out)


def collect_execution_observed_state(
    run_state: RunState,
    tasks_by_issue: Mapping[int, TaskMetadata],
    config: DispatcherConfig,
    open_prs: Sequence[PrRecord],
    branches_by_issue: Mapping[int, str] | None,
    repository_id: str,
    observed_at: datetime,
) -> ObservedRepositoryState:
    collector = ObservationCollector(
        repository_id=repository_id,
        worktree_probe=_WorktreeProbe(),
        process_probe=_ProcessProbe(),
        external_probe=_ExternalExecutionProbe(config),
        clock=lambda: observed_at,
    )
    issues = tuple(
        _task_issue(task)
        for task in sorted(tasks_by_issue.values(), key=lambda item: item.issue_number)
    )
    return collector.collect(
        forge=ForgeSnapshot(
            issues=issues,
            pull_requests=tuple(open_prs),
            fetched_at=observed_at,
        ),
        executions=_execution_records(run_state),
        branches_by_issue=_branches_by_issue(run_state, branches_by_issue),
    )


def derive_execution_desired_state(
    tasks_by_issue: Mapping[int, TaskMetadata],
    config: DispatcherConfig,
    repository_id: str,
    observed_at: datetime,
) -> DesiredRepositoryState:
    return derive_desired_repository_state(
        repository_id,
        _desired_tasks(tasks_by_issue),
        active_task_ids=_active_task_ids(tasks_by_issue),
        policy=DispatchPolicy(
            max_concurrent=config.max_concurrent,
            task_timeout_seconds=config.task_timeout_seconds,
            zombie_gc_enabled=config.zombie_gc,
        ),
        now=observed_at,
    )


__all__ = [
    "REPAIR_COMMAND_BINDINGS",
    "DispatchRepairExecutorAdapter",
    "RepairCommandBinding",
    "RepairCommandDomain",
    "RepairCommandOperation",
    "RepairCommandHandler",
    "ReclaimPrecondition",
    "collect_execution_observed_state",
    "command_finding_codes",
    "derive_execution_desired_state",
    "execution_record_from_active",
    "revalidate_reclaim_preconditions",
]


def skipped_repair(command: RepairCommand, detail: str) -> RepairResult:
    return RepairResult(
        command=command,
        status=RepairStatus.SKIPPED,
        diagnostics=(detail,),
    )


def consistency_scope_order(scope: ConsistencyScope) -> int:
    return (
        ConsistencyScope.REPOSITORY,
        ConsistencyScope.PARENT,
        ConsistencyScope.TASK,
    ).index(scope)


def restorable_active(candidate: ActiveWorktree) -> bool:
    return (
        candidate.launch.external_id is not None
        or candidate.claim.owner_kind == "interactive"
    )


def is_interactive_restoration(
    subject_id: str | None, restorations: Sequence[tuple[str, str, ActiveWorktree]]
) -> bool:
    if subject_id is None:
        return False
    return any(
        item[0] == subject_id and item[2].claim.owner_kind == "interactive"
        for item in restorations
    )
