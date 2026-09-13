"""Execute supervisor-selected typed status repair commands."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

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
from orchestune.dispatch.status_dependency_policy import dependencies_completed
from orchestune.dispatch.status_repair_dependencies import (
    CompletionEvidenceView,
    FreshDependencyEvaluation,
    evaluate_fresh_dependencies,
    task_lifecycle,
)

_STATUS_REPAIR_OPERATION = "supervisor-status-repair"


@dataclass(frozen=True, slots=True)
class VerifiedStatusTransition:
    """A status transition proven by live state and a verified intent journal."""

    issue_number: int
    before_labels: tuple[str, ...]
    verified_labels: tuple[str, ...]
    intent_id: str


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
    labels: tuple[str, ...],
    dependencies_declared: bool,
    dependencies_resolved: bool,
) -> bool:
    primary = primary_status_labels(labels)
    if precondition in {"finding-certainty:known", "issue-open"}:
        return True
    if precondition == "absent-primary-status":
        return not primary
    if precondition == "dependencies-declared":
        return dependencies_declared
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


def _fresh_preconditions_hold(
    command: RepairCommand,
    task: Task,
    tasks_by_issue: Mapping[int, Task],
    completion_evidence: CompletionEvidenceView,
    config: DispatcherConfig,
) -> FreshDependencyEvaluation | None:
    evaluation = evaluate_fresh_dependencies(
        task,
        tasks_by_issue,
        completion_evidence=completion_evidence,
        forge=config.resolved_forge,
    )
    if evaluation is None or evaluation.task.issue_state.upper() != "OPEN":
        return None
    labels = tuple(evaluation.task.status_labels)
    holds = all(
        _precondition_holds(
            precondition,
            labels=labels,
            dependencies_declared=not evaluation.dependencies.is_empty,
            dependencies_resolved=dependencies_completed(evaluation.assessment),
        )
        for precondition in command.preconditions
    )
    return evaluation if holds else None


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


def _verified_status_labels(
    issue_number: int, expected_label: str, config: DispatcherConfig
) -> tuple[str, ...] | None:
    if config.resolved_forge.get_issue_state(issue_number).upper() != "OPEN":
        return None
    labels = tuple(config.resolved_forge.get_issue_labels(issue_number))
    return labels if primary_status_labels(labels) == (expected_label,) else None


def _status_is_verified(
    issue_number: int, expected_label: str, config: DispatcherConfig
) -> bool:
    return _verified_status_labels(issue_number, expected_label, config) is not None


def reconcile_status_repair_intents(
    config: DispatcherConfig, *, now: datetime | None = None
) -> tuple[TransitionIntent, ...]:
    """Settle pending status Intents whose expected live state already exists."""
    if not config.apply:
        return ()
    observed_at = datetime.now(UTC) if now is None else now
    journal = IntentJournal(status_intent_journal_path(config))
    try:
        pending = journal.pending(now=observed_at)
    except Exception:  # noqa: BLE001 - leave unreadable journal for a later cycle
        return ()

    verified: list[TransitionIntent] = []
    for intent in pending:
        expected = _intent_expected_label(intent)
        try:
            issue_number = int(intent.subject_id or "")
            if expected is None or not _status_is_verified(
                issue_number, expected, config
            ):
                continue
            if intent.status is IntentStatus.PLANNED:
                journal.mark_applied(intent.intent_id)
            verified.append(journal.mark_verified(intent.intent_id))
        except Exception:  # noqa: BLE001 - retry live verification next cycle
            continue
    return tuple(verified)


def _execute(
    command: RepairCommand,
    task: Task,
    tasks_by_issue: Mapping[int, Task],
    completion_evidence: CompletionEvidenceView,
    config: DispatcherConfig,
    journal: IntentJournal,
    now: datetime,
    intent: TransitionIntent | None = None,
) -> VerifiedStatusTransition | None:
    fresh = _fresh_preconditions_hold(
        command, task, tasks_by_issue, completion_evidence, config
    )
    if fresh is None:
        return None
    current = intent or journal.plan(_new_intent(command, now))
    _apply_command(command, fresh.task, current, journal, config)
    expected = _intent_expected_label(current)
    verified_labels = (
        None
        if expected is None
        else _verified_status_labels(fresh.task.issue_number, expected, config)
    )
    if verified_labels is None:
        return None
    journal.mark_applied(current.intent_id)
    journal.mark_verified(current.intent_id)
    return VerifiedStatusTransition(
        issue_number=fresh.task.issue_number,
        before_labels=tuple(fresh.task.status_labels),
        verified_labels=verified_labels,
        intent_id=current.intent_id,
    )


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
    completion_evidence: CompletionEvidenceView,
    config: DispatcherConfig,
    observed_at: datetime,
    on_verified: Callable[[VerifiedStatusTransition], None] | None,
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
        evidence = _execute(
            command,
            task,
            tasks_by_issue,
            completion_evidence,
            config,
            journal,
            observed_at,
            intent,
        )
        if evidence is not None and on_verified is not None:
            on_verified(evidence)
    except Exception as exc:  # noqa: BLE001 - retain the Intent for restart
        return _failed_repair_result(command, exc)
    return RepairResult(
        command=command,
        status=RepairStatus.APPLIED if evidence is not None else RepairStatus.SKIPPED,
    )


def execute_status_repair_command(
    command: RepairCommand,
    tasks_by_issue: Mapping[int, Task],
    *,
    completion_evidence: CompletionEvidenceView,
    config: DispatcherConfig,
    now: datetime | None = None,
    on_verified: Callable[[VerifiedStatusTransition], None] | None = None,
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
        completion_evidence,
        config,
        observed_at,
        on_verified,
    )


__all__ = [
    "VerifiedStatusTransition",
    "execute_status_repair_command",
    "reconcile_status_repair_intents",
    "status_intent_journal_path",
    "task_lifecycle",
]
