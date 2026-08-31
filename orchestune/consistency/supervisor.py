"""Orchestration for repository consistency scans and guarded repair.

The supervisor coordinates observation, desired-state derivation, invariant
evaluation, and repair planning. Shadow scans never receive an executor;
repair mode accepts one explicitly and keeps every mutation bounded.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from orchestune.consistency.contracts import Observer, RepairExecutor, RepairPlanner
from orchestune.consistency.engine import ConsistencyEngine
from orchestune.consistency.models import (
    ConsistencyFinding,
    ConsistencyReport,
    ConsistencyScope,
    DesiredRepositoryState,
    Evidence,
    FindingSeverity,
    ObservationCertainty,
    ObservedRepositoryState,
    Repairability,
    RepairCommand,
    RepairResult,
    RepairStatus,
    StateChanged,
)

_SCOPE_ORDER = {
    ConsistencyScope.REPOSITORY: 0,
    ConsistencyScope.PARENT: 1,
    ConsistencyScope.TASK: 2,
}


class ConsistencyMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    REPAIR = "repair"


class RepairDisposition(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    DEFERRED = "deferred"
    FAILED = "failed"
    OBSERVATION_UNKNOWN = "observation-unknown"


class ScanKind(StrEnum):
    FULL = "full"
    TARGETED = "targeted"


class DesiredStateDeriver(Protocol):
    """Pure adapter from the latest observation to desired state."""

    def derive(self, observed: ObservedRepositoryState) -> DesiredRepositoryState: ...


@dataclass(frozen=True, slots=True)
class ConsistencyUnknownFact:
    scope: ConsistencyScope
    name: str
    source: str
    certainty: ObservationCertainty
    subject_id: str | None = None
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConsistencyScanResult:
    boundary: str
    kind: ScanKind
    report: ConsistencyReport
    unknown_facts: tuple[ConsistencyUnknownFact, ...] = ()
    repair_candidates: tuple[RepairCommand, ...] = ()
    state_changes: tuple[StateChanged, ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConsistencyRepairPass:
    number: int
    results: tuple[RepairResult, ...]


@dataclass(frozen=True, slots=True)
class ConsistencyRepairOutcome:
    finding_code: str
    scope: ConsistencyScope
    disposition: RepairDisposition
    subject_id: str | None = None
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConsistencyCycleReport:
    mode: ConsistencyMode
    scans: tuple[ConsistencyScanResult, ...] = ()
    repair_passes: tuple[ConsistencyRepairPass, ...] = ()
    repair_outcomes: tuple[ConsistencyRepairOutcome, ...] = ()


MAX_REPAIR_PASSES = 5


def _describe(exc: Exception) -> str:
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def _scope_key(scope: ConsistencyScope, subject_id: str | None) -> tuple[int, str]:
    return (_SCOPE_ORDER[scope], subject_id or "")


def _change_key(change: StateChanged) -> tuple[object, ...]:
    return (
        *_scope_key(change.scope, change.subject_id),
        tuple(sorted(set(change.fields))),
        change.source,
    )


def _observation_value(observation) -> tuple[object, ...]:
    """Compare fact meaning while ignoring collection timestamps."""
    return (
        observation.certainty,
        observation.source,
        observation.value,
        observation.diagnostics,
    )


def _snapshot_index(snapshot: ObservedRepositoryState) -> dict:
    return {
        (scope.scope, scope.subject_id): {
            observation.name: _observation_value(observation)
            for observation in scope.facts
        }
        for scope in snapshot.observations
    }


def _changed_fields(before: dict, after: dict, scope_key: tuple) -> tuple[str, ...]:
    before_facts = before.get(scope_key, {})
    after_facts = after.get(scope_key, {})
    names = set(before_facts) | set(after_facts)
    return tuple(
        sorted(
            name for name in names if before_facts.get(name) != after_facts.get(name)
        )
    )


def diff_snapshots(
    before: ObservedRepositoryState,
    after: ObservedRepositoryState,
) -> tuple[StateChanged, ...]:
    """Return a deterministic semantic diff between immutable snapshots."""
    if before.repository_id != after.repository_id:
        raise ValueError("cannot diff snapshots from different repositories")
    before_index = _snapshot_index(before)
    after_index = _snapshot_index(after)
    scopes = sorted(
        set(before_index) | set(after_index),
        key=lambda item: _scope_key(item[0], item[1]),
    )
    changes = []
    for scope, subject_id in scopes:
        fields = _changed_fields(before_index, after_index, (scope, subject_id))
        if fields:
            changes.append(
                StateChanged(
                    scope=scope,
                    subject_id=subject_id,
                    fields=fields,
                    source="consistency-supervisor.full-scan",
                    occurred_at=after.observed_at,
                )
            )
    return tuple(changes)


def _unknown_facts(
    observed: ObservedRepositoryState,
) -> tuple[ConsistencyUnknownFact, ...]:
    facts = [
        ConsistencyUnknownFact(
            scope=scope.scope,
            subject_id=scope.subject_id,
            name=observation.name,
            source=observation.source,
            certainty=observation.certainty,
            diagnostics=observation.diagnostics,
        )
        for scope in observed.observations
        for observation in scope.facts
        if observation.certainty is not ObservationCertainty.KNOWN
    ]
    return tuple(
        sorted(
            facts,
            key=lambda fact: (*_scope_key(fact.scope, fact.subject_id), fact.name),
        )
    )


def _repair_key(command: RepairCommand) -> tuple[object, ...]:
    return (
        *_scope_key(command.scope, command.subject_id),
        command.code,
        command.idempotency_key,
        repr(command.parameters),
        command.preconditions,
    )


def _deduplicated_commands(
    commands: Iterable[RepairCommand],
) -> tuple[RepairCommand, ...]:
    by_key = {_repair_key(command): command for command in commands}
    return tuple(by_key[key] for key in sorted(by_key))


def _failure_finding(stage: str, description: str) -> ConsistencyFinding:
    return ConsistencyFinding(
        code=f"supervisor.{stage}-failed",
        scope=ConsistencyScope.REPOSITORY,
        severity=FindingSeverity.WARNING,
        expected=Evidence(summary=f"{stage} completes"),
        observed=Evidence(summary=f"{stage} failed", details=(description,)),
        repairability=Repairability.NONE,
    )


def _failure_unknown(stage: str, description: str) -> ConsistencyUnknownFact:
    return ConsistencyUnknownFact(
        scope=ConsistencyScope.REPOSITORY,
        name=f"supervisor.{stage}",
        source="consistency-supervisor",
        certainty=ObservationCertainty.UNKNOWN,
        diagnostics=(description,),
    )


def _failure_scan(
    repository_id: str,
    boundary: str,
    kind: ScanKind,
    stage: str,
    exc: Exception,
    state_changes: tuple[StateChanged, ...] = (),
) -> ConsistencyScanResult:
    description = _describe(exc)
    return ConsistencyScanResult(
        boundary=boundary,
        kind=kind,
        report=ConsistencyReport(
            repository_id=repository_id,
            findings=(_failure_finding(stage, description),),
        ),
        unknown_facts=(_failure_unknown(stage, description),),
        state_changes=state_changes,
        diagnostics=(f"{stage} failed: {description}",),
    )


def _finding_is_impacted(
    finding: ConsistencyFinding, changes: tuple[StateChanged, ...]
) -> bool:
    matching = (change for change in changes if change.scope is finding.scope)
    return any(
        change.scope is ConsistencyScope.REPOSITORY
        or change.subject_id == finding.subject_id
        for change in matching
    )


def _finding_key(finding: ConsistencyFinding) -> tuple[str, str, str]:
    return (finding.scope.value, finding.subject_id or "", finding.code)


def _command_finding_codes(command: RepairCommand) -> tuple[str, ...]:
    parameters = dict(command.parameters)
    single = parameters.get("finding_code")
    multiple = parameters.get("finding_codes")
    if isinstance(single, str):
        return (single,)
    if isinstance(multiple, tuple):
        return tuple(value for value in multiple if isinstance(value, str))
    return ()


def _command_is_allowed(command: RepairCommand, allowlist: frozenset[str]) -> bool:
    finding_codes = _command_finding_codes(command)
    return command.code in allowlist or (
        bool(finding_codes) and set(finding_codes).issubset(allowlist)
    )


def _matching_findings(
    command: RepairCommand, findings: Iterable[ConsistencyFinding]
) -> tuple[ConsistencyFinding, ...]:
    codes = frozenset(_command_finding_codes(command))
    if not codes:
        return ()
    return tuple(
        finding
        for finding in findings
        if finding.scope is command.scope
        and finding.subject_id == command.subject_id
        and finding.code in codes
    )


def _command_is_executable(
    command: RepairCommand,
    scan: ConsistencyScanResult,
    allowlist: frozenset[str],
) -> bool:
    matching = _matching_findings(command, scan.report.findings)
    return (
        _command_is_allowed(command, allowlist)
        and bool(matching)
        and all(
            finding.repairability is Repairability.AUTOMATIC for finding in matching
        )
    )


def _execute_repair(executor: RepairExecutor, command: RepairCommand) -> RepairResult:
    try:
        result = executor.execute(command)
        if result.command != command:
            raise ValueError("repair executor returned a result for another command")
        return result
    except Exception as exc:  # noqa: BLE001 - failure belongs in the cycle report
        return RepairResult(
            command=command,
            status=RepairStatus.FAILED,
            diagnostics=(_describe(exc),),
        )


def _outcome_disposition(
    finding: ConsistencyFinding,
    final_keys: frozenset[tuple[str, str, str]],
    results: tuple[RepairResult, ...],
    observation_unknown: bool,
) -> RepairDisposition:
    if any(result.status is RepairStatus.FAILED for result in results):
        return RepairDisposition.FAILED
    if observation_unknown:
        return RepairDisposition.OBSERVATION_UNKNOWN
    if _finding_key(finding) not in final_keys:
        return RepairDisposition.RESOLVED
    if any(result.status is RepairStatus.APPLIED for result in results):
        return RepairDisposition.UNRESOLVED
    if "observation-unknown" in finding.code or finding.code.startswith("supervisor."):
        return RepairDisposition.OBSERVATION_UNKNOWN
    return RepairDisposition.DEFERRED


def _authoritative_observation_failures(
    final_scan: ConsistencyScanResult,
) -> tuple[ConsistencyUnknownFact, ...]:
    return tuple(
        fact
        for fact in final_scan.unknown_facts
        if fact.source == "consistency-supervisor"
    )


def _outcome_diagnostics(
    results: Iterable[RepairResult],
    observation_failures: Iterable[ConsistencyUnknownFact],
) -> tuple[str, ...]:
    return tuple(
        diagnostic for result in results for diagnostic in result.diagnostics
    ) + tuple(
        diagnostic for fact in observation_failures for diagnostic in fact.diagnostics
    )


def _repair_outcomes(
    findings: Iterable[ConsistencyFinding],
    final_scan: ConsistencyScanResult,
    results_by_finding: dict[tuple[str, str, str], list[RepairResult]],
) -> tuple[ConsistencyRepairOutcome, ...]:
    indexed = {_finding_key(finding): finding for finding in findings}
    final_keys = frozenset(
        _finding_key(finding) for finding in final_scan.report.findings
    )
    observation_failures = _authoritative_observation_failures(final_scan)
    outcomes = [
        ConsistencyRepairOutcome(
            finding_code=finding.code,
            scope=finding.scope,
            subject_id=finding.subject_id,
            disposition=_outcome_disposition(
                finding,
                final_keys,
                tuple(results_by_finding.get(key, ())),
                bool(observation_failures),
            ),
            diagnostics=_outcome_diagnostics(
                results_by_finding.get(key, ()), observation_failures
            ),
        )
        for key, finding in sorted(indexed.items())
    ]
    outcomes.extend(
        ConsistencyRepairOutcome(
            finding_code=f"observation.{fact.name}",
            scope=fact.scope,
            subject_id=fact.subject_id,
            disposition=RepairDisposition.OBSERVATION_UNKNOWN,
            diagnostics=fact.diagnostics,
        )
        for fact in final_scan.unknown_facts
    )
    return tuple(
        sorted(
            outcomes,
            key=lambda item: (
                item.scope.value,
                item.subject_id or "",
                item.finding_code,
            ),
        )
    )


class ConsistencySupervisor:
    """Coordinate scans and use only the executor explicitly supplied per repair."""

    def __init__(
        self,
        *,
        repository_id: str,
        engine: ConsistencyEngine,
        repair_planners: Iterable[RepairPlanner] = (),
    ) -> None:
        if not repository_id:
            raise ValueError("repository_id must not be empty")
        self._repository_id = repository_id
        self._engine = engine
        self._repair_planners = tuple(repair_planners)
        self._previous_full_snapshot: ObservedRepositoryState | None = None
        self._seen_changes: set[tuple[object, ...]] = set()
        self._scans: list[ConsistencyScanResult] = []
        self._repair_passes: list[ConsistencyRepairPass] = []
        self._repair_outcomes: tuple[ConsistencyRepairOutcome, ...] = ()

    def _new_changes(self, changes: Iterable[StateChanged]) -> tuple[StateChanged, ...]:
        normalized: dict[tuple[object, ...], StateChanged] = {}
        for change in changes:
            key = _change_key(change)
            if key not in self._seen_changes:
                normalized[key] = change
        self._seen_changes.update(normalized)
        return tuple(normalized[key] for key in sorted(normalized))

    def _plan_repairs(
        self, report: ConsistencyReport
    ) -> tuple[tuple[RepairCommand, ...], tuple[str, ...]]:
        commands: list[RepairCommand] = []
        diagnostics: list[str] = []
        for planner in self._repair_planners:
            try:
                commands.extend(planner.plan(report))
            except Exception as exc:  # noqa: BLE001 - shadow diagnostics are best effort
                diagnostics.append(f"repair planning failed: {_describe(exc)}")
        return _deduplicated_commands(commands), tuple(diagnostics)

    def _evaluate(
        self,
        boundary: str,
        kind: ScanKind,
        observed: ObservedRepositoryState,
        deriver: DesiredStateDeriver,
        engine: ConsistencyEngine,
        changes: tuple[StateChanged, ...],
    ) -> ConsistencyScanResult:
        try:
            desired = deriver.derive(observed)
            report = engine.evaluate(observed, desired)
        except Exception as exc:  # noqa: BLE001 - report a shadow failure, do not fail cycle
            return _failure_scan(
                self._repository_id, boundary, kind, "evaluation", exc, changes
            )
        if kind is ScanKind.TARGETED:
            report = ConsistencyReport(
                repository_id=report.repository_id,
                findings=tuple(
                    finding
                    for finding in report.findings
                    if _finding_is_impacted(finding, changes)
                ),
                evaluated_invariants=report.evaluated_invariants,
            )
        candidates, diagnostics = self._plan_repairs(report)
        return ConsistencyScanResult(
            boundary=boundary,
            kind=kind,
            report=report,
            unknown_facts=_unknown_facts(observed),
            repair_candidates=candidates,
            state_changes=changes,
            diagnostics=diagnostics,
        )

    def _observe(
        self,
        boundary: str,
        kind: ScanKind,
        observer: Observer,
        changes: tuple[StateChanged, ...],
    ) -> ObservedRepositoryState | ConsistencyScanResult:
        try:
            observed = observer.observe()
            if observed.repository_id != self._repository_id:
                raise ValueError(
                    "repository_id mismatch: "
                    f"expected={self._repository_id!r}, "
                    f"observed={observed.repository_id!r}"
                )
            return observed
        except Exception as exc:  # noqa: BLE001 - shadow observation is non-fatal
            return _failure_scan(
                self._repository_id, boundary, kind, "observation", exc, changes
            )

    def full_scan(
        self,
        boundary: str,
        *,
        observer: Observer,
        deriver: DesiredStateDeriver,
    ) -> ConsistencyScanResult:
        observed = self._observe(boundary, ScanKind.FULL, observer, ())
        if isinstance(observed, ConsistencyScanResult):
            self._scans.append(observed)
            return observed
        raw_changes = (
            diff_snapshots(self._previous_full_snapshot, observed)
            if self._previous_full_snapshot is not None
            else ()
        )
        changes = self._new_changes(raw_changes)
        self._previous_full_snapshot = observed
        scan = self._evaluate(
            boundary, ScanKind.FULL, observed, deriver, self._engine, changes
        )
        self._scans.append(scan)
        return scan

    def targeted_scan(
        self,
        boundary: str,
        changes: Iterable[StateChanged],
        *,
        observer: Observer,
        deriver: DesiredStateDeriver,
    ) -> ConsistencyScanResult | None:
        normalized = self._new_changes(changes)
        if not normalized:
            return None
        scopes = {change.scope for change in normalized}
        engine = ConsistencyEngine(
            invariant
            for invariant in self._engine.invariants
            if invariant.scope in scopes
        )
        observed = self._observe(boundary, ScanKind.TARGETED, observer, normalized)
        if isinstance(observed, ConsistencyScanResult):
            self._scans.append(observed)
            return observed
        scan = self._evaluate(
            boundary, ScanKind.TARGETED, observed, deriver, engine, normalized
        )
        self._scans.append(scan)
        return scan

    def repair_until_stable(
        self,
        initial_scan: ConsistencyScanResult,
        *,
        observer: Observer,
        deriver: DesiredStateDeriver,
        executor: RepairExecutor,
        allowlist: Iterable[str],
        max_passes: int,
    ) -> ConsistencyScanResult:
        """Apply explicitly allowed commands, re-observing after every pass."""
        if not 1 <= max_passes <= MAX_REPAIR_PASSES:
            raise ValueError(f"max_passes must be between 1 and {MAX_REPAIR_PASSES}")
        allowed = frozenset(allowlist)
        current = initial_scan
        findings = list(current.report.findings)
        executed: set[str] = set()
        results_by_finding: dict[tuple[str, str, str], list[RepairResult]] = {}
        for number in range(1, max_passes + 1):
            commands = tuple(
                command
                for command in current.repair_candidates
                if command.idempotency_key not in executed
                and _command_is_executable(command, current, allowed)
            )
            if not commands:
                break
            results = tuple(_execute_repair(executor, command) for command in commands)
            executed.update(command.idempotency_key for command in commands)
            for command, result in zip(commands, results, strict=True):
                for finding in _matching_findings(command, current.report.findings):
                    results_by_finding.setdefault(_finding_key(finding), []).append(
                        result
                    )
            self._repair_passes.append(
                ConsistencyRepairPass(number=number, results=results)
            )
            current = self.full_scan(
                f"repair-{number}", observer=observer, deriver=deriver
            )
            findings.extend(current.report.findings)
            if not any(result.status is RepairStatus.APPLIED for result in results):
                break
        self._repair_outcomes = _repair_outcomes(findings, current, results_by_finding)
        return current

    def cycle_report(self, *, mode: ConsistencyMode) -> ConsistencyCycleReport:
        return ConsistencyCycleReport(
            mode=mode,
            scans=tuple(self._scans),
            repair_passes=tuple(self._repair_passes),
            repair_outcomes=self._repair_outcomes,
        )


def _evidence_to_dict(evidence: Evidence) -> dict:
    return {
        "summary": evidence.summary,
        "details": list(evidence.details),
        "value": evidence.value,
    }


def _finding_to_dict(finding: ConsistencyFinding) -> dict:
    return {
        "code": finding.code,
        "scope": finding.scope.value,
        "subject_id": finding.subject_id,
        "severity": finding.severity.value,
        "expected": _evidence_to_dict(finding.expected),
        "observed": _evidence_to_dict(finding.observed),
        "repairability": finding.repairability.value,
    }


def _unknown_to_dict(fact: ConsistencyUnknownFact) -> dict:
    return {
        "scope": fact.scope.value,
        "subject_id": fact.subject_id,
        "name": fact.name,
        "source": fact.source,
        "certainty": fact.certainty.value,
        "diagnostics": list(fact.diagnostics),
    }


def _command_to_dict(command: RepairCommand) -> dict:
    return {
        "code": command.code,
        "scope": command.scope.value,
        "subject_id": command.subject_id,
        "idempotency_key": command.idempotency_key,
        "parameters": {key: value for key, value in command.parameters},
        "preconditions": list(command.preconditions),
    }


def _change_to_dict(change: StateChanged) -> dict:
    return {
        "scope": change.scope.value,
        "subject_id": change.subject_id,
        "fields": list(change.fields),
        "source": change.source,
        "occurred_at": change.occurred_at.isoformat(),
    }


def _repair_result_to_dict(result: RepairResult) -> dict:
    return {
        "command": _command_to_dict(result.command),
        "status": result.status.value,
        "diagnostics": list(result.diagnostics),
    }


def _repair_outcome_to_dict(outcome: ConsistencyRepairOutcome) -> dict:
    return {
        "finding_code": outcome.finding_code,
        "scope": outcome.scope.value,
        "subject_id": outcome.subject_id,
        "disposition": outcome.disposition.value,
        "diagnostics": list(outcome.diagnostics),
    }


def _scan_to_dict(scan: ConsistencyScanResult) -> dict:
    return {
        "boundary": scan.boundary,
        "kind": scan.kind.value,
        "repository_id": scan.report.repository_id,
        "evaluated_invariants": list(scan.report.evaluated_invariants),
        "findings": [_finding_to_dict(finding) for finding in scan.report.findings],
        "unknown_facts": [_unknown_to_dict(fact) for fact in scan.unknown_facts],
        "repair_candidates": [
            _command_to_dict(command) for command in scan.repair_candidates
        ],
        "state_changes": [_change_to_dict(change) for change in scan.state_changes],
        "diagnostics": list(scan.diagnostics),
    }


def consistency_cycle_to_dict(report: ConsistencyCycleReport) -> dict:
    return {
        "mode": report.mode.value,
        "scans": [_scan_to_dict(scan) for scan in report.scans],
        "repair_passes": [
            {
                "number": repair_pass.number,
                "results": [
                    _repair_result_to_dict(result) for result in repair_pass.results
                ],
            }
            for repair_pass in report.repair_passes
        ],
        "repair_outcomes": [
            _repair_outcome_to_dict(outcome) for outcome in report.repair_outcomes
        ],
    }


__all__ = [
    "ConsistencyCycleReport",
    "ConsistencyMode",
    "ConsistencyRepairOutcome",
    "ConsistencyRepairPass",
    "ConsistencyScanResult",
    "ConsistencySupervisor",
    "ConsistencyUnknownFact",
    "DesiredStateDeriver",
    "MAX_REPAIR_PASSES",
    "RepairDisposition",
    "ScanKind",
    "consistency_cycle_to_dict",
    "diff_snapshots",
]
