"""Pure derivation of repository expectations from normalized task state."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from orchestune.consistency.models import (
    ConsistencyScope,
    DesiredFact,
    DesiredRepositoryState,
    FactValue,
    IntentStatus,
    TransitionIntent,
)


class TaskLifecycle(StrEnum):
    """Task-state-machine positions that do not come from active run state."""

    OPEN = "open"
    DONE = "done"
    NOT_NEEDED = "not-needed"
    HUMAN_REVIEW = "human-review"


@dataclass(frozen=True, slots=True)
class DesiredTaskInput:
    """Normalized task inputs required to derive desired consistency facts."""

    task_id: str
    subject_id: str
    depends_on: tuple[str, ...] = ()
    lifecycle: TaskLifecycle = TaskLifecycle.OPEN
    forced_serial: bool = False

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task_id must not be empty")
        if not self.subject_id:
            raise ValueError("subject_id must not be empty")
        if self.task_id in self.depends_on:
            raise ValueError(f"task {self.task_id!r} cannot depend on itself")
        object.__setattr__(self, "depends_on", tuple(sorted(set(self.depends_on))))


@dataclass(frozen=True, slots=True)
class DispatchPolicy:
    """Policy values that constrain whether a ready task may be dispatched."""

    max_concurrent: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_concurrent, bool)
            or not isinstance(self.max_concurrent, int)
            or self.max_concurrent < 0
        ):
            raise ValueError("max_concurrent must be a non-negative integer")


_TERMINAL_STATUS = {
    TaskLifecycle.DONE: "status:done",
    TaskLifecycle.NOT_NEEDED: "status:not-needed",
    TaskLifecycle.HUMAN_REVIEW: "status:blocked-human-review",
}
_LIVE_INTENT_STATUSES = frozenset({IntentStatus.PLANNED, IntentStatus.APPLIED})


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _normalized_tasks(
    tasks: Iterable[DesiredTaskInput],
) -> tuple[DesiredTaskInput, ...]:
    normalized = tuple(sorted(tasks, key=lambda task: task.task_id))
    task_ids = [task.task_id for task in normalized]
    subject_ids = [task.subject_id for task in normalized]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("duplicate task_id in desired-state input")
    if len(subject_ids) != len(set(subject_ids)):
        raise ValueError("duplicate subject_id in desired-state input")
    return normalized


def _live_intents(
    intents: Iterable[TransitionIntent], now: datetime
) -> tuple[TransitionIntent, ...]:
    by_id: dict[str, TransitionIntent] = {}
    for intent in intents:
        _require_aware(intent.created_at, "intent.created_at")
        if intent.expires_at is not None:
            _require_aware(intent.expires_at, "intent.expires_at")
        if intent.intent_id in by_id:
            raise ValueError(f"duplicate intent_id: {intent.intent_id}")
        by_id[intent.intent_id] = intent
    return tuple(
        intent
        for intent_id, intent in sorted(by_id.items())
        if intent.status in _LIVE_INTENT_STATUSES
        and (intent.expires_at is None or intent.expires_at > now)
    )


def _repository_facts(
    policy: DispatchPolicy,
    active_count: int,
    forced_serial_active: bool,
) -> tuple[DesiredFact, ...]:
    values: dict[str, FactValue] = {
        "dispatch.active_count": active_count,
        "dispatch.available_slots": max(0, policy.max_concurrent - active_count),
        "dispatch.forced_serial_active": forced_serial_active,
        "dispatch.max_concurrent": policy.max_concurrent,
    }
    return tuple(
        DesiredFact(
            name=name,
            value=value,
            scope=ConsistencyScope.REPOSITORY,
            reason="configured dispatch policy and active run state",
        )
        for name, value in values.items()
    )


def _task_status(
    task: DesiredTaskInput,
    *,
    is_active: bool,
    unresolved: tuple[str, ...],
) -> str:
    terminal = _TERMINAL_STATUS.get(task.lifecycle)
    if terminal is not None:
        return terminal
    if is_active:
        return "status:in-progress"
    if unresolved:
        return "status:blocked"
    return "status:queued"


def _is_dispatch_eligible(
    task: DesiredTaskInput,
    *,
    is_active: bool,
    unresolved: tuple[str, ...],
    available_slots: int,
    active_count: int,
    forced_serial_active: bool,
) -> bool:
    if task.lifecycle is not TaskLifecycle.OPEN or is_active or unresolved:
        return False
    if available_slots == 0 or forced_serial_active:
        return False
    return not task.forced_serial or active_count == 0


def _task_facts(
    task: DesiredTaskInput,
    *,
    is_active: bool,
    unresolved: tuple[str, ...],
    eligible: bool,
) -> tuple[DesiredFact, ...]:
    status = _task_status(task, is_active=is_active, unresolved=unresolved)
    values: dict[str, FactValue] = {
        "task.dependencies_resolved": not unresolved,
        "task.dispatch_eligible": eligible,
        "task.run_state_active": is_active,
        "task.status_label": status,
        "task.unresolved_dependencies": unresolved,
    }
    return tuple(
        DesiredFact(
            name=name,
            value=value,
            scope=ConsistencyScope.TASK,
            subject_id=task.subject_id,
            reason="derived from task lifecycle, dependencies, and dispatch policy",
        )
        for name, value in values.items()
    )


def _all_task_facts(
    tasks: tuple[DesiredTaskInput, ...],
    active_ids: frozenset[str],
    completed_ids: frozenset[str],
    policy: DispatchPolicy,
) -> tuple[DesiredFact, ...]:
    active_count = len(active_ids)
    forced_serial_active = any(
        task.task_id in active_ids and task.forced_serial for task in tasks
    )
    available_slots = max(0, policy.max_concurrent - active_count)
    facts: list[DesiredFact] = []
    for task in tasks:
        unresolved = tuple(dep for dep in task.depends_on if dep not in completed_ids)
        is_active = task.task_id in active_ids
        eligible = _is_dispatch_eligible(
            task,
            is_active=is_active,
            unresolved=unresolved,
            available_slots=available_slots,
            active_count=active_count,
            forced_serial_active=forced_serial_active,
        )
        facts.extend(
            _task_facts(
                task, is_active=is_active, unresolved=unresolved, eligible=eligible
            )
        )
    return tuple(facts)


def derive_desired_repository_state(
    repository_id: str,
    tasks: Iterable[DesiredTaskInput],
    *,
    active_task_ids: Iterable[str] = (),
    completed_task_ids: Iterable[str] = (),
    policy: DispatchPolicy,
    intents: Iterable[TransitionIntent] = (),
    now: datetime,
) -> DesiredRepositoryState:
    """Derive deterministic desired facts without observing or mutating state.

    ``completed_task_ids`` may name dependencies outside the supplied task slice;
    unlike active run-state entries, those external completion facts are valid input.
    ``task.dispatch_eligible`` describes each task independently.  Consumers must
    still select no more than ``dispatch.available_slots`` tasks from that set.
    """

    if not repository_id:
        raise ValueError("repository_id must not be empty")
    _require_aware(now, "now")
    normalized_tasks = _normalized_tasks(tasks)
    task_ids = {task.task_id for task in normalized_tasks}
    requested_active = frozenset(active_task_ids)
    unknown_active = requested_active - task_ids
    if unknown_active:
        raise ValueError(f"unknown active task IDs: {sorted(unknown_active)}")
    completed_ids = frozenset(completed_task_ids) | frozenset(
        task.task_id
        for task in normalized_tasks
        if task.lifecycle in {TaskLifecycle.DONE, TaskLifecycle.NOT_NEEDED}
    )
    active_ids = frozenset(
        task.task_id
        for task in normalized_tasks
        if task.task_id in requested_active and task.lifecycle is TaskLifecycle.OPEN
    )
    facts = [
        *_repository_facts(
            policy,
            len(active_ids),
            any(
                task.task_id in active_ids and task.forced_serial
                for task in normalized_tasks
            ),
        ),
        *_all_task_facts(normalized_tasks, active_ids, completed_ids, policy),
    ]
    return DesiredRepositoryState(
        repository_id=repository_id,
        facts=tuple(
            sorted(
                facts,
                key=lambda fact: (fact.scope.value, fact.subject_id or "", fact.name),
            )
        ),
        transition_intents=_live_intents(intents, now),
    )


__all__ = [
    "DesiredTaskInput",
    "DispatchPolicy",
    "TaskLifecycle",
    "derive_desired_repository_state",
]
