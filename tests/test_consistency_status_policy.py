"""Table-driven tests for the pure status consistency policy (#705)."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from orchestune.consistency.desired import (
    DesiredTaskInput,
    DispatchPolicy,
    TaskLifecycle,
    derive_desired_repository_state,
)
from orchestune.consistency.engine import ConsistencyEngine
from orchestune.consistency.invariants.status import (
    BLOCKED_PROMOTION_HELD,
    BLOCKED_WITH_RESOLVED_DEPENDENCIES,
    DONE_WITH_ACTIVE_EXECUTION,
    FORCED_SERIAL_MISMATCH,
    FORGE_OBSERVATION_UNKNOWN,
    IN_PROGRESS_WITHOUT_EXECUTION,
    PRIMARY_STATUS_CONFLICT,
    PRIMARY_STATUS_LABELS,
    PRIMARY_STATUS_MISSING,
    PROMOTION_HOLD_LABELS,
    QUEUED_WITH_UNRESOLVED_DEPENDENCIES,
    STATUS_OBSERVATION_UNKNOWN,
    TERMINAL_ESCALATION_LABELS,
    status_invariants,
)
from orchestune.consistency.models import (
    ConsistencyFinding,
    ConsistencyReport,
    ConsistencyScope,
    DesiredFact,
    DesiredRepositoryState,
    Evidence,
    FindingSeverity,
    IntentStatus,
    Observation,
    ObservationCertainty,
    ObservedRepositoryState,
    Repairability,
    RepairCommand,
    ScopedObservations,
    TransitionIntent,
)
from orchestune.consistency.observation import (
    EXECUTION_KIND_CLOUD,
    EXECUTION_KIND_LOCAL,
    EXECUTION_KIND_NONE,
    FACT_EXECUTION_KIND,
    FACT_FORGE_REACHABLE,
    FACT_ISSUE_LABELS,
    FACT_ISSUE_STATE,
    FACT_ISSUE_STATUS_LABELS,
)
from orchestune.consistency.repairs.status import (
    COMMAND_ADD_LABEL,
    COMMAND_REMOVE_LABEL,
    COMMAND_TRANSITION_LABEL,
    plan_status_repairs,
)
from orchestune.dispatch.labels import (
    PRIMARY_STATUS_LABELS as DISPATCH_PRIMARY_STATUS_LABELS,
)
from orchestune.dispatch.labels import (
    TERMINAL_ESCALATION_LABELS as DISPATCH_TERMINAL_ESCALATION_LABELS,
)

REPOSITORY = "Saltmu/orchestune"
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
KNOWN = ObservationCertainty.KNOWN
UNKNOWN = ObservationCertainty.UNKNOWN


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _observation(
    name: str,
    value: object,
    *,
    certainty: ObservationCertainty = KNOWN,
) -> Observation:
    return Observation(
        name=name,
        value=None if certainty is not KNOWN else value,  # type: ignore[arg-type]
        certainty=certainty,
        source="forge",
        observed_at=NOW,
        diagnostics=() if certainty is KNOWN else ("probe failed",),
    )


def _task_scope(
    issue_number: int,
    *,
    labels: Sequence[str] = ("status:queued",),
    issue_state: str | None = "OPEN",
    execution_kind: str = EXECUTION_KIND_NONE,
    uncertain: Sequence[str] = (),
    omit: Sequence[str] = (),
) -> ScopedObservations:
    """Mirror what `ObservationCollector` emits for one task Issue."""
    all_labels = tuple(sorted(set(labels)))
    values: dict[str, object] = {
        FACT_EXECUTION_KIND: execution_kind,
        FACT_ISSUE_LABELS: all_labels,
        FACT_ISSUE_STATE: issue_state,
        FACT_ISSUE_STATUS_LABELS: tuple(
            label for label in all_labels if label.startswith("status:")
        ),
    }
    facts = tuple(
        _observation(
            name,
            value,
            certainty=UNKNOWN if name in uncertain else KNOWN,
        )
        for name, value in sorted(values.items())
        if name not in omit
    )
    return ScopedObservations(
        scope=ConsistencyScope.TASK, subject_id=str(issue_number), facts=facts
    )


def _observed(
    *tasks: ScopedObservations,
    forge_certainty: ObservationCertainty = KNOWN,
) -> ObservedRepositoryState:
    repository = ScopedObservations(
        scope=ConsistencyScope.REPOSITORY,
        facts=(_observation(FACT_FORGE_REACHABLE, True, certainty=forge_certainty),),
    )
    return ObservedRepositoryState(
        repository_id=REPOSITORY,
        observed_at=NOW,
        observations=(repository, *tasks),
    )


def _desired_task(
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


def _desired(
    *tasks: DesiredTaskInput,
    active: tuple[str, ...] = (),
    completed: tuple[str, ...] = (),
    intents: tuple[TransitionIntent, ...] = (),
    max_concurrent: int = 3,
) -> DesiredRepositoryState:
    """Derive desired state with the real #702 derivation, not a stand-in."""
    return derive_desired_repository_state(
        REPOSITORY,
        tasks,
        active_task_ids=active,
        completed_task_ids=completed,
        policy=DispatchPolicy(max_concurrent=max_concurrent),
        intents=intents,
        now=NOW,
    )


