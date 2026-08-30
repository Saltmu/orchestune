"""Apply consistency-kernel status repairs at existing dispatch boundaries."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from orchestune.consistency.desired import (
    DesiredTaskInput,
    DispatchPolicy,
    TaskLifecycle,
    derive_desired_repository_state,
)
from orchestune.consistency.engine import ConsistencyEngine
from orchestune.consistency.intents import IntentJournal
from orchestune.consistency.invariants.status import (
    BLOCKED_WITH_RESOLVED_DEPENDENCIES,
    PRIMARY_STATUS_CONFLICT,
    PROMOTION_HOLD_LABELS,
    primary_status_labels,
    status_invariants,
)
from orchestune.consistency.models import (
    ConsistencyReport,
    ConsistencyScope,
    DesiredFact,
    IntentStatus,
    RepairCommand,
    RepairResult,
    RepairStatus,
    TransitionIntent,
)
from orchestune.consistency.observation import ForgeSnapshot, ObservationCollector
from orchestune.consistency.repairs.status import (
    COMMAND_ADD_LABEL,
    COMMAND_REMOVE_LABEL,
    COMMAND_TRANSITION_LABEL,
    plan_status_repairs,
)
from orchestune.consistency.vocabulary import DESIRED_STATUS_LABEL
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.labels import transition_status_label
from orchestune.dispatch.scoring import Task
from orchestune.models import IssueRecord


class StatusRepairPhase(StrEnum):
    """Existing phase boundaries that may execute status repair commands."""

    BLOCKED_PROMOTION = "blocked-promotion"
    DUAL_STATUS = "dual-status"
    CLOSED_LOOP = "closed-loop"


@dataclass(frozen=True, slots=True)
class StatusRepairEvaluation:
    """Immutable status findings and typed commands for one task snapshot."""

    report: ConsistencyReport
    commands: tuple[RepairCommand, ...]


def _repository_id() -> str:
    return os.environ.get("GITHUB_REPOSITORY") or "orchestune-repository"


def _observed_issue(task: Task) -> IssueRecord:
    parent = None if task.parent_number is None else {"number": task.parent_number}
    return IssueRecord(
        number=task.issue_number,
        title=task.subtask_id or f"Issue {task.issue_number}",
        body="",
        labels=task.status_labels,
        created_at=task.created_at,
        state=task.issue_state,
        parent=parent,
    )


def task_lifecycle(
    status_labels: tuple[str, ...], *, completed: bool = False
) -> TaskLifecycle:
    """Resolve lifecycle with an explicit same-cycle completion override."""
    if completed:
        return TaskLifecycle.DONE
    # Preserve the dispatch adapter's existing lifecycle precedence everywhere
    # except the one interrupted rollback this phase owns.  In `done + queued`,
    # queued is the durable destination and done is the label to remove.
    if "status:done" in status_labels and "status:queued" in status_labels:
        return TaskLifecycle.OPEN
    if "status:done" in status_labels:
        return TaskLifecycle.DONE
    if "status:not-needed" in status_labels:
        return TaskLifecycle.NOT_NEEDED
    if any(
        label in status_labels
        for label in ("status:blocked-human-review", "status:manual-merge-required")
    ):
        return TaskLifecycle.HUMAN_REVIEW
    return TaskLifecycle.OPEN


def _desired_tasks(
    tasks_by_issue: Mapping[int, Task], completed_subtask_ids: frozenset[str]
) -> tuple[DesiredTaskInput, ...]:
    return tuple(
        DesiredTaskInput(
            task_id=task.subtask_id,
            subject_id=str(task.issue_number),
            depends_on=task.depends_on,
            footprint=task.footprint,
            lifecycle=task_lifecycle(
                task.status_labels,
                completed=task.subtask_id in completed_subtask_ids,
            ),
        )
        for task in sorted(tasks_by_issue.values(), key=lambda item: item.issue_number)
        if task.subtask_id
    )


def evaluate_status_repair_plan(
    tasks_by_issue: Mapping[int, Task],
    *,
    completed_subtask_ids: Iterable[str] = (),
    intents: Iterable[TransitionIntent] = (),
    now: datetime | None = None,
) -> StatusRepairEvaluation:
    """Evaluate status invariants and plan repairs without applying mutations."""
    observed_at = datetime.now(UTC) if now is None else now
    repository_id = _repository_id()
    completed = frozenset(completed_subtask_ids)
    tasks = _desired_tasks(tasks_by_issue, completed)
    observed = ObservationCollector(
        repository_id=repository_id, clock=lambda: observed_at
    ).collect(
        forge=ForgeSnapshot(
            issues=tuple(
                _observed_issue(task)
                for task in sorted(
                    tasks_by_issue.values(), key=lambda item: item.issue_number
                )
            ),
            fetched_at=observed_at,
        )
    )
    desired = derive_desired_repository_state(
        repository_id,
        tasks,
        completed_task_ids=completed,
        policy=DispatchPolicy(max_concurrent=max(1, len(tasks))),
        intents=intents,
        now=observed_at,
    )
    report = ConsistencyEngine(status_invariants()).evaluate(observed, desired)
    return StatusRepairEvaluation(report=report, commands=plan_status_repairs(report))


def status_intent_journal_path(config: DispatcherConfig) -> Path:
    """Keep transition intents beside the run-state file under a stable name."""
    return Path(config.run_state_path).with_suffix(".status-intents.json")


def _parameters(command: RepairCommand) -> dict[str, object]:
    return dict(command.parameters)


def _finding_code(command: RepairCommand) -> str | None:
    value = _parameters(command).get("finding_code")
    return value if isinstance(value, str) else None


def _retained_label(command: RepairCommand) -> str | None:
    prefix = "retains-primary-status:"
    return next(
        (
            precondition.removeprefix(prefix)
            for precondition in command.preconditions
            if precondition.startswith(prefix)
        ),
        None,
    )


def _expected_label(command: RepairCommand) -> str | None:
    parameters = _parameters(command)
    if command.code == COMMAND_ADD_LABEL:
        value = parameters.get("label")
    elif command.code == COMMAND_TRANSITION_LABEL:
        value = parameters.get("new_label")
    else:
        value = _retained_label(command)
    return value if isinstance(value, str) else None


def _selected(command: RepairCommand, phase: StatusRepairPhase) -> bool:
    parameters = _parameters(command)
    if phase is StatusRepairPhase.BLOCKED_PROMOTION:
        return (
            command.code == COMMAND_TRANSITION_LABEL
            and _finding_code(command) == BLOCKED_WITH_RESOLVED_DEPENDENCIES
            and parameters.get("new_label") == "status:queued"
            and parameters.get("old_labels") == ("status:blocked",)
        )
    return (
        command.code == COMMAND_REMOVE_LABEL
        and _finding_code(command) == PRIMARY_STATUS_CONFLICT
        and parameters.get("label") == "status:done"
        and _retained_label(command) == "status:queued"
    )


def _operation(command: RepairCommand, phase: StatusRepairPhase) -> str:
    return f"{phase.value}:{command.code}:{_finding_code(command) or 'unknown'}"


def _new_intent(
    command: RepairCommand, phase: StatusRepairPhase, now: datetime
) -> TransitionIntent:
    expected = _expected_label(command)
    assert command.subject_id is not None and expected is not None
    return TransitionIntent(
        intent_id=f"status-{command.subject_id}-{uuid4().hex}",
        scope=ConsistencyScope.TASK,
        subject_id=command.subject_id,
        operation=_operation(command, phase),
        created_at=now,
        status=IntentStatus.PLANNED,
        expected_changes=(
            DesiredFact(
                name=DESIRED_STATUS_LABEL,
                value=expected,
                scope=ConsistencyScope.TASK,
                subject_id=command.subject_id,
                reason=f"execute {command.idempotency_key}",
            ),
        ),
    )


def _intent_expected_label(intent: TransitionIntent) -> str | None:
    matches = tuple(
        change.value
        for change in intent.expected_changes
        if change.scope is ConsistencyScope.TASK
        and change.subject_id == intent.subject_id
        and change.name == DESIRED_STATUS_LABEL
        and isinstance(change.value, str)
    )
    return matches[0] if len(matches) == 1 else None


def _intent_is_for_phase(intent: TransitionIntent, phase: StatusRepairPhase) -> bool:
    return intent.operation.startswith(f"{phase.value}:")


def _precondition_holds(
    precondition: str,
    *,
    task: Task,
    labels: tuple[str, ...],
    dependencies_resolved: bool,
) -> bool:
    primary = primary_status_labels(labels)
    if precondition == "finding-certainty:known" or precondition == "issue-open":
        return True
    if precondition == "absent-primary-status":
        return not primary
    if precondition == "dependencies-declared":
        return bool(task.depends_on)
    if precondition == "dependencies-resolved":
        return dependencies_resolved
    if precondition == "dependencies-unresolved":
        return not dependencies_resolved
    if precondition == "no-promotion-hold":
        return not any(label in labels for label in PROMOTION_HOLD_LABELS)
    if precondition.startswith("holds-primary-status:"):
        return precondition.removeprefix("holds-primary-status:") in primary
    if precondition.startswith("retains-primary-status:"):
        return precondition.removeprefix("retains-primary-status:") in primary
    return False


def _fresh_dependencies_resolved(
    task: Task,
    tasks_by_issue: Mapping[int, Task],
    completed_subtask_ids: frozenset[str],
    config: DispatcherConfig,
) -> bool:
    tasks_by_subtask = {
        candidate.subtask_id: candidate
        for candidate in tasks_by_issue.values()
        if candidate.subtask_id
    }
    for dependency in task.depends_on:
        if dependency not in completed_subtask_ids:
            return False
        dependency_task = tasks_by_subtask.get(dependency)
        if dependency_task is None:
            continue
        labels = config.resolved_forge.get_issue_labels(dependency_task.issue_number)
        if not any(label in labels for label in ("status:done", "status:not-needed")):
            return False
    return True


def _fresh_preconditions_hold(
    command: RepairCommand,
    task: Task,
    tasks_by_issue: Mapping[int, Task],
    completed_subtask_ids: frozenset[str],
    config: DispatcherConfig,
) -> bool:
    if config.resolved_forge.get_issue_state(task.issue_number).upper() != "OPEN":
        return False
    labels = tuple(config.resolved_forge.get_issue_labels(task.issue_number))
    dependency_preconditions = {
        "dependencies-resolved",
        "dependencies-unresolved",
    }
    dependencies_resolved = not dependency_preconditions.isdisjoint(
        command.preconditions
    ) and _fresh_dependencies_resolved(
        task,
        tasks_by_issue,
        completed_subtask_ids,
        config,
    )
    return all(
        _precondition_holds(
            precondition,
            task=task,
            labels=labels,
            dependencies_resolved=dependencies_resolved,
        )
        for precondition in command.preconditions
    )


def _apply_command(
    command: RepairCommand,
    task: Task,
    intent: TransitionIntent,
    journal: IntentJournal,
    config: DispatcherConfig,
) -> None:
    parameters = _parameters(command)
    if command.code == COMMAND_TRANSITION_LABEL:
        new_label = parameters["new_label"]
        old_labels = parameters["old_labels"]
        assert isinstance(new_label, str) and isinstance(old_labels, tuple)

        def mark_applied() -> None:
            journal.mark_applied(intent.intent_id)

        transition_status_label(
            config.resolved_forge,
            task.issue_number,
            new_label,
            tuple(label for label in old_labels if isinstance(label, str)),
            on_label_added=mark_applied,
        )
        return
    label = parameters.get("label")
    assert isinstance(label, str)
    if command.code == COMMAND_ADD_LABEL:
        config.resolved_forge.add_label(task.issue_number, label)
    else:
        config.resolved_forge.remove_label(task.issue_number, label)
    journal.mark_applied(intent.intent_id)


def _status_is_verified(
    issue_number: int, expected_label: str, config: DispatcherConfig
) -> bool:
    if config.resolved_forge.get_issue_state(issue_number).upper() != "OPEN":
        return False
    labels = tuple(config.resolved_forge.get_issue_labels(issue_number))
    return primary_status_labels(labels) == (expected_label,)


def _execute(
    command: RepairCommand,
    task: Task,
    tasks_by_issue: Mapping[int, Task],
    completed_subtask_ids: frozenset[str],
    config: DispatcherConfig,
    journal: IntentJournal,
    phase: StatusRepairPhase,
    now: datetime,
    intent: TransitionIntent | None = None,
) -> bool:
    if not _fresh_preconditions_hold(
        command, task, tasks_by_issue, completed_subtask_ids, config
    ):
        return False
    current = intent or journal.plan(_new_intent(command, phase, now))
    _apply_command(command, task, current, journal, config)
    expected = _intent_expected_label(current)
    if expected is None or not _status_is_verified(task.issue_number, expected, config):
        return False
    journal.mark_applied(current.intent_id)
    journal.mark_verified(current.intent_id)
    return True


def _resume_command(
    intent: TransitionIntent, commands: tuple[RepairCommand, ...]
) -> RepairCommand | None:
    expected = _intent_expected_label(intent)
    return next(
        (
            command
            for command in commands
            if command.subject_id == intent.subject_id
            and _expected_label(command) == expected
        ),
        None,
    )


def _resume_pending(
    pending: tuple[TransitionIntent, ...],
    base_commands: tuple[RepairCommand, ...],
    tasks_by_issue: Mapping[int, Task],
    completed: frozenset[str],
    config: DispatcherConfig,
    journal: IntentJournal,
    phase: StatusRepairPhase,
    now: datetime,
) -> list[Task]:
    repaired: list[Task] = []
    for intent in pending:
        if intent.subject_id is None or not _intent_is_for_phase(intent, phase):
            continue
        task = tasks_by_issue.get(int(intent.subject_id))
        expected = _intent_expected_label(intent)
        if task is None or expected is None:
            continue
        if _status_is_verified(task.issue_number, expected, config):
            journal.mark_applied(intent.intent_id)
            journal.mark_verified(intent.intent_id)
            repaired.append(task)
            continue
        command = _resume_command(intent, base_commands)
        if command is not None and _execute(
            command,
            task,
            tasks_by_issue,
            completed,
            config,
            journal,
            phase,
            now,
            intent,
        ):
            repaired.append(task)
    return repaired


def _evaluate_candidates(
    tasks_by_issue: Mapping[int, Task],
    completed: frozenset[str],
    pending: tuple[TransitionIntent, ...],
    phase: StatusRepairPhase,
    now: datetime,
) -> tuple[tuple[RepairCommand, ...], tuple[RepairCommand, ...]]:
    base = evaluate_status_repair_plan(
        tasks_by_issue,
        completed_subtask_ids=completed,
        now=now,
    )
    if not pending:
        selected = tuple(
            command for command in base.commands if _selected(command, phase)
        )
        return base.commands, selected
    guarded = evaluate_status_repair_plan(
        tasks_by_issue,
        completed_subtask_ids=completed,
        intents=pending,
        now=now,
    )
    selected = tuple(
        command for command in guarded.commands if _selected(command, phase)
    )
    return base.commands, selected


def _dry_run_tasks(
    tasks_by_issue: Mapping[int, Task],
    pending: tuple[TransitionIntent, ...],
    selected: tuple[RepairCommand, ...],
    phase: StatusRepairPhase,
) -> tuple[Task, ...]:
    subjects = {
        *(
            intent.subject_id
            for intent in pending
            if _intent_is_for_phase(intent, phase)
        ),
        *(command.subject_id for command in selected),
    }
    return tuple(
        task for task in tasks_by_issue.values() if str(task.issue_number) in subjects
    )


def _apply_selected(
    selected: tuple[RepairCommand, ...],
    covered: set[str | None],
    tasks_by_issue: Mapping[int, Task],
    completed: frozenset[str],
    config: DispatcherConfig,
    journal: IntentJournal,
    phase: StatusRepairPhase,
    now: datetime,
) -> list[Task]:
    repaired: list[Task] = []
    commands_by_subject = {
        command.subject_id: command
        for command in selected
        if command.subject_id is not None
    }
    for task in tasks_by_issue.values():
        subject_id = str(task.issue_number)
        command = commands_by_subject.get(subject_id)
        if command is None or subject_id in covered:
            continue
        if _execute(
            command, task, tasks_by_issue, completed, config, journal, phase, now
        ):
            repaired.append(task)
    return repaired


def reconcile_status_repairs(
    tasks_by_issue: Mapping[int, Task],
    *,
    completed_subtask_ids: Iterable[str],
    config: DispatcherConfig,
    phase: StatusRepairPhase,
    now: datetime | None = None,
) -> tuple[Task, ...]:
    """Resume live intents, then execute new commands owned by one phase."""
    observed_at = datetime.now(UTC) if now is None else now
    completed = frozenset(completed_subtask_ids)
    journal = IntentJournal(status_intent_journal_path(config))
    pending = journal.pending(now=observed_at)
    base_commands, selected = _evaluate_candidates(
        tasks_by_issue,
        completed,
        pending,
        phase,
        observed_at,
    )
    if not config.apply:
        return _dry_run_tasks(tasks_by_issue, pending, selected, phase)

    repaired = _resume_pending(
        pending,
        base_commands,
        tasks_by_issue,
        completed,
        config,
        journal,
        phase,
        observed_at,
    )
    repaired.extend(
        _apply_selected(
            selected,
            {
                intent.subject_id
                for intent in pending
                if _intent_is_for_phase(intent, phase)
            },
            tasks_by_issue,
            completed,
            config,
            journal,
            phase,
            observed_at,
        )
    )
    return tuple(dict.fromkeys(repaired))


def _repair_subject_task(
    command: RepairCommand, tasks_by_issue: Mapping[int, Task]
) -> Task | None:
    if command.subject_id is None:
        return None
    try:
        return tasks_by_issue.get(int(command.subject_id))
    except ValueError:
        return None


def _matching_pending_intent(
    command: RepairCommand, pending: Iterable[TransitionIntent]
) -> TransitionIntent | None:
    operation_suffix = f":{command.code}:{_finding_code(command) or 'unknown'}"
    return next(
        (
            candidate
            for candidate in pending
            if candidate.subject_id == command.subject_id
            and _intent_expected_label(candidate) == _expected_label(command)
            and candidate.operation.endswith(operation_suffix)
        ),
        None,
    )


def _failed_repair_result(command: RepairCommand, exc: Exception) -> RepairResult:
    detail = str(exc).strip()
    diagnostic = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    return RepairResult(
        command=command,
        status=RepairStatus.FAILED,
        diagnostics=(diagnostic,),
    )


def execute_status_repair_command(
    command: RepairCommand,
    tasks_by_issue: Mapping[int, Task],
    *,
    completed_subtask_ids: Iterable[str],
    config: DispatcherConfig,
    now: datetime | None = None,
) -> RepairResult:
    """Execute one supervisor-selected command through the existing safeguards."""
    if not config.apply:
        return RepairResult(command=command, status=RepairStatus.SKIPPED)
    task = _repair_subject_task(command, tasks_by_issue)
    if task is None:
        return RepairResult(
            command=command,
            status=RepairStatus.SKIPPED,
            diagnostics=("repair subject is not an observed task",),
        )
    observed_at = datetime.now(UTC) if now is None else now
    journal = IntentJournal(status_intent_journal_path(config))
    intent = _matching_pending_intent(command, journal.pending(now=observed_at))
    try:
        applied = _execute(
            command,
            task,
            tasks_by_issue,
            frozenset(completed_subtask_ids),
            config,
            journal,
            StatusRepairPhase.CLOSED_LOOP,
            observed_at,
            intent,
        )
    except Exception as exc:  # noqa: BLE001 - retain the Intent for restart
        return _failed_repair_result(command, exc)
    return RepairResult(
        command=command,
        status=RepairStatus.APPLIED if applied else RepairStatus.SKIPPED,
    )


__all__ = [
    "StatusRepairEvaluation",
    "StatusRepairPhase",
    "evaluate_status_repair_plan",
    "execute_status_repair_command",
    "reconcile_status_repairs",
    "status_intent_journal_path",
    "task_lifecycle",
]
