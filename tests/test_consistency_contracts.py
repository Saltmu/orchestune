from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from orchestune.consistency import (
    ConsistencyEngine,
    ConsistencyFinding,
    ConsistencyReport,
    ConsistencyScope,
    DesiredFact,
    DesiredRepositoryState,
    Evidence,
    FindingSeverity,
    IntentStatus,
    Invariant,
    Observation,
    ObservationCertainty,
    ObservedRepositoryState,
    Observer,
    Repairability,
    RepairCommand,
    RepairExecutor,
    RepairPlanner,
    RepairResult,
    RepairStatus,
    ScopedObservations,
    StateChanged,
    TransitionIntent,
)

NOW = datetime(2026, 8, 28, tzinfo=UTC)


def _observed() -> ObservedRepositoryState:
    return ObservedRepositoryState(
        repository_id="Saltmu/orchestune",
        observed_at=NOW,
        observations=(
            ScopedObservations(
                scope=ConsistencyScope.REPOSITORY,
                facts=(
                    Observation(
                        name="forge_available",
                        value=True,
                        certainty=ObservationCertainty.KNOWN,
                        source="github-api",
                        observed_at=NOW,
                    ),
                ),
            ),
        ),
    )


def _desired() -> DesiredRepositoryState:
    return DesiredRepositoryState(
        repository_id="Saltmu/orchestune",
        facts=(
            DesiredFact(
                name="primary_status_count",
                value=1,
                scope=ConsistencyScope.TASK,
                subject_id="701",
                reason="a task has exactly one primary status",
            ),
        ),
    )


def _finding(
    code: str,
    scope: ConsistencyScope,
    *,
    subject_id: str | None = None,
) -> ConsistencyFinding:
    return ConsistencyFinding(
        code=code,
        scope=scope,
        subject_id=subject_id,
        severity=FindingSeverity.ERROR,
        expected=Evidence("one primary status", ("expected count: 1",)),
        observed=Evidence("invalid primary status count", ("observed count: 2",)),
        repairability=Repairability.AUTOMATIC,
    )


def test_observation_preserves_certainty_provenance_and_diagnostics() -> None:
    observation = Observation(
        name="issue_labels",
        value=None,
        certainty=ObservationCertainty.UNKNOWN,
        source="github-api",
        observed_at=NOW,
        diagnostics=("request timed out",),
    )

    assert observation.certainty is ObservationCertainty.UNKNOWN
    assert observation.source == "github-api"
    assert observation.observed_at == NOW
    assert observation.diagnostics == ("request timed out",)
    assert {member.value for member in ObservationCertainty} == {
        "known",
        "unknown",
        "stale",
    }


@pytest.mark.parametrize(
    "model,attribute,new_value",
    [
        (_observed(), "repository_id", "other/repository"),
        (_desired(), "repository_id", "other/repository"),
        (
            TransitionIntent(
                intent_id="status-701-1",
                scope=ConsistencyScope.TASK,
                subject_id="701",
                operation="set-status",
                created_at=NOW,
            ),
            "status",
            IntentStatus.APPLIED,
        ),
        (_finding("task.multiple-statuses", ConsistencyScope.TASK), "code", "x"),
        (
            ConsistencyReport(
                repository_id="Saltmu/orchestune",
                findings=(_finding("x", ConsistencyScope.REPOSITORY),),
            ),
            "findings",
            (),
        ),
        (
            StateChanged(
                scope=ConsistencyScope.TASK,
                subject_id="701",
                fields=("labels",),
                source="dispatch",
                occurred_at=NOW,
            ),
            "source",
            "other",
        ),
        (
            RepairCommand(
                code="task.set-status",
                scope=ConsistencyScope.TASK,
                subject_id="701",
                idempotency_key="task-701-status",
            ),
            "code",
            "x",
        ),
    ],
)
def test_core_state_and_result_models_are_frozen(
    model: object, attribute: str, new_value: object
) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(model, attribute, new_value)


def test_finding_keeps_stable_identity_evidence_and_repairability() -> None:
    finding = _finding(
        "task.multiple-primary-statuses",
        ConsistencyScope.TASK,
        subject_id="701",
    )

    assert finding.code == "task.multiple-primary-statuses"
    assert finding.scope is ConsistencyScope.TASK
    assert finding.subject_id == "701"
    assert finding.severity is FindingSeverity.ERROR
    assert finding.expected.details == ("expected count: 1",)
    assert finding.observed.details == ("observed count: 2",)
    assert finding.repairability is Repairability.AUTOMATIC