def _status_intent(
    issue_number: int,
    *,
    status: IntentStatus = IntentStatus.APPLIED,
    expires_at: datetime | None = None,
    changed_fact: str = "task.status_label",
    scope: ConsistencyScope = ConsistencyScope.TASK,
    subject_id: str | None = None,
    created_at: datetime | None = None,
) -> TransitionIntent:
    subject = str(issue_number) if subject_id is None else subject_id
    return TransitionIntent(
        intent_id=f"intent-{issue_number}",
        scope=scope,
        subject_id=subject,
        operation="transition-status",
        created_at=NOW - timedelta(minutes=1) if created_at is None else created_at,
        status=status,
        expires_at=expires_at,
        expected_changes=(
            DesiredFact(
                name=changed_fact,
                value="status:in-progress",
                scope=ConsistencyScope.TASK,
                subject_id=str(issue_number),
                reason="launch in flight",
            ),
        ),
    )


def _with_intents(
    desired: DesiredRepositoryState, *intents: TransitionIntent
) -> DesiredRepositoryState:
    """Attach intents the #702 derivation would have filtered out on its own."""
    return DesiredRepositoryState(
        repository_id=desired.repository_id,
        facts=desired.facts,
        transition_intents=intents,
    )


def _evaluate(
    observed: ObservedRepositoryState, desired: DesiredRepositoryState
) -> ConsistencyReport:
    return ConsistencyEngine(status_invariants()).evaluate(observed, desired)


def _codes(report: ConsistencyReport) -> tuple[str, ...]:
    return tuple(finding.code for finding in report.findings)


def _only(report: ConsistencyReport, code: str) -> ConsistencyFinding:
    matches = [finding for finding in report.findings if finding.code == code]
    assert len(matches) == 1, _codes(report)
    return matches[0]


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def test_primary_status_labels_cover_the_dispatch_transition_sweep_set() -> None:
    """The kernel's primary set must contain the labels a transition sweeps."""
    assert set(DISPATCH_PRIMARY_STATUS_LABELS) <= set(PRIMARY_STATUS_LABELS)
    assert set(DISPATCH_TERMINAL_ESCALATION_LABELS) == set(TERMINAL_ESCALATION_LABELS)
    assert set(TERMINAL_ESCALATION_LABELS) <= set(PRIMARY_STATUS_LABELS)
    assert not set(PROMOTION_HOLD_LABELS) & set(PRIMARY_STATUS_LABELS)
    assert PRIMARY_STATUS_LABELS == tuple(sorted(PRIMARY_STATUS_LABELS))


