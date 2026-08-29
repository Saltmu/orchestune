"""Pure derivation of repository expectations from normalized task state."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from orchestune.consistency.intents import (
    intent_is_live,
    require_timezone_aware,
)
from orchestune.consistency.models import (
    ConsistencyScope,
    DesiredFact,
    DesiredRepositoryState,
    FactValue,
    TransitionIntent,
)
from orchestune.consistency.vocabulary import (
    DESIRED_ACTIVE_COUNT,
    DESIRED_AVAILABLE_SLOTS,
    DESIRED_DEPENDENCIES_RESOLVED,
    DESIRED_DISPATCH_ELIGIBLE,
    DESIRED_FORCED_SERIAL_ACTIVE,
    DESIRED_MAX_CONCURRENT,
    DESIRED_RUN_STATE_ACTIVE,
    DESIRED_STATUS_LABEL,
    DESIRED_UNRESOLVED_DEPENDENCIES,
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
    footprint: tuple[str, ...] = ()
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
        object.__setattr__(self, "footprint", tuple(sorted(set(self.footprint))))


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


def _normalized_active_ids(
    tasks: tuple[DesiredTaskInput, ...],
    active_task_ids: Iterable[str],
) -> frozenset[str]:
    requested = frozenset(active_task_ids)
    unknown = requested - {task.task_id for task in tasks}
    if unknown:
        raise ValueError(f"unknown active task IDs: {sorted(unknown)}")
    return frozenset(
        task.task_id
        for task in tasks
        if task.task_id in requested and task.lifecycle is TaskLifecycle.OPEN
    )


def _normalized_completed_ids(
    tasks: tuple[DesiredTaskInput, ...],
    completed_task_ids: Iterable[str],
) -> frozenset[str]:
    terminal = frozenset(
        task.task_id
        for task in tasks
        if task.lifecycle in {TaskLifecycle.DONE, TaskLifecycle.NOT_NEEDED}
    )
    requested = frozenset(completed_task_ids)
    known_ids = {task.task_id for task in tasks}
    contradictory = sorted((requested & known_ids) - terminal)
    if contradictory:
        raise ValueError(f"non-completed tasks declared completed: {contradictory}")
    return requested | terminal


def _live_intents(
    intents: Iterable[TransitionIntent], now: datetime
) -> tuple[TransitionIntent, ...]:
    by_id: dict[str, TransitionIntent] = {}
    for intent in intents:
        if intent.intent_id in by_id:
            raise ValueError(f"duplicate intent_id: {intent.intent_id}")
        by_id[intent.intent_id] = intent
    return tuple(
        intent
        for intent_id, intent in sorted(by_id.items())
        if intent_is_live(intent, now=now)
    )


def _repository_facts(
    policy: DispatchPolicy,
    active_count: int,
    forced_serial_active: bool,
) -> tuple[DesiredFact, ...]:
    values: dict[str, FactValue] = {
        DESIRED_ACTIVE_COUNT: active_count,
        DESIRED_AVAILABLE_SLOTS: max(0, policy.max_concurrent - active_count),
        DESIRED_FORCED_SERIAL_ACTIVE: forced_serial_active,
        DESIRED_MAX_CONCURRENT: policy.max_concurrent,
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
    forced_serial_conflict: bool,
) -> bool:
    if task.lifecycle is not TaskLifecycle.OPEN or is_active or unresolved:
        return False
    if available_slots == 0 or forced_serial_conflict:
        return False
    return True


def _conflicts_with_forced_serial(
    candidate: DesiredTaskInput,
    active: DesiredTaskInput,
) -> bool:
    if set(candidate.footprint) & set(active.footprint):
        return True
    if active.task_id in candidate.depends_on:
        return True
    return candidate.task_id in active.depends_on


def _task_facts(
    task: DesiredTaskInput,
    *,
    is_active: bool,
    unresolved: tuple[str, ...],
    eligible: bool,
) -> tuple[DesiredFact, ...]:
    status = _task_status(task, is_active=is_active, unresolved=unresolved)
    values: dict[str, FactValue] = {
        DESIRED_DEPENDENCIES_RESOLVED: not unresolved,
        DESIRED_DISPATCH_ELIGIBLE: eligible,
        DESIRED_RUN_STATE_ACTIVE: is_active,
        DESIRED_STATUS_LABEL: status,
        DESIRED_UNRESOLVED_DEPENDENCIES: unresolved,
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
    forced_serial_actives = tuple(
        task for task in tasks if task.task_id in active_ids and task.forced_serial
    )
    available_slots = max(0, policy.max_concurrent - active_count)
    facts: list[DesiredFact] = []
    for task in tasks:
        unresolved = tuple(dep for dep in task.depends_on if dep not in completed_ids)
        is_active = task.task_id in active_ids
        forced_serial_conflict = any(
            _conflicts_with_forced_serial(task, active)
            for active in forced_serial_actives
        )
        eligible = _is_dispatch_eligible(
            task,
            is_active=is_active,
            unresolved=unresolved,
            available_slots=available_slots,
            forced_serial_conflict=forced_serial_conflict,
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
    require_timezone_aware(now, "now")
    normalized_tasks = _normalized_tasks(tasks)
    active_ids = _normalized_active_ids(normalized_tasks, active_task_ids)
    completed_ids = _normalized_completed_ids(normalized_tasks, completed_task_ids)
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
