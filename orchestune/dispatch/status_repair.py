"""Execute supervisor-selected typed status repair commands."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from orchestune.consistency.desired import TaskLifecycle
from orchestune.consistency.intents import IntentJournal
from orchestune.consistency.invariants.status import (
    PROMOTION_HOLD_LABELS,
    primary_status_labels,
)
from orchestune.consistency.models import (
    ConsistencyScope,
    DesiredFact,
    IntentStatus,
    RepairCommand,
    RepairResult,
    RepairStatus,
    TransitionIntent,
)
from orchestune.consistency.repairs.status import (
    COMMAND_ADD_LABEL,
    COMMAND_REMOVE_LABEL,
    COMMAND_TRANSITION_LABEL,
)
from orchestune.consistency.vocabulary import DESIRED_STATUS_LABEL
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.labels import transition_status_label
from orchestune.dispatch.scoring import Task
from orchestune.labels import StatusLabel

_STATUS_REPAIR_OPERATION = "supervisor-status-repair"


def task_lifecycle(
    status_labels: tuple[str, ...], *, completed: bool = False
) -> TaskLifecycle:
    """Resolve lifecycle with an explicit same-cycle completion override."""
    if completed:
        return TaskLifecycle.DONE
    if StatusLabel.DONE in status_labels and StatusLabel.QUEUED in status_labels:
        return TaskLifecycle.OPEN
    if StatusLabel.DONE in status_labels:
        return TaskLifecycle.DONE
    if StatusLabel.NOT_NEEDED in status_labels:
        return TaskLifecycle.NOT_NEEDED
    if any(
        label in status_labels
        for label in (
            StatusLabel.BLOCKED_HUMAN_REVIEW,
            StatusLabel.MANUAL_MERGE_REQUIRED,
        )
    ):
        return TaskLifecycle.HUMAN_REVIEW
    return TaskLifecycle.OPEN


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


def _new_intent(command: RepairCommand, now: datetime) -> TransitionIntent:
    expected = _expected_label(command)
    assert command.subject_id is not None and expected is not None
    finding_code = _finding_code(command) or "unknown"
    return TransitionIntent(
        intent_id=f"status-{command.subject_id}-{uuid4().hex}",
        scope=ConsistencyScope.TASK,
        subject_id=command.subject_id,
        operation=f"{_STATUS_REPAIR_OPERATION}:{command.code}:{finding_code}",
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


def _precondition_holds(
    precondition: str,
    *,
    task: Task,
    labels: tuple[str, ...],
    dependencies_resolved: bool,
) -> bool:
    primary = primary_status_labels(labels)
    if precondition in {"finding-certainty:known", "issue-open"}:
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
        if not any(
            label in labels for label in (StatusLabel.DONE, StatusLabel.NOT_NEEDED)
        ):
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
    now: datetime,
    intent: TransitionIntent | None = None,
) -> bool:
    if not _fresh_preconditions_hold(
        command, task, tasks_by_issue, completed_subtask_ids, config
    ):
        return False
    current = intent or journal.plan(_new_intent(command, now))
    _apply_command(command, task, current, journal, config)
    expected = _intent_expected_label(current)
    if expected is None or not _status_is_verified(task.issue_number, expected, config):
        return False
    journal.mark_applied(current.intent_id)
    journal.mark_verified(current.intent_id)
    return True


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
    exact_suffix = f":{command.code}:{_finding_code(command) or 'unknown'}"

    def resumable(candidate: TransitionIntent) -> bool:
        exact_command = candidate.operation.endswith(exact_suffix)
        interrupted_transition = (
            f":{COMMAND_TRANSITION_LABEL}:" in f":{candidate.operation}:"
            and command.code == COMMAND_REMOVE_LABEL
        )
        return exact_command or interrupted_transition

    matches = tuple(
        candidate
        for candidate in pending
        if candidate.subject_id == command.subject_id
        and _intent_expected_label(candidate) == _expected_label(command)
        and resumable(candidate)
    )
    return matches[0] if len(matches) == 1 else None


def _failed_repair_result(command: RepairCommand, exc: Exception) -> RepairResult:
    detail = str(exc).strip()
    diagnostic = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    return RepairResult(
        command=command,
        status=RepairStatus.FAILED,
        diagnostics=(diagnostic,),
    )


def _status_command_preflight(
    command: RepairCommand,
    tasks_by_issue: Mapping[int, Task],
    config: DispatcherConfig,
) -> RepairResult | None:
    if command.code not in {
        COMMAND_ADD_LABEL,
        COMMAND_REMOVE_LABEL,
        COMMAND_TRANSITION_LABEL,
    }:
        return RepairResult(
            command=command,
            status=RepairStatus.FAILED,
            diagnostics=(f"unsupported status repair command: {command.code}",),
        )
    if not config.apply:
        return RepairResult(command=command, status=RepairStatus.SKIPPED)
    if _repair_subject_task(command, tasks_by_issue) is None:
        return RepairResult(
            command=command,
            status=RepairStatus.SKIPPED,
            diagnostics=("repair subject is not an observed task",),
        )
    return None


def _execute_with_pending_intent(
    command: RepairCommand,
    task: Task,
    tasks_by_issue: Mapping[int, Task],
    completed_subtask_ids: Iterable[str],
    config: DispatcherConfig,
    observed_at: datetime,
) -> RepairResult:
    journal = IntentJournal(status_intent_journal_path(config))
    pending = journal.pending(now=observed_at)
    intent = _matching_pending_intent(command, pending)
    if intent is None and any(
        candidate.subject_id == command.subject_id for candidate in pending
    ):
        return RepairResult(
            command=command,
            status=RepairStatus.SKIPPED,
            diagnostics=("another live status transition covers this subject",),
        )
    try:
        applied = _execute(
            command,
            task,
            tasks_by_issue,
            frozenset(completed_subtask_ids),
            config,
            journal,
            observed_at,
            intent,
        )
    except Exception as exc:  # noqa: BLE001 - retain the Intent for restart
        return _failed_repair_result(command, exc)
    return RepairResult(
        command=command,
        status=RepairStatus.APPLIED if applied else RepairStatus.SKIPPED,
    )


def execute_status_repair_command(
    command: RepairCommand,
    tasks_by_issue: Mapping[int, Task],
    *,
    completed_subtask_ids: Iterable[str],
    config: DispatcherConfig,
    now: datetime | None = None,
) -> RepairResult:
    """Execute one supervisor-selected command through live safeguards."""
    preflight = _status_command_preflight(command, tasks_by_issue, config)
    if preflight is not None:
        return preflight
    task = _repair_subject_task(command, tasks_by_issue)
    assert task is not None
    observed_at = datetime.now(UTC) if now is None else now
    return _execute_with_pending_intent(
        command,
        task,
        tasks_by_issue,
        completed_subtask_ids,
        config,
        observed_at,
    )


__all__ = [
    "execute_status_repair_command",
    "status_intent_journal_path",
    "task_lifecycle",
]
