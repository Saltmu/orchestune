"""Table-driven tests for the pure status repair planner (#705)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import pytest

from orchestune.consistency.desired import TaskLifecycle
from orchestune.consistency.invariants.status import (
    BLOCKED_WITH_RESOLVED_DEPENDENCIES,
    FORGE_OBSERVATION_UNKNOWN,
    PRIMARY_STATUS_CONFLICT,
    PRIMARY_STATUS_MISSING,
    QUEUED_WITH_UNRESOLVED_DEPENDENCIES,
)
from orchestune.consistency.models import (
    ConsistencyFinding,
    ConsistencyReport,
    ConsistencyScope,
    Evidence,
    FindingSeverity,
    ObservationCertainty,
    ObservedRepositoryState,
    Repairability,
    RepairCommand,
    ScopedObservations,
    TransitionIntent,
)
from orchestune.consistency.observation import FACT_ISSUE_LABELS
from orchestune.consistency.repairs.status import (
    COMMAND_ADD_LABEL,
    COMMAND_REMOVE_LABEL,
    COMMAND_TRANSITION_LABEL,
    plan_status_repairs,
)
from tests.consistency_status_test_support import (
    NOW,
    REPOSITORY,
    _codes,
    _desired,
    _desired_task,
    _evaluate,
    _observed,
    _only,
    _reachable,
    _repository_scope,
    _status_intent,
    _task_scope,
)


def _plan(
    labels: Sequence[str],
    *,
    lifecycle: TaskLifecycle = TaskLifecycle.OPEN,
    depends_on: tuple[str, ...] = (),
    completed: tuple[str, ...] = (),
    intents: tuple[TransitionIntent, ...] = (),
) -> tuple[str, ...]:
    report = _evaluate(
        _observed(_task_scope(705, labels=labels)),
        _desired(
            _desired_task(
                "status-policy", 705, lifecycle=lifecycle, depends_on=depends_on
            ),
            completed=completed,
            intents=intents,
        ),
    )
    return tuple(command.code for command in plan_status_repairs(report))


@dataclass(frozen=True)
class _PlanCase:
    name: str
    labels: tuple[str, ...]
    lifecycle: TaskLifecycle
    depends_on: tuple[str, ...]
    completed: tuple[str, ...]
    expected: tuple[str, ...]


_PLAN_CASES = (
    _PlanCase("healthy task", ("status:queued",), TaskLifecycle.OPEN, (), (), ()),
    _PlanCase(
        "restore a lost primary label",
        (),
        TaskLifecycle.OPEN,
        (),
        (),
        (COMMAND_ADD_LABEL,),
    ),
    _PlanCase(
        "finish an interrupted rollback",
        ("status:done", "status:queued"),
        TaskLifecycle.OPEN,
        (),
        (),
        (COMMAND_REMOVE_LABEL,),
    ),
    _PlanCase(
        "never strip a human gate",
        ("status:done", "status:blocked-human-review"),
        TaskLifecycle.DONE,
        (),
        (),
        (),
    ),
    _PlanCase(
        "promote a resolved dependency",
        ("status:blocked",),
        TaskLifecycle.OPEN,
        ("external",),
        ("external",),
        (COMMAND_TRANSITION_LABEL,),
    ),
    _PlanCase(
        "demote a prematurely queued task",
        ("status:queued",),
        TaskLifecycle.OPEN,
        ("external",),
        (),
        (COMMAND_TRANSITION_LABEL,),
    ),
    _PlanCase(
        "leave a held promotion alone",
        ("status:blocked", "ci:base-branch-red"),
        TaskLifecycle.OPEN,
        ("external",),
        ("external",),
        (),
    ),
)


@pytest.mark.parametrize("case", _PLAN_CASES, ids=lambda case: case.name)
def test_plan_status_repairs_emits_only_deterministic_commands(
    case: _PlanCase,
) -> None:
    assert (
        _plan(
            case.labels,
            lifecycle=case.lifecycle,
            depends_on=case.depends_on,
            completed=case.completed,
        )
        == case.expected
    )


def test_a_live_intent_reports_the_divergence_without_planning_a_repair() -> None:
    labels = ("status:done", "status:queued")
    intents = (_status_intent(705),)
    report = _evaluate(
        _observed(_task_scope(705, labels=labels)),
        _desired(_desired_task("status-policy", 705), intents=intents),
    )

    finding = _only(report, PRIMARY_STATUS_CONFLICT)
    assert finding.repairability is Repairability.NONE
    assert any("intent-705" in detail for detail in finding.observed.details)
    assert plan_status_repairs(report) == ()


def test_add_command_names_the_label_and_guards_the_transition() -> None:
    report = _evaluate(
        _observed(_task_scope(705, labels=())),
        _desired(_desired_task("status-policy", 705)),
    )

    (command,) = plan_status_repairs(report)
    assert command.code == COMMAND_ADD_LABEL
    assert command.scope is ConsistencyScope.TASK
    assert command.subject_id == "705"
    assert dict(command.parameters)["label"] == "status:queued"
    assert command.idempotency_key == "status:705:add:status:queued"
    assert "issue-open" in command.preconditions


def test_remove_command_keeps_the_desired_label() -> None:
    report = _evaluate(
        _observed(
            _task_scope(705, labels=("status:blocked", "status:done", "status:queued"))
        ),
        _desired(_desired_task("status-policy", 705)),
    )

    commands = plan_status_repairs(report)
    assert [dict(command.parameters)["label"] for command in commands] == [
        "status:blocked",
        "status:done",
    ]
    assert all(command.code == COMMAND_REMOVE_LABEL for command in commands)
    assert all(
        "retains-primary-status:status:queued" in command.preconditions
        for command in commands
    )
    assert len({command.idempotency_key for command in commands}) == 2


def test_transition_command_carries_the_replaced_label() -> None:
    report = _evaluate(
        _observed(_task_scope(705, labels=("status:blocked",))),
        _desired(
            _desired_task("status-policy", 705, depends_on=("external",)),
            completed=("external",),
        ),
    )

    (command,) = plan_status_repairs(report)
    parameters = dict(command.parameters)
    assert command.code == COMMAND_TRANSITION_LABEL
    assert parameters["new_label"] == "status:queued"
    assert parameters["old_labels"] == ("status:blocked",)
    assert "dependencies-declared" in command.preconditions
    assert "dependencies-resolved" in command.preconditions
    assert "no-promotion-hold" in command.preconditions


def test_plan_ignores_findings_it_does_not_own() -> None:
    report = ConsistencyReport(
        repository_id=REPOSITORY,
        findings=(
            ConsistencyFinding(
                code="execution.local-process-dead",
                scope=ConsistencyScope.TASK,
                subject_id="705",
                severity=FindingSeverity.ERROR,
                expected=Evidence("alive", value=True),
                observed=Evidence("dead", value=False),
                repairability=Repairability.AUTOMATIC,
            ),
        ),
        evaluated_invariants=("execution.task-state",),
    )

    assert plan_status_repairs(report) == ()


def test_plan_is_ordered_by_subject_then_command() -> None:
    report = _evaluate(
        _observed(
            _task_scope(706, labels=()),
            _task_scope(705, labels=("status:done", "status:queued")),
        ),
        _desired(
            _desired_task("status-policy", 705),
            _desired_task("shadow-supervisor", 706),
        ),
    )

    commands = plan_status_repairs(report)
    assert [(command.subject_id, command.code) for command in commands] == [
        ("705", COMMAND_REMOVE_LABEL),
        ("706", COMMAND_ADD_LABEL),
    ]


def _subject_ids(commands: Iterable[RepairCommand]) -> list[str | None]:
    return [command.subject_id for command in commands]


def test_an_unknown_subject_is_excluded_but_its_peers_are_not() -> None:
    report = _evaluate(
        _observed(
            _task_scope(705, labels=(), uncertain=(FACT_ISSUE_LABELS,)),
            _task_scope(706, labels=()),
        ),
        _desired(
            _desired_task("status-policy", 705),
            _desired_task("shadow-supervisor", 706),
        ),
    )

    assert _subject_ids(plan_status_repairs(report)) == ["706"]


def _automatic_finding(
    code: str, *, expected: object, observed: object
) -> ConsistencyReport:
    return ConsistencyReport(
        repository_id=REPOSITORY,
        findings=(
            ConsistencyFinding(
                code=code,
                scope=ConsistencyScope.TASK,
                subject_id="705",
                severity=FindingSeverity.ERROR,
                expected=Evidence("expected", value=expected),  # type: ignore[arg-type]
                observed=Evidence("observed", value=observed),  # type: ignore[arg-type]
                repairability=Repairability.AUTOMATIC,
            ),
        ),
        evaluated_invariants=("status.task-policy",),
    )


@pytest.mark.parametrize(
    ("code", "expected", "observed"),
    (
        (PRIMARY_STATUS_MISSING, None, ()),
        (PRIMARY_STATUS_MISSING, "", ()),
        (PRIMARY_STATUS_CONFLICT, None, ("status:done", "status:queued")),
        (PRIMARY_STATUS_CONFLICT, "status:queued", "status:queued"),
        (PRIMARY_STATUS_CONFLICT, "status:not-needed", ("status:done",)),
        (BLOCKED_WITH_RESOLVED_DEPENDENCIES, "status:queued", ("status:queued",)),
        (QUEUED_WITH_UNRESOLVED_DEPENDENCIES, "status:blocked", ()),
    ),
)
def test_plan_skips_findings_without_complete_evidence(
    code: str, expected: object, observed: object
) -> None:
    assert (
        plan_status_repairs(
            _automatic_finding(code, expected=expected, observed=observed)
        )
        == ()
    )


def test_plan_survives_a_report_rebuilt_from_plain_strings() -> None:
    """Finding codes are data, so equality — not identity — must drive planning."""
    report = _automatic_finding(
        "".join(("status.", "blocked-with-resolved-dependencies")),
        expected="status:queued",
        observed=("status:blocked",),
    )

    (command,) = plan_status_repairs(report)
    assert command.code == COMMAND_TRANSITION_LABEL
    assert "no-promotion-hold" in command.preconditions


def test_plan_refuses_to_strip_a_human_gate_even_when_told_to() -> None:
    """A report may claim anything; removing a human gate stays out of reach."""
    report = _automatic_finding(
        PRIMARY_STATUS_CONFLICT,
        expected="status:done",
        observed=("status:blocked-human-review", "status:done"),
    )

    assert plan_status_repairs(report) == ()


_UNTRUSTWORTHY_FORGE_SCOPES: dict[str, tuple[ScopedObservations, ...]] = {
    "no repository scope": (),
    "two repository scopes": (
        _repository_scope(_reachable()),
        _repository_scope(_reachable()),
    ),
    "no reachability fact": (_repository_scope(),),
    "a duplicated reachability fact": (_repository_scope(_reachable(), _reachable()),),
    "a stale reachability fact": (
        _repository_scope(_reachable(certainty=ObservationCertainty.STALE)),
    ),
    "a negative reachability fact": (_repository_scope(_reachable(False)),),
    "a non-boolean reachability fact": (_repository_scope(_reachable("yes")),),
}


@pytest.mark.parametrize("name", tuple(_UNTRUSTWORTHY_FORGE_SCOPES))
def test_only_one_certain_reachable_forge_permits_a_plan(name: str) -> None:
    observed = ObservedRepositoryState(
        repository_id=REPOSITORY,
        observed_at=NOW,
        observations=(
            *_UNTRUSTWORTHY_FORGE_SCOPES[name],
            _task_scope(705, labels=()),
        ),
    )

    report = _evaluate(observed, _desired(_desired_task("status-policy", 705)))

    assert _only(report, FORGE_OBSERVATION_UNKNOWN).scope is ConsistencyScope.REPOSITORY
    assert PRIMARY_STATUS_MISSING in _codes(report)
    assert plan_status_repairs(report) == ()


@pytest.mark.parametrize(
    ("code", "observed_label", "required"),
    (
        (
            BLOCKED_WITH_RESOLVED_DEPENDENCIES,
            "status:blocked",
            ("dependencies-declared", "dependencies-resolved", "no-promotion-hold"),
        ),
        (
            QUEUED_WITH_UNRESOLVED_DEPENDENCIES,
            "status:queued",
            ("dependencies-unresolved",),
        ),
    ),
)
def test_a_transition_revalidates_the_dependency_state_it_relied_on(
    code: str, observed_label: str, required: tuple[str, ...]
) -> None:
    """Dependencies can move between evaluation and execution, so re-check."""
    report = _automatic_finding(
        code,
        expected="status:queued"
        if observed_label == "status:blocked"
        else ("status:blocked"),
        observed=(observed_label,),
    )

    (command,) = plan_status_repairs(report)
    assert set(required) <= set(command.preconditions)
    assert f"holds-primary-status:{observed_label}" in command.preconditions
