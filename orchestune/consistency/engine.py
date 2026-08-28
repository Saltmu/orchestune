"""Deterministic, exhaustive consistency invariant evaluation."""

from __future__ import annotations

from collections.abc import Iterable

from orchestune.consistency.contracts import Invariant
from orchestune.consistency.models import (
    ConsistencyFinding,
    ConsistencyReport,
    ConsistencyScope,
    DesiredRepositoryState,
    ObservedRepositoryState,
)

_SCOPE_ORDER = {
    ConsistencyScope.REPOSITORY: 0,
    ConsistencyScope.PARENT: 1,
    ConsistencyScope.TASK: 2,
}


def _invariant_key(invariant: Invariant) -> tuple[int, str]:
    return (_SCOPE_ORDER[invariant.scope], invariant.code)


def _finding_key(finding: ConsistencyFinding) -> tuple[object, ...]:
    return (
        _SCOPE_ORDER[finding.scope],
        finding.subject_id or "",
        finding.code,
        finding.severity.value,
        finding.expected.summary,
        finding.expected.details,
        finding.observed.summary,
        finding.observed.details,
        finding.repairability.value,
    )


class ConsistencyEngine:
    """Evaluates all registered invariants and returns one stable report."""

    def __init__(self, invariants: Iterable[Invariant]) -> None:
        self._invariants = tuple(sorted(invariants, key=_invariant_key))

    @property
    def invariants(self) -> tuple[Invariant, ...]:
        return self._invariants

    def evaluate(
        self,
        observed: ObservedRepositoryState,
        desired: DesiredRepositoryState,
    ) -> ConsistencyReport:
        if observed.repository_id != desired.repository_id:
            raise ValueError(
                "repository_id mismatch: "
                f"observed={observed.repository_id!r}, "
                f"desired={desired.repository_id!r}"
            )
        findings: list[ConsistencyFinding] = []
        evaluated: list[str] = []
        for invariant in self._invariants:
            evaluated.append(invariant.code)
            findings.extend(invariant.evaluate(observed, desired))
        return ConsistencyReport(
            repository_id=observed.repository_id,
            findings=tuple(sorted(findings, key=_finding_key)),
            evaluated_invariants=tuple(evaluated),
        )
