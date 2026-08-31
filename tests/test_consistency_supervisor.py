"""ConsistencySupervisorの純粋なshadow scan契約 (#706)。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from orchestune.consistency.engine import ConsistencyEngine
from orchestune.consistency.models import (
    ConsistencyFinding,
    ConsistencyReport,
    ConsistencyScope,
    DesiredRepositoryState,
    Evidence,
    FindingSeverity,
    Observation,
    ObservationCertainty,
    ObservedRepositoryState,
    Repairability,
    RepairCommand,
    RepairResult,
    RepairStatus,
    ScopedObservations,
    StateChanged,
)
from orchestune.consistency.supervisor import (
    ConsistencyMode,
    ConsistencySupervisor,
    RepairDisposition,
    ScanKind,
    consistency_cycle_to_dict,
)

NOW = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)


def _observation(
    name: str,
    value: str | int | None,
    *,
    certainty: ObservationCertainty = ObservationCertainty.KNOWN,
    observed_at: datetime = NOW,
    diagnostics: tuple[str, ...] = (),
    source: str = "test",
) -> Observation:
    return Observation(
        name=name,
        certainty=certainty,
        source=source,
        observed_at=observed_at,
        value=value,
        diagnostics=diagnostics,
    )


def _snapshot(
    value: str,
    *,
    observed_at: datetime = NOW,
    extra_facts: tuple[Observation, ...] = (),
) -> ObservedRepositoryState:
    return ObservedRepositoryState(
        repository_id="owner/repo",
        observed_at=observed_at,
        observations=(
            ScopedObservations(
                scope=ConsistencyScope.REPOSITORY,
                facts=(_observation("repository", "ready", observed_at=observed_at),),
            ),
            ScopedObservations(
                scope=ConsistencyScope.TASK,
                subject_id="706",
                facts=(
                    _observation("status", value, observed_at=observed_at),
                    *extra_facts,
                ),
            ),
        ),
    )


class _SequenceObserver:
    def __init__(self, *snapshots: ObservedRepositoryState) -> None:
        self._snapshots = iter(snapshots)

    def observe(self) -> ObservedRepositoryState:
        return next(self._snapshots)


class _FailingObserver:
    def observe(self) -> ObservedRepositoryState:
        raise OSError("forge unavailable")


class _StaticDeriver:
    def derive(self, observed: ObservedRepositoryState) -> DesiredRepositoryState:
        return DesiredRepositoryState(repository_id=observed.repository_id)


@dataclass
class _FindingInvariant:
    code: str
    scope: ConsistencyScope
    subject_id: str | None = None
    repairability: Repairability = Repairability.AUTOMATIC
    calls: int = 0

    def evaluate(
        self,
        observed: ObservedRepositoryState,
        desired: DesiredRepositoryState,
    ) -> tuple[ConsistencyFinding, ...]:
        self.calls += 1
        return (
            ConsistencyFinding(
                code=f"{self.code}.finding",
                scope=self.scope,
                subject_id=self.subject_id,
                severity=FindingSeverity.WARNING,
                expected=Evidence(summary="expected"),
                observed=Evidence(summary="observed"),
                repairability=self.repairability,
            ),
        )


class _Planner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(self, report: ConsistencyReport) -> tuple[RepairCommand, ...]:
        self.calls += 1
        return (
            RepairCommand(
                code="repair.status",
                scope=ConsistencyScope.TASK,
                subject_id="706",
                idempotency_key="status:706",
                parameters=(("finding_code", report.findings[0].code),),
            ),
        )


class _MissingFindingCodePlanner:
    def plan(self, report: ConsistencyReport) -> tuple[RepairCommand, ...]:
        return (
            RepairCommand(
                code="repair.status",
                scope=ConsistencyScope.TASK,
                subject_id="706",
                idempotency_key="missing-finding-code",
            ),
        )


class _RecordingExecutor:
    def __init__(self, status: RepairStatus = RepairStatus.APPLIED) -> None:
        self.status = status
        self.commands: list[RepairCommand] = []

    def execute(self, command: RepairCommand) -> RepairResult:
        self.commands.append(command)
        return RepairResult(command=command, status=self.status)


@dataclass
class _ChangingInvariant:
    code: str = "task.changing"
    scope: ConsistencyScope = ConsistencyScope.TASK
    calls: int = 0

    def evaluate(self, observed, desired) -> tuple[ConsistencyFinding, ...]:
        self.calls += 1
        return (
            ConsistencyFinding(
                code=f"task.changing-{self.calls}",
                scope=self.scope,
                subject_id="706",
                severity=FindingSeverity.WARNING,
                expected=Evidence(summary="healthy"),
                observed=Evidence(summary="drifted"),
                repairability=Repairability.AUTOMATIC,
            ),
        )


class _FindingPlanner:
    def plan(self, report: ConsistencyReport) -> tuple[RepairCommand, ...]:
        return tuple(
            RepairCommand(
                code="repair.changing",
                scope=finding.scope,
                subject_id=finding.subject_id,
                idempotency_key=finding.code,
                parameters=(("finding_code", finding.code),),
            )
            for finding in report.findings
        )


@dataclass
class _ObservedStatusInvariant:
    code: str = "task.observed-status"
    scope: ConsistencyScope = ConsistencyScope.TASK

    def evaluate(self, observed, desired) -> tuple[ConsistencyFinding, ...]:
        return tuple(
            ConsistencyFinding(
                code=f"{self.code}.finding",
                scope=self.scope,
                subject_id=scoped.subject_id,
                severity=FindingSeverity.WARNING,
                expected=Evidence(summary="healthy"),
                observed=Evidence(summary="queued"),
                repairability=Repairability.AUTOMATIC,
            )
            for scoped in observed.observations
            for fact in scoped.facts
            if scoped.scope is ConsistencyScope.TASK
            and fact.name == "status"
            and fact.certainty is ObservationCertainty.KNOWN
            and fact.value == "queued"
        )


def _task_status_snapshot(
    first: Observation, second: Observation
) -> ObservedRepositoryState:
    return ObservedRepositoryState(
        repository_id="owner/repo",
        observed_at=NOW,
        observations=(
            ScopedObservations(
                scope=ConsistencyScope.TASK,
                subject_id="706",
                facts=(first,),
            ),
            ScopedObservations(
                scope=ConsistencyScope.TASK,
                subject_id="707",
                facts=(second,),
            ),
        ),
    )


def test_full_scans_are_deterministic_and_diff_ignores_collection_time() -> None:
    initial = _snapshot("queued")
    same_facts_later = _snapshot("queued", observed_at=NOW + timedelta(seconds=5))
    changed = _snapshot("in-progress", observed_at=NOW + timedelta(seconds=10))
    repeated = _snapshot("in-progress", observed_at=NOW + timedelta(seconds=15))
    invariant = _FindingInvariant("task.status", ConsistencyScope.TASK, "706")
    supervisor = ConsistencySupervisor(
        repository_id="owner/repo",
        engine=ConsistencyEngine((invariant,)),
    )
    observer = _SequenceObserver(initial, same_facts_later, changed, repeated)
    deriver = _StaticDeriver()

    start = supervisor.full_scan("start", observer=observer, deriver=deriver)
    unchanged = supervisor.full_scan("unchanged", observer=observer, deriver=deriver)
    end = supervisor.full_scan("end", observer=observer, deriver=deriver)
    repeat = supervisor.full_scan("repeat", observer=observer, deriver=deriver)

    assert start.kind is ScanKind.FULL
    assert start.state_changes == ()
    assert unchanged.state_changes == ()
    assert end.state_changes == (
        StateChanged(
            scope=ConsistencyScope.TASK,
            subject_id="706",
            fields=("status",),
            source="consistency-supervisor.full-scan",
            occurred_at=NOW + timedelta(seconds=10),
        ),
    )
    assert repeat.state_changes == ()
    assert start.report == unchanged.report == end.report == repeat.report


def test_targeted_scan_deduplicates_changes_and_limits_invariant_scopes() -> None:
    repository_invariant = _FindingInvariant(
        "repository.policy", ConsistencyScope.REPOSITORY
    )
    task_invariant = _FindingInvariant(
        "task.policy", ConsistencyScope.TASK, subject_id="706"
    )
    supervisor = ConsistencySupervisor(
        repository_id="owner/repo",
        engine=ConsistencyEngine((repository_invariant, task_invariant)),
    )
    change = StateChanged(
        scope=ConsistencyScope.TASK,
        subject_id="706",
        fields=("status",),
        source="dispatch.reconciliation",
        occurred_at=NOW,
    )

    scan = supervisor.targeted_scan(
        "pipeline",
        (change, change),
        observer=_SequenceObserver(_snapshot("in-progress")),
        deriver=_StaticDeriver(),
    )

    assert scan is not None
    assert scan.kind is ScanKind.TARGETED
    assert scan.state_changes == (change,)
    assert scan.report.evaluated_invariants == ("task.policy",)
    assert [finding.code for finding in scan.report.findings] == ["task.policy.finding"]
    assert repository_invariant.calls == 0
    assert task_invariant.calls == 1
    assert (
        supervisor.targeted_scan(
            "pipeline-repeat",
            (change,),
            observer=_FailingObserver(),
            deriver=_StaticDeriver(),
        )
        is None
    )


def test_shadow_scan_reports_unknown_facts_and_plans_without_executing() -> None:
    unknown = _observation(
        "forge_reachable",
        None,
        certainty=ObservationCertainty.UNKNOWN,
        diagnostics=("timeout",),
    )
    invariant = _FindingInvariant("task.status", ConsistencyScope.TASK, "706")
    planner = _Planner()
    supervisor = ConsistencySupervisor(
        repository_id="owner/repo",
        engine=ConsistencyEngine((invariant,)),
        repair_planners=(planner,),
    )

    scan = supervisor.full_scan(
        "start",
        observer=_SequenceObserver(_snapshot("queued", extra_facts=(unknown,))),
        deriver=_StaticDeriver(),
    )

    assert [
        (fact.subject_id, fact.name, fact.diagnostics) for fact in scan.unknown_facts
    ] == [("706", "forge_reachable", ("timeout",))]
    assert scan.repair_candidates == (
        RepairCommand(
            code="repair.status",
            scope=ConsistencyScope.TASK,
            subject_id="706",
            idempotency_key="status:706",
            parameters=(("finding_code", "task.status.finding"),),
        ),
    )
    assert planner.calls == 1


def test_observation_failure_becomes_report_data_instead_of_escaping() -> None:
    supervisor = ConsistencySupervisor(
        repository_id="owner/repo",
        engine=ConsistencyEngine(()),
    )

    scan = supervisor.full_scan(
        "end", observer=_FailingObserver(), deriver=_StaticDeriver()
    )

    assert scan.report.repository_id == "owner/repo"
    assert [finding.code for finding in scan.report.findings] == [
        "supervisor.observation-failed"
    ]
    assert scan.unknown_facts[0].name == "supervisor.observation"
    assert scan.unknown_facts[0].diagnostics == ("OSError: forge unavailable",)
    assert scan.diagnostics == ("observation failed: OSError: forge unavailable",)


def test_cycle_report_serialization_is_stable_and_json_safe() -> None:
    supervisor = ConsistencySupervisor(
        repository_id="owner/repo",
        engine=ConsistencyEngine(()),
    )
    supervisor.full_scan(
        "start",
        observer=_SequenceObserver(_snapshot("queued")),
        deriver=_StaticDeriver(),
    )
    supervisor.full_scan(
        "end",
        observer=_SequenceObserver(
            _snapshot("in-progress", observed_at=NOW + timedelta(seconds=1))
        ),
        deriver=_StaticDeriver(),
    )

    payload = consistency_cycle_to_dict(
        supervisor.cycle_report(mode=ConsistencyMode.SHADOW)
    )

    assert payload["mode"] == "shadow"
    assert [scan["boundary"] for scan in payload["scans"]] == ["start", "end"]
    assert payload["scans"][1]["state_changes"][0] == {
        "scope": "task",
        "subject_id": "706",
        "fields": ["status"],
        "source": "consistency-supervisor.full-scan",
        "occurred_at": "2026-08-29T10:00:01+00:00",
    }
    json.dumps(payload)


def test_repair_mode_executes_only_allowlisted_command_and_reobserves() -> None:
    initial = _snapshot("queued")
    repaired = _snapshot("healthy", observed_at=NOW + timedelta(seconds=1))
    invariant = _FindingInvariant("task.status", ConsistencyScope.TASK, "706")
    planner = _Planner()
    executor = _RecordingExecutor()
    supervisor = ConsistencySupervisor(
        repository_id="owner/repo",
        engine=ConsistencyEngine((invariant,)),
        repair_planners=(planner,),
    )
    start = supervisor.full_scan(
        "end", observer=_SequenceObserver(initial), deriver=_StaticDeriver()
    )

    supervisor.repair_until_stable(
        start,
        observer=_SequenceObserver(repaired),
        deriver=_StaticDeriver(),
        executor=executor,
        allowlist=("repair.status",),
        max_passes=2,
    )

    report = supervisor.cycle_report(mode=ConsistencyMode.REPAIR)
    assert executor.commands == list(start.repair_candidates)
    assert len(report.repair_passes) == 1
    assert report.repair_passes[0].number == 1
    assert [scan.boundary for scan in report.scans] == ["end", "repair-1"]
    assert report.repair_outcomes[0].disposition is RepairDisposition.UNRESOLVED


def test_repair_mode_defers_non_allowlisted_and_reports_executor_failure() -> None:
    invariant = _FindingInvariant("task.status", ConsistencyScope.TASK, "706")
    planner = _Planner()
    supervisor = ConsistencySupervisor(
        repository_id="owner/repo",
        engine=ConsistencyEngine((invariant,)),
        repair_planners=(planner,),
    )
    start = supervisor.full_scan(
        "end", observer=_SequenceObserver(_snapshot("queued")), deriver=_StaticDeriver()
    )
    supervisor.repair_until_stable(
        start,
        observer=_SequenceObserver(_snapshot("queued")),
        deriver=_StaticDeriver(),
        executor=_RecordingExecutor(RepairStatus.FAILED),
        allowlist=("repair.status",),
        max_passes=1,
    )
    failed = supervisor.cycle_report(mode=ConsistencyMode.REPAIR)
    assert failed.repair_outcomes[0].disposition is RepairDisposition.FAILED

    deferred_supervisor = ConsistencySupervisor(
        repository_id="owner/repo",
        engine=ConsistencyEngine((invariant,)),
        repair_planners=(planner,),
    )
    deferred_start = deferred_supervisor.full_scan(
        "end", observer=_SequenceObserver(_snapshot("queued")), deriver=_StaticDeriver()
    )
    deferred_supervisor.repair_until_stable(
        deferred_start,
        observer=_FailingObserver(),
        deriver=_StaticDeriver(),
        executor=_RecordingExecutor(),
        allowlist=(),
        max_passes=1,
    )
    deferred = deferred_supervisor.cycle_report(mode=ConsistencyMode.REPAIR)
    assert deferred.repair_passes == ()
    assert deferred.repair_outcomes[0].disposition is RepairDisposition.DEFERRED


def test_repair_outcome_is_unknown_when_authoritative_reobservation_fails() -> None:
    invariant = _FindingInvariant("task.status", ConsistencyScope.TASK, "706")
    supervisor = ConsistencySupervisor(
        repository_id="owner/repo",
        engine=ConsistencyEngine((invariant,)),
        repair_planners=(_Planner(),),
    )
    start = supervisor.full_scan(
        "end", observer=_SequenceObserver(_snapshot("queued")), deriver=_StaticDeriver()
    )

    supervisor.repair_until_stable(
        start,
        observer=_FailingObserver(),
        deriver=_StaticDeriver(),
        executor=_RecordingExecutor(),
        allowlist=("repair.status",),
        max_passes=1,
    )

    outcome = next(
        item
        for item in supervisor.cycle_report(mode=ConsistencyMode.REPAIR).repair_outcomes
        if item.finding_code == "task.status.finding"
    )
    assert outcome.disposition is RepairDisposition.OBSERVATION_UNKNOWN
    assert outcome.diagnostics == ("OSError: forge unavailable",)


def test_reobservation_unknown_is_scoped_to_the_affected_finding() -> None:
    initial = _task_status_snapshot(
        _observation("status", "queued"),
        _observation("status", "queued"),
    )
    repaired = _task_status_snapshot(
        _observation("status", "healthy"),
        _observation(
            "status",
            None,
            certainty=ObservationCertainty.UNKNOWN,
            diagnostics=("task 707 unavailable",),
            source="consistency-supervisor",
        ),
    )
    supervisor = ConsistencySupervisor(
        repository_id="owner/repo",
        engine=ConsistencyEngine((_ObservedStatusInvariant(),)),
        repair_planners=(_FindingPlanner(),),
    )
    start = supervisor.full_scan(
        "end", observer=_SequenceObserver(initial), deriver=_StaticDeriver()
    )

    supervisor.repair_until_stable(
        start,
        observer=_SequenceObserver(repaired),
        deriver=_StaticDeriver(),
        executor=_RecordingExecutor(),
        allowlist=("repair.changing",),
        max_passes=1,
    )

    outcomes = {
        outcome.subject_id: outcome
        for outcome in supervisor.cycle_report(
            mode=ConsistencyMode.REPAIR
        ).repair_outcomes
        if outcome.finding_code == "task.observed-status.finding"
    }
    assert outcomes["706"].disposition is RepairDisposition.RESOLVED
    assert outcomes["706"].diagnostics == ()
    assert outcomes["707"].disposition is RepairDisposition.OBSERVATION_UNKNOWN
    assert outcomes["707"].diagnostics == ("task 707 unavailable",)


def test_repair_mode_stops_at_configured_pass_bound() -> None:
    supervisor = ConsistencySupervisor(
        repository_id="owner/repo",
        engine=ConsistencyEngine((_ChangingInvariant(),)),
        repair_planners=(_FindingPlanner(),),
    )
    start = supervisor.full_scan(
        "end", observer=_SequenceObserver(_snapshot("one")), deriver=_StaticDeriver()
    )
    executor = _RecordingExecutor()

    supervisor.repair_until_stable(
        start,
        observer=_SequenceObserver(_snapshot("two"), _snapshot("three")),
        deriver=_StaticDeriver(),
        executor=executor,
        allowlist=("repair.changing",),
        max_passes=2,
    )

    report = supervisor.cycle_report(mode=ConsistencyMode.REPAIR)
    assert [repair_pass.number for repair_pass in report.repair_passes] == [1, 2]
    assert [command.idempotency_key for command in executor.commands] == [
        "task.changing-1",
        "task.changing-2",
    ]
    assert any(
        outcome.finding_code == "task.changing-3"
        and outcome.disposition is RepairDisposition.DEFERRED
        for outcome in report.repair_outcomes
    )


def test_repair_mode_never_executes_non_repairable_finding() -> None:
    invariant = _FindingInvariant(
        "task.manual",
        ConsistencyScope.TASK,
        "706",
        repairability=Repairability.MANUAL,
    )
    supervisor = ConsistencySupervisor(
        repository_id="owner/repo",
        engine=ConsistencyEngine((invariant,)),
        repair_planners=(_Planner(),),
    )
    start = supervisor.full_scan(
        "end", observer=_SequenceObserver(_snapshot("queued")), deriver=_StaticDeriver()
    )
    executor = _RecordingExecutor()

    supervisor.repair_until_stable(
        start,
        observer=_FailingObserver(),
        deriver=_StaticDeriver(),
        executor=executor,
        allowlist=("repair.status",),
        max_passes=1,
    )

    report = supervisor.cycle_report(mode=ConsistencyMode.REPAIR)
    assert executor.commands == []
    assert report.repair_passes == ()
    assert report.repair_outcomes[0].disposition is RepairDisposition.DEFERRED


def test_repair_mode_fails_closed_when_command_omits_finding_code() -> None:
    supervisor = ConsistencySupervisor(
        repository_id="owner/repo",
        engine=ConsistencyEngine(
            (_FindingInvariant("task.status", ConsistencyScope.TASK, "706"),)
        ),
        repair_planners=(_MissingFindingCodePlanner(),),
    )
    start = supervisor.full_scan(
        "end", observer=_SequenceObserver(_snapshot("queued")), deriver=_StaticDeriver()
    )
    executor = _RecordingExecutor()

    supervisor.repair_until_stable(
        start,
        observer=_FailingObserver(),
        deriver=_StaticDeriver(),
        executor=executor,
        allowlist=("repair.status",),
        max_passes=1,
    )

    assert executor.commands == []
    report = supervisor.cycle_report(mode=ConsistencyMode.REPAIR)
    assert report.repair_passes == ()
    assert report.repair_outcomes[0].disposition is RepairDisposition.DEFERRED