def test_status_invariants_are_stable_and_scoped() -> None:
    invariants = status_invariants()
    assert [invariant.code for invariant in invariants] == [
        invariant.code for invariant in status_invariants()
    ]
    assert {invariant.scope for invariant in invariants} == {
        ConsistencyScope.REPOSITORY,
        ConsistencyScope.TASK,
    }
    assert all(invariant.code.startswith("status.") for invariant in invariants)


# ---------------------------------------------------------------------------
# Primary status cardinality
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CardinalityCase:
    name: str
    labels: tuple[str, ...]
    lifecycle: TaskLifecycle
    expected_code: str | None
    repairability: Repairability | None = None


_CARDINALITY_CASES = (
    _CardinalityCase("single primary", ("status:queued",), TaskLifecycle.OPEN, None),
    _CardinalityCase(
        "cross-cutting labels are not primary",
        ("status:queued", "status:external-lock", "status:force-serial"),
        TaskLifecycle.OPEN,
        None,
    ),
    _CardinalityCase(
        "no primary label at all",
        ("status:external-lock",),
        TaskLifecycle.OPEN,
        PRIMARY_STATUS_MISSING,
        Repairability.AUTOMATIC,
    ),
    _CardinalityCase(
        "no labels at all",
        (),
        TaskLifecycle.OPEN,
        PRIMARY_STATUS_MISSING,
        Repairability.AUTOMATIC,
    ),
    _CardinalityCase(
        "interrupted rollback keeps done beside queued",
        ("status:done", "status:queued"),
        TaskLifecycle.OPEN,
        PRIMARY_STATUS_CONFLICT,
        Repairability.AUTOMATIC,
    ),
    _CardinalityCase(
        "conflict whose desired label is absent",
        ("status:blocked", "status:in-progress"),
        TaskLifecycle.DONE,
        PRIMARY_STATUS_CONFLICT,
        Repairability.MANUAL,
    ),
    _CardinalityCase(
        "conflict that would remove a human gate",
        ("status:done", "status:blocked-human-review"),
        TaskLifecycle.DONE,
        PRIMARY_STATUS_CONFLICT,
        Repairability.MANUAL,
    ),
)


@pytest.mark.parametrize("case", _CARDINALITY_CASES, ids=lambda case: case.name)
def test_primary_status_cardinality(case: _CardinalityCase) -> None:
    report = _evaluate(
        _observed(_task_scope(705, labels=case.labels)),
        _desired(_desired_task("status-policy", 705, lifecycle=case.lifecycle)),
    )

    if case.expected_code is None:
        assert _codes(report) == ()
        return
    finding = _only(report, case.expected_code)
    assert finding.repairability is case.repairability
    assert finding.severity is FindingSeverity.ERROR
    assert finding.subject_id == "705"
    assert finding.observed.value == tuple(sorted(case.labels))


def test_primary_status_missing_is_manual_without_a_desired_label() -> None:
    """An Issue the plan does not describe has no deterministic label to add."""
    report = _evaluate(_observed(_task_scope(705, labels=())), _desired())

    finding = _only(report, PRIMARY_STATUS_MISSING)
    assert finding.repairability is Repairability.MANUAL
    assert finding.expected.value is None


# ---------------------------------------------------------------------------
# Evidence for active and completed states
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _EvidenceCase:
    name: str
    labels: tuple[str, ...]
    lifecycle: TaskLifecycle
    execution_kind: str
    with_intent: bool
    expected_code: str | None