class _Observer:
    def observe(self) -> ObservedRepositoryState:
        return _observed()


class _Invariant:
    code = "task.invalid-status"
    scope = ConsistencyScope.TASK

    def evaluate(
        self,
        observed: ObservedRepositoryState,
        desired: DesiredRepositoryState,
    ) -> tuple[ConsistencyFinding, ...]:
        return (_finding(self.code, self.scope, subject_id="701"),)


class _Planner:
    def plan(self, report: ConsistencyReport) -> tuple[RepairCommand, ...]:
        return (
            RepairCommand(
                code="task.set-status",
                scope=ConsistencyScope.TASK,
                subject_id=report.findings[0].subject_id,
                idempotency_key="task-701-status",
            ),
        )


class _Executor:
    def execute(self, command: RepairCommand) -> RepairResult:
        return RepairResult(command=command, status=RepairStatus.APPLIED)


def test_side_effect_boundaries_are_runtime_checkable_protocols() -> None:
    assert isinstance(_Observer(), Observer)
    assert isinstance(_Invariant(), Invariant)
    assert isinstance(_Planner(), RepairPlanner)
    assert isinstance(_Executor(), RepairExecutor)


class _RecordingInvariant:
    def __init__(
        self,
        code: str,
        scope: ConsistencyScope,
        calls: list[str],
        findings: tuple[ConsistencyFinding, ...],
    ) -> None:
        self.code = code
        self.scope = scope
        self._calls = calls
        self._findings = findings

    def evaluate(
        self,
        observed: ObservedRepositoryState,
        desired: DesiredRepositoryState,
    ) -> tuple[ConsistencyFinding, ...]:
        self._calls.append(self.code)
        return self._findings


def test_engine_evaluates_every_scope_in_deterministic_order_without_short_circuit() -> (
    None
):
    calls: list[str] = []
    invariants = (
        _RecordingInvariant(
            "task.z-last",
            ConsistencyScope.TASK,
            calls,
            (_finding("z", ConsistencyScope.TASK, subject_id="9"),),
        ),
        _RecordingInvariant(
            "repository.b-first",
            ConsistencyScope.REPOSITORY,
            calls,
            (_finding("b", ConsistencyScope.REPOSITORY),),
        ),
        _RecordingInvariant(
            "parent.c-middle",
            ConsistencyScope.PARENT,
            calls,
            (),
        ),
        _RecordingInvariant(
            "repository.a-first",
            ConsistencyScope.REPOSITORY,
            calls,
            (_finding("a", ConsistencyScope.REPOSITORY),),
        ),
    )

    engine = ConsistencyEngine(invariants)
    report = engine.evaluate(_observed(), _desired())

    assert [invariant.code for invariant in engine.invariants] == [
        "repository.a-first",
        "repository.b-first",
        "parent.c-middle",
        "task.z-last",
    ]
    assert calls == [
        "repository.a-first",
        "repository.b-first",
        "parent.c-middle",
        "task.z-last",
    ]
    assert report.evaluated_invariants == tuple(calls)
    assert [finding.code for finding in report.findings] == ["a", "b", "z"]


def test_engine_normalizes_findings_returned_in_unstable_order() -> None:
    calls: list[str] = []
    unstable = _RecordingInvariant(
        "repository.all",
        ConsistencyScope.REPOSITORY,
        calls,
        (
            _finding("task.z", ConsistencyScope.TASK, subject_id="20"),
            _finding("task.a", ConsistencyScope.TASK, subject_id="10"),
            _finding("parent.a", ConsistencyScope.PARENT, subject_id="700"),
        ),
    )

    first = ConsistencyEngine((unstable,)).evaluate(_observed(), _desired())
    second = ConsistencyEngine((unstable,)).evaluate(_observed(), _desired())

    assert first == second
    assert [finding.code for finding in first.findings] == [
        "parent.a",
        "task.a",
        "task.z",
    ]


def test_engine_rejects_repository_mismatch_before_evaluating_invariants() -> None:
    calls: list[str] = []
    invariant = _RecordingInvariant(
        "repository.all",
        ConsistencyScope.REPOSITORY,
        calls,
        (),
    )
    desired = DesiredRepositoryState(repository_id="other/repository")

    with pytest.raises(ValueError, match="repository_id mismatch"):
        ConsistencyEngine((invariant,)).evaluate(_observed(), desired)

    assert calls == []
