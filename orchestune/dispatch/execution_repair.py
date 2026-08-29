"""Dispatch adapter for consistency-kernel execution repair plans.

The adapter normalizes one repository-wide cycle snapshot, asks the pure
execution invariants and planner for typed commands, and performs no mutation.
Recovery and GC remain the only side-effect boundaries that execute the plan.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from orchestune.consistency.desired import (
    DesiredTaskInput,
    DispatchPolicy,
    TaskLifecycle,
    derive_desired_repository_state,
)
from orchestune.consistency.engine import ConsistencyEngine
from orchestune.consistency.invariants.execution import execution_invariants
from orchestune.consistency.models import (
    ConsistencyReport,
    DesiredRepositoryState,
    ObservedRepositoryState,
    RepairCommand,
)
from orchestune.consistency.observation import (
    EXECUTION_KIND_CLOUD,
    EXECUTION_KIND_LOCAL,
    ExecutionRecord,
    ForgeSnapshot,
    ObservationCollector,
)
from orchestune.consistency.repairs.execution import plan_execution_repairs
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import RunState
from orchestune.dispatch.targets import DispatchHandle
from orchestune.infra.process_utils import is_process_alive
from orchestune.models import IssueRecord, PrRecord


@dataclass(frozen=True, slots=True)
class ExecutionRepairEvaluation:
    """Immutable findings and typed commands for one observed snapshot."""

    report: ConsistencyReport
    commands: tuple[RepairCommand, ...]


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
        assert self.config.dispatch_target is not None
        return self.config.dispatch_target.completion_status(
            DispatchHandle(external_id=external_id),
            forge=self.config.resolved_forge,
        )


def _repository_id() -> str:
    return os.environ.get("GITHUB_REPOSITORY") or "orchestune-repository"


def _task_issue(task: Task) -> IssueRecord:
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


def _task_lifecycle(task: Task) -> TaskLifecycle:
    if "status:done" in task.status_labels:
        return TaskLifecycle.DONE
    if "status:not-needed" in task.status_labels:
        return TaskLifecycle.NOT_NEEDED
    if any(
        label in task.status_labels
        for label in ("status:blocked-human-review", "status:manual-merge-required")
    ):
        return TaskLifecycle.HUMAN_REVIEW
    return TaskLifecycle.OPEN


def _desired_tasks(tasks_by_issue: Mapping[int, Task]) -> tuple[DesiredTaskInput, ...]:
    return tuple(
        DesiredTaskInput(
            task_id=f"issue-{task.issue_number}",
            subject_id=str(task.issue_number),
            footprint=task.footprint,
            lifecycle=_task_lifecycle(task),
        )
        for task in sorted(tasks_by_issue.values(), key=lambda item: item.issue_number)
    )


def _active_task_ids(tasks_by_issue: Mapping[int, Task]) -> tuple[str, ...]:
    return tuple(
        f"issue-{task.issue_number}"
        for task in sorted(tasks_by_issue.values(), key=lambda item: item.issue_number)
        if "status:in-progress" in task.status_labels
    )


def _execution_records(run_state: RunState) -> tuple[ExecutionRecord, ...]:
    return tuple(
        ExecutionRecord(
            issue_number=active.issue_number,
            branch=active.branch,
            worktree_path=active.worktree_path,
            pid=active.pid,
            external_id=active.external_id,
            started_at=active.started_at,
            kind=(
                EXECUTION_KIND_CLOUD
                if active.external_id is not None
                else EXECUTION_KIND_LOCAL
                if active.pid is not None
                else None
            ),
        )
        for _, active in sorted(run_state.active_worktrees.items())
    )


def _branches_by_issue(
    run_state: RunState, declared: Mapping[int, str] | None
) -> dict[int, str]:
    branches = {
        active.issue_number: active.branch
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


def _collect_observed_state(
    run_state: RunState,
    tasks_by_issue: Mapping[int, Task],
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


def _derive_desired_state(
    tasks_by_issue: Mapping[int, Task],
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


def evaluate_execution_repair_plan(
    run_state: RunState,
    tasks_by_issue: Mapping[int, Task],
    config: DispatcherConfig,
    *,
    open_prs: Sequence[PrRecord] = (),
    branches_by_issue: Mapping[int, str] | None = None,
    held_issue_numbers: Iterable[int] = (),
    now: float | None = None,
) -> ExecutionRepairEvaluation:
    """Observe, evaluate, and plan execution repairs without applying them."""
    observed_at = datetime.now(UTC) if now is None else datetime.fromtimestamp(now, UTC)
    repository_id = _repository_id()
    observed = _collect_observed_state(
        run_state,
        tasks_by_issue,
        config,
        open_prs,
        branches_by_issue,
        repository_id,
        observed_at,
    )
    desired = _derive_desired_state(
        tasks_by_issue,
        config,
        repository_id,
        observed_at,
    )
    report = ConsistencyEngine(execution_invariants()).evaluate(observed, desired)
    held_subjects = {str(issue_number) for issue_number in held_issue_numbers}
    commands = tuple(
        command
        for command in plan_execution_repairs(report)
        if command.subject_id not in held_subjects
    )
    return ExecutionRepairEvaluation(report=report, commands=commands)


__all__ = [
    "ExecutionRepairEvaluation",
    "command_finding_codes",
    "evaluate_execution_repair_plan",
]