_EVIDENCE_CASES = (
    _EvidenceCase(
        "in-progress backed by a local process",
        ("status:in-progress",),
        TaskLifecycle.OPEN,
        EXECUTION_KIND_LOCAL,
        False,
        None,
    ),
    _EvidenceCase(
        "in-progress backed by a cloud execution",
        ("status:in-progress",),
        TaskLifecycle.OPEN,
        EXECUTION_KIND_CLOUD,
        False,
        None,
    ),
    _EvidenceCase(
        "in-progress with nothing running",
        ("status:in-progress",),
        TaskLifecycle.OPEN,
        EXECUTION_KIND_NONE,
        False,
        IN_PROGRESS_WITHOUT_EXECUTION,
    ),
    _EvidenceCase(
        "in-progress justified by a live launch intent",
        ("status:in-progress",),
        TaskLifecycle.OPEN,
        EXECUTION_KIND_NONE,
        True,
        None,
    ),
    _EvidenceCase(
        "done with no execution",
        ("status:done",),
        TaskLifecycle.DONE,
        EXECUTION_KIND_NONE,
        False,
        None,
    ),
    _EvidenceCase(
        "done while an execution is still recorded",
        ("status:done",),
        TaskLifecycle.DONE,
        EXECUTION_KIND_LOCAL,
        False,
        DONE_WITH_ACTIVE_EXECUTION,
    ),
    _EvidenceCase(
        "done during a recorded transition",
        ("status:done",),
        TaskLifecycle.DONE,
        EXECUTION_KIND_LOCAL,
        True,
        None,
    ),
)


@pytest.mark.parametrize("case", _EVIDENCE_CASES, ids=lambda case: case.name)
def test_active_and_completed_states_require_evidence(case: _EvidenceCase) -> None:
    intents = (_status_intent(705),) if case.with_intent else ()
    report = _evaluate(
        _observed(
            _task_scope(705, labels=case.labels, execution_kind=case.execution_kind)
        ),
        _desired(
            _desired_task("status-policy", 705, lifecycle=case.lifecycle),
            intents=intents,
        ),
    )

    assert _codes(report) == (
        () if case.expected_code is None else (case.expected_code,)
    )
    if case.expected_code is not None:
        assert _only(report, case.expected_code).repairability is Repairability.MANUAL


_SETTLED_INTENTS = {
    "verified": {"status": IntentStatus.VERIFIED},
    "failed": {"status": IntentStatus.FAILED},
    "expired status": {"status": IntentStatus.EXPIRED},
    "past its deadline": {"expires_at": NOW - timedelta(seconds=1)},
    "changing another fact": {"changed_fact": "task.run_state_active"},
    "scoped to the parent": {"scope": ConsistencyScope.PARENT},
    "naming another task": {"subject_id": "706"},
    "with a naive timestamp": {"created_at": datetime(2026, 8, 29, 11, 0)},
}


@pytest.mark.parametrize("name", tuple(_SETTLED_INTENTS))
def test_settled_or_unrelated_intents_do_not_justify_in_progress(name: str) -> None:
    desired = _with_intents(
        _desired(_desired_task("status-policy", 705)),
        _status_intent(705, **_SETTLED_INTENTS[name]),  # type: ignore[arg-type]
    )

    report = _evaluate(
        _observed(_task_scope(705, labels=("status:in-progress",))), desired
    )

    assert _codes(report) == (IN_PROGRESS_WITHOUT_EXECUTION,)


# ---------------------------------------------------------------------------
# Dependency correspondence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DependencyCase:
    name: str
    labels: tuple[str, ...]
    dependency_done: bool
    expected_code: str | None
    repairability: Repairability | None = None
    severity: FindingSeverity = FindingSeverity.ERROR


_DEPENDENCY_CASES = (
    _DependencyCase("blocked while waiting", ("status:blocked",), False, None),
    _DependencyCase(
        "blocked after its dependency resolved",
        ("status:blocked",),
        True,
        BLOCKED_WITH_RESOLVED_DEPENDENCIES,
        Repairability.AUTOMATIC,
    ),
    _DependencyCase("queued once resolved", ("status:queued",), True, None),
    _DependencyCase(
        "queued while a dependency is unresolved",
        ("status:queued",),
        False,
        QUEUED_WITH_UNRESOLVED_DEPENDENCIES,
        Repairability.AUTOMATIC,
    ),
    _DependencyCase(
        "promotion held by a base-branch-red marker",
        ("status:blocked", "ci:base-branch-red"),
        True,
        BLOCKED_PROMOTION_HELD,
        Repairability.NONE,
        FindingSeverity.INFO,
    ),
    _DependencyCase(
        "promotion held by a pending recompute",
        ("status:blocked", "status:blocked-recompute"),
        True,
        BLOCKED_PROMOTION_HELD,
        Repairability.NONE,
        FindingSeverity.INFO,
    ),
)


