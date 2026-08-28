from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from orchestune.consistency.desired import (
    DesiredTaskInput,
    DispatchPolicy,
    TaskLifecycle,
    derive_desired_repository_state,
)
from orchestune.consistency.models import (
    ConsistencyScope,
    DesiredFact,
    IntentStatus,
    TransitionIntent,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _task(
    task_id: str,
    issue_number: int,
    *,
    depends_on: tuple[str, ...] = (),
    lifecycle: TaskLifecycle = TaskLifecycle.OPEN,
    forced_serial: bool = False,
) -> DesiredTaskInput:
    return DesiredTaskInput(
        task_id=task_id,
        subject_id=str(issue_number),
        depends_on=depends_on,
        lifecycle=lifecycle,
        forced_serial=forced_serial,
    )


def _intent(
    intent_id: str,
    status: IntentStatus,
    *,
    expires_at: datetime | None = None,
) -> TransitionIntent:
    return TransitionIntent(
        intent_id=intent_id,
        scope=ConsistencyScope.TASK,
        subject_id="702",
        operation="launch-task",
        created_at=NOW,
        status=status,
        expires_at=expires_at,
        expected_changes=(
            DesiredFact(
                name="task.status_label",
                value="status:in-progress",
                scope=ConsistencyScope.TASK,
                subject_id="702",
                reason="task launch",
            ),
        ),
    )


def _fact_value(state, subject_id: str | None, name: str):
    matches = [
        fact.value
        for fact in state.facts
        if fact.subject_id == subject_id and fact.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def test_derivation_is_deterministic_for_equivalent_unordered_inputs() -> None:
    tasks = (
        _task("contract", 701, lifecycle=TaskLifecycle.DONE),
        _task("desired", 702, depends_on=("contract",)),
        _task("observer", 703, forced_serial=True),
    )
    intents = (
        _intent("z-intent", IntentStatus.APPLIED),
        _intent("a-intent", IntentStatus.PLANNED),
    )

    first = derive_desired_repository_state(
        "Saltmu/orchestune",
        tasks,
        active_task_ids=("desired",),
        completed_task_ids=("contract",),
        policy=DispatchPolicy(max_concurrent=2),
        intents=intents,
        now=NOW,
    )
    second = derive_desired_repository_state(
        "Saltmu/orchestune",
        reversed(tasks),
        active_task_ids={"desired"},
        completed_task_ids={"contract"},
        policy=DispatchPolicy(max_concurrent=2),
        intents=reversed(intents),
        now=NOW,
    )

    assert first == second
    assert [intent.intent_id for intent in first.transition_intents] == [
        "a-intent",
        "z-intent",
    ]
    assert list(first.facts) == sorted(
        first.facts,
        key=lambda fact: (fact.scope.value, fact.subject_id or "", fact.name),
    )


def test_dependencies_drive_blocked_and_queued_task_statuses() -> None:
    tasks = (
        _task("blocked", 702, depends_on=("missing", "contract")),
        _task("ready", 703, depends_on=("contract",)),
        _task("contract", 701, lifecycle=TaskLifecycle.DONE),
    )

    state = derive_desired_repository_state(
        "Saltmu/orchestune",
        tasks,
        completed_task_ids=("contract",),
        policy=DispatchPolicy(max_concurrent=2),
        now=NOW,
    )

    assert _fact_value(state, "702", "task.status_label") == "status:blocked"
    assert _fact_value(state, "702", "task.dependencies_resolved") is False
    assert _fact_value(state, "702", "task.unresolved_dependencies") == ("missing",)
    assert _fact_value(state, "702", "task.dispatch_eligible") is False
    assert _fact_value(state, "703", "task.status_label") == "status:queued"
    assert _fact_value(state, "703", "task.dependencies_resolved") is True
    assert _fact_value(state, "703", "task.dispatch_eligible") is True


@pytest.mark.parametrize(
    ("lifecycle", "status_label"),
    [
        (TaskLifecycle.DONE, "status:done"),
        (TaskLifecycle.NOT_NEEDED, "status:not-needed"),
        (TaskLifecycle.HUMAN_REVIEW, "status:blocked-human-review"),
    ],
)
def test_terminal_lifecycle_wins_over_stale_active_run_state(
    lifecycle: TaskLifecycle,
    status_label: str,
) -> None:
    state = derive_desired_repository_state(
        "Saltmu/orchestune",
        (_task("task", 702, lifecycle=lifecycle),),
        active_task_ids=("task",),
        policy=DispatchPolicy(max_concurrent=2),
        now=NOW,
    )

    assert _fact_value(state, "702", "task.status_label") == status_label
    assert _fact_value(state, "702", "task.run_state_active") is False
    assert _fact_value(state, "702", "task.dispatch_eligible") is False


def test_active_task_and_capacity_are_reflected_in_desired_facts() -> None:
    state = derive_desired_repository_state(
        "Saltmu/orchestune",
        (_task("active", 702), _task("waiting", 703)),
        active_task_ids=("active",),
        policy=DispatchPolicy(max_concurrent=1),
        now=NOW,
    )

    assert _fact_value(state, "702", "task.status_label") == "status:in-progress"
    assert _fact_value(state, "702", "task.run_state_active") is True
    assert _fact_value(state, "703", "task.status_label") == "status:queued"
    assert _fact_value(state, "703", "task.dispatch_eligible") is False
    assert _fact_value(state, None, "dispatch.active_count") == 1
    assert _fact_value(state, None, "dispatch.available_slots") == 0


def test_forced_serial_policy_blocks_overlapping_dispatch() -> None:
    tasks = (
        _task("serial-active", 702, forced_serial=True),
        _task("normal", 703),
        _task("serial-waiting", 704, forced_serial=True),
    )
    while_serial_active = derive_desired_repository_state(
        "Saltmu/orchestune",
        tasks,
        active_task_ids=("serial-active",),
        policy=DispatchPolicy(max_concurrent=3),
        now=NOW,
    )
    while_normal_active = derive_desired_repository_state(
        "Saltmu/orchestune",
        tasks,
        active_task_ids=("normal",),
        policy=DispatchPolicy(max_concurrent=3),
        now=NOW,
    )

    assert _fact_value(while_serial_active, "703", "task.dispatch_eligible") is False
    assert _fact_value(while_normal_active, "704", "task.dispatch_eligible") is False


def test_only_live_planned_or_applied_intents_are_valid_transitions() -> None:
    intents = (
        _intent("verified", IntentStatus.VERIFIED),
        _intent("failed", IntentStatus.FAILED),
        _intent("expired-status", IntentStatus.EXPIRED),
        _intent(
            "expired-by-time",
            IntentStatus.PLANNED,
            expires_at=NOW - timedelta(seconds=1),
        ),
        _intent(
            "planned",
            IntentStatus.PLANNED,
            expires_at=NOW + timedelta(seconds=1),
        ),
        _intent("applied", IntentStatus.APPLIED),
    )

    state = derive_desired_repository_state(
        "Saltmu/orchestune",
        (),
        policy=DispatchPolicy(max_concurrent=1),
        intents=intents,
        now=NOW,
    )

    assert [intent.intent_id for intent in state.transition_intents] == [
        "applied",
        "planned",
    ]


def test_invalid_or_ambiguous_inputs_fail_closed() -> None:
    with pytest.raises(ValueError, match="max_concurrent"):
        DispatchPolicy(max_concurrent=-1)
    with pytest.raises(ValueError, match="duplicate task_id"):
        derive_desired_repository_state(
            "Saltmu/orchestune",
            (_task("same", 1), _task("same", 2)),
            policy=DispatchPolicy(max_concurrent=1),
            now=NOW,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        derive_desired_repository_state(
            "Saltmu/orchestune",
            (),
            policy=DispatchPolicy(max_concurrent=1),
            now=datetime(2026, 8, 28),
        )


def test_task_identity_and_reference_validation_fails_closed() -> None:
    with pytest.raises(ValueError, match="task_id"):
        _task("", 1)
    with pytest.raises(ValueError, match="subject_id"):
        DesiredTaskInput(task_id="task", subject_id="")
    with pytest.raises(ValueError, match="depend on itself"):
        _task("task", 1, depends_on=("task",))
    with pytest.raises(ValueError, match="duplicate subject_id"):
        derive_desired_repository_state(
            "Saltmu/orchestune",
            (_task("one", 1), _task("two", 1)),
            policy=DispatchPolicy(max_concurrent=1),
            now=NOW,
        )
    with pytest.raises(ValueError, match="repository_id"):
        derive_desired_repository_state(
            "", (), policy=DispatchPolicy(max_concurrent=1), now=NOW
        )
    with pytest.raises(ValueError, match="unknown active task"):
        derive_desired_repository_state(
            "Saltmu/orchestune",
            (),
            active_task_ids=("missing",),
            policy=DispatchPolicy(max_concurrent=1),
            now=NOW,
        )


def test_duplicate_or_naive_intent_metadata_fails_closed() -> None:
    intent = _intent("same", IntentStatus.PLANNED)
    with pytest.raises(ValueError, match="duplicate intent_id"):
        derive_desired_repository_state(
            "Saltmu/orchestune",
            (),
            policy=DispatchPolicy(max_concurrent=1),
            intents=(intent, intent),
            now=NOW,
        )
    naive = TransitionIntent(
        intent_id="naive",
        scope=ConsistencyScope.TASK,
        operation="launch",
        created_at=datetime(2026, 8, 28),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        derive_desired_repository_state(
            "Saltmu/orchestune",
            (),
            policy=DispatchPolicy(max_concurrent=1),
            intents=(naive,),
            now=NOW,
        )