@pytest.mark.parametrize("case", _DEPENDENCY_CASES, ids=lambda case: case.name)
def test_dependency_state_matches_blocked_and_queued(case: _DependencyCase) -> None:
    dependency = _desired_task(
        "dependency",
        704,
        lifecycle=TaskLifecycle.DONE if case.dependency_done else TaskLifecycle.OPEN,
    )
    report = _evaluate(
        _observed(
            _task_scope(
                704,
                labels=("status:done",) if case.dependency_done else ("status:queued",),
            ),
            _task_scope(705, labels=case.labels),
        ),
        _desired(
            dependency,
            _desired_task("status-policy", 705, depends_on=("dependency",)),
        ),
    )

    if case.expected_code is None:
        assert _codes(report) == ()
        return
    finding = _only(report, case.expected_code)
    assert finding.repairability is case.repairability
    assert finding.severity is case.severity
    assert finding.subject_id == "705"


def test_dependency_completed_in_the_same_cycle_unblocks_the_dependent() -> None:
    """The label of a dependency finished this cycle may not have flipped yet."""
    report = _evaluate(
        _observed(
            _task_scope(
                704,
                labels=("status:in-progress",),
                execution_kind=EXECUTION_KIND_LOCAL,
            ),
            _task_scope(705, labels=("status:blocked",)),
        ),
        _desired(
            _desired_task("dependency", 704, lifecycle=TaskLifecycle.DONE),
            _desired_task("status-policy", 705, depends_on=("dependency",)),
        ),
    )

    assert _codes(report) == (BLOCKED_WITH_RESOLVED_DEPENDENCIES,)


def test_dependency_completed_outside_the_task_slice_unblocks_the_dependent() -> None:
    report = _evaluate(
        _observed(_task_scope(705, labels=("status:blocked",))),
        _desired(
            _desired_task("status-policy", 705, depends_on=("external",)),
            completed=("external",),
        ),
    )

    finding = _only(report, BLOCKED_WITH_RESOLVED_DEPENDENCIES)
    assert finding.expected.value == "status:queued"


def test_dependency_checks_are_skipped_without_a_desired_task() -> None:
    report = _evaluate(
        _observed(_task_scope(705, labels=("status:blocked",))), _desired()
    )

    assert _codes(report) == ()


# ---------------------------------------------------------------------------
# Uncertainty
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fact_name",
    (
        FACT_EXECUTION_KIND,
        FACT_ISSUE_LABELS,
        FACT_ISSUE_STATE,
        FACT_ISSUE_STATUS_LABELS,
    ),
)
def test_uncertain_facts_replace_every_other_task_finding(fact_name: str) -> None:
    report = _evaluate(
        _observed(
            _task_scope(
                705, labels=("status:done", "status:queued"), uncertain=(fact_name,)
            )
        ),
        _desired(_desired_task("status-policy", 705)),
    )

    finding = _only(report, STATUS_OBSERVATION_UNKNOWN)
    assert _codes(report) == (STATUS_OBSERVATION_UNKNOWN,)
    assert finding.repairability is Repairability.NONE
    assert finding.severity is FindingSeverity.WARNING
    assert f"fact={fact_name}" in finding.observed.details
    assert plan_status_repairs(report) == ()


def test_absent_facts_are_reported_as_unknown() -> None:
    report = _evaluate(
        _observed(_task_scope(705, labels=(), omit=(FACT_ISSUE_STATUS_LABELS,))),
        _desired(_desired_task("status-policy", 705)),
    )

    finding = _only(report, STATUS_OBSERVATION_UNKNOWN)
    assert "certainty=absent" in finding.observed.details


def test_unreachable_forge_reports_once_and_empties_the_plan() -> None:
    report = _evaluate(
        _observed(
            _task_scope(705, labels=()),
            forge_certainty=UNKNOWN,
        ),
        _desired(_desired_task("status-policy", 705)),
    )

    forge_finding = _only(report, FORGE_OBSERVATION_UNKNOWN)
    assert forge_finding.scope is ConsistencyScope.REPOSITORY
    assert forge_finding.repairability is Repairability.NONE
    assert PRIMARY_STATUS_MISSING in _codes(report)
    assert plan_status_repairs(report) == ()


@pytest.mark.parametrize("issue_state", (None, "CLOSED", "closed"))
def test_closed_or_absent_issues_are_not_evaluated(issue_state: str | None) -> None:
    report = _evaluate(
        _observed(
            _task_scope(
                705, labels=("status:done", "status:queued"), issue_state=issue_state
            )
        ),
        _desired(_desired_task("status-policy", 705)),
    )

    assert _codes(report) == ()


# ---------------------------------------------------------------------------
# Forced serial
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ForcedSerialCase:
    name: str
    forced_serial: bool
    labelled: bool
    expected: bool


_FORCED_SERIAL_CASES = (
    _ForcedSerialCase("agreeing on forced serial", True, True, False),
    _ForcedSerialCase("agreeing on normal dispatch", False, False, False),
    _ForcedSerialCase("plan forces serial but no label", True, False, True),
    _ForcedSerialCase("label forces serial but plan does not", False, True, True),
)


@pytest.mark.parametrize("case", _FORCED_SERIAL_CASES, ids=lambda case: case.name)
def test_forced_serial_agreement(case: _ForcedSerialCase) -> None:
    labels = ("status:in-progress",) + (
        ("status:force-serial",) if case.labelled else ()
    )
    report = _evaluate(
        _observed(_task_scope(705, labels=labels, execution_kind=EXECUTION_KIND_LOCAL)),
        _desired(
            _desired_task("status-policy", 705, forced_serial=case.forced_serial),
            active=("status-policy",),
        ),
    )

    if not case.expected:
        assert _codes(report) == ()
        return
    finding = _only(report, FORCED_SERIAL_MISMATCH)
    assert finding.scope is ConsistencyScope.REPOSITORY
    assert finding.subject_id is None
    assert finding.repairability is Repairability.MANUAL
    assert plan_status_repairs(report) == ()


def test_forced_serial_ignores_labels_on_inactive_tasks() -> None:
    report = _evaluate(
        _observed(_task_scope(705, labels=("status:queued", "status:force-serial"))),
        _desired(_desired_task("status-policy", 705)),
    )

    assert _codes(report) == ()


def test_forced_serial_stays_silent_while_a_label_is_uncertain() -> None:
    report = _evaluate(
        _observed(
            _task_scope(
                705,
                labels=("status:in-progress",),
                execution_kind=EXECUTION_KIND_LOCAL,
                uncertain=(FACT_ISSUE_STATUS_LABELS,),
            )
        ),
        _desired(
            _desired_task("status-policy", 705, forced_serial=True),
            active=("status-policy",),
        ),
    )

    assert _codes(report) == (STATUS_OBSERVATION_UNKNOWN,)


# ---------------------------------------------------------------------------
# Purity and determinism
# ---------------------------------------------------------------------------


def test_evaluation_is_deterministic_and_leaves_inputs_untouched() -> None:
    observed = _observed(
        _task_scope(705, labels=("status:done", "status:queued")),
        _task_scope(706, labels=("status:blocked",)),
    )
    desired = _desired(
        _desired_task("status-policy", 705, lifecycle=TaskLifecycle.DONE),
        _desired_task("shadow-supervisor", 706, depends_on=("status-policy",)),
    )
    observed_before = copy.deepcopy(observed)
    desired_before = copy.deepcopy(desired)

    first = _evaluate(observed, desired)
    second = _evaluate(observed, desired)

    assert first == second
    assert observed == observed_before
    assert desired == desired_before
    assert plan_status_repairs(first) == plan_status_repairs(second)


# ---------------------------------------------------------------------------
# Repair planning
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Malformed input is never mistaken for divergence
# ---------------------------------------------------------------------------


def _malformed_task(**overrides: object) -> ScopedObservations:
    values: dict[str, object] = {
        FACT_EXECUTION_KIND: EXECUTION_KIND_NONE,
        FACT_ISSUE_LABELS: ("status:queued",),
        FACT_ISSUE_STATE: "OPEN",
        FACT_ISSUE_STATUS_LABELS: ("status:queued",),
    }
    values.update(overrides)
    return ScopedObservations(
        scope=ConsistencyScope.TASK,
        subject_id="705",
        facts=tuple(
            _observation(name, value) for name, value in sorted(values.items())
        ),
    )


@pytest.mark.parametrize(
    "overrides",
    (
        {FACT_ISSUE_STATUS_LABELS: "status:queued"},
        {FACT_ISSUE_STATUS_LABELS: (7, None)},
        {FACT_ISSUE_STATE: 705},
        {FACT_EXECUTION_KIND: None},
    ),
    ids=(
        "labels are not a tuple",
        "labels are not strings",
        "state is not a string",
        "execution kind is not a string",
    ),
)
def test_unusable_fact_shapes_read_as_uncertainty_not_absence(
    overrides: dict[str, object],
) -> None:
    report = _evaluate(
        _observed(_malformed_task(**overrides)),
        _desired(_desired_task("status-policy", 705)),
    )

    finding = _only(report, STATUS_OBSERVATION_UNKNOWN)
    assert "usability=unusable" in finding.observed.details
    assert plan_status_repairs(report) == ()


def test_a_task_scope_without_a_subject_is_skipped() -> None:
    anonymous = ScopedObservations(
        scope=ConsistencyScope.TASK,
        subject_id=None,
        facts=(_observation(FACT_ISSUE_STATUS_LABELS, ()),),
    )

    report = _evaluate(_observed(anonymous), _desired())

    assert _codes(report) == ()


def test_unnamed_unresolved_dependencies_still_demote_a_queued_task() -> None:
    desired = _desired(_desired_task("status-policy", 705, depends_on=("external",)))
    reshaped = DesiredRepositoryState(
        repository_id=desired.repository_id,
        facts=tuple(
            DesiredFact(
                name=fact.name,
                value="not-a-tuple"
                if fact.name == "task.unresolved_dependencies"
                else fact.value,
                scope=fact.scope,
                subject_id=fact.subject_id,
                reason=fact.reason,
            )
            for fact in desired.facts
        ),
    )

    report = _evaluate(_observed(_task_scope(705, labels=("status:queued",))), reshaped)

    finding = _only(report, QUEUED_WITH_UNRESOLVED_DEPENDENCIES)
    assert "unresolved dependencies: (unnamed)" in finding.observed.details


def test_forced_serial_needs_a_desired_dispatch_fact() -> None:
    desired = DesiredRepositoryState(repository_id=REPOSITORY, facts=())

    report = _evaluate(
        _observed(_task_scope(705, labels=("status:queued", "status:force-serial"))),
        desired,
    )

    assert _codes(report) == ()


def test_one_uncertain_label_does_not_hide_a_known_forced_serial_owner() -> None:
    report = _evaluate(
        _observed(
            _task_scope(
                705,
                labels=("status:in-progress", "status:force-serial"),
                execution_kind=EXECUTION_KIND_LOCAL,
            ),
            _task_scope(
                706,
                labels=("status:in-progress",),
                execution_kind=EXECUTION_KIND_LOCAL,
                uncertain=(FACT_ISSUE_STATUS_LABELS,),
            ),
        ),
        _desired(
            _desired_task("status-policy", 705),
            _desired_task("shadow-supervisor", 706),
            active=("shadow-supervisor", "status-policy"),
        ),
    )

    finding = _only(report, FORCED_SERIAL_MISMATCH)
    assert finding.observed.value == ("705",)


# ---------------------------------------------------------------------------
# The planner refuses findings it cannot act on deterministically
# ---------------------------------------------------------------------------


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
