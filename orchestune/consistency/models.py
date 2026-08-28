"""Immutable values shared by the repository consistency kernel.

This module intentionally imports no Orchestune adapters.  Forge, Git, process,
and filesystem access belongs behind the Protocols in ``contracts``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

type FactValue = str | int | float | bool | None | tuple[FactValue, ...]


class ObservationCertainty(StrEnum):
    """How confidently an observed fact describes current external state."""

    KNOWN = "known"
    UNKNOWN = "unknown"
    STALE = "stale"


class ConsistencyScope(StrEnum):
    """Repository hierarchy at which an invariant is evaluated."""

    REPOSITORY = "repository"
    PARENT = "parent"
    TASK = "task"


class FindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Repairability(StrEnum):
    """Whether and how a finding may be repaired."""

    AUTOMATIC = "automatic"
    MANUAL = "manual"
    NONE = "none"


class IntentStatus(StrEnum):
    """Lifecycle of an explicitly recorded non-atomic transition."""

    PLANNED = "planned"
    APPLIED = "applied"
    VERIFIED = "verified"
    FAILED = "failed"
    EXPIRED = "expired"


class RepairStatus(StrEnum):
    APPLIED = "applied"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Evidence:
    """Human-readable, stable evidence for an expected or observed condition."""

    summary: str
    details: tuple[str, ...] = ()
    value: FactValue = None


@dataclass(frozen=True, slots=True)
class Observation:
    """One normalized fact, including provenance and uncertainty."""

    name: str
    certainty: ObservationCertainty
    source: str
    observed_at: datetime
    value: FactValue = None
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScopedObservations:
    """Facts associated with a repository, parent Issue, or task Issue."""

    scope: ConsistencyScope
    subject_id: str | None = None
    facts: tuple[Observation, ...] = ()


@dataclass(frozen=True, slots=True)
class ObservedRepositoryState:
    """A side-effect-free snapshot of normalized repository observations."""

    repository_id: str
    observed_at: datetime
    observations: tuple[ScopedObservations, ...] = ()


@dataclass(frozen=True, slots=True)
class DesiredFact:
    """One desired condition and the reason it is expected."""

    name: str
    value: FactValue
    scope: ConsistencyScope
    subject_id: str | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class TransitionIntent:
    """An explicit, diagnosable in-flight non-atomic state transition."""

    intent_id: str
    scope: ConsistencyScope
    operation: str
    created_at: datetime
    subject_id: str | None = None
    status: IntentStatus = IntentStatus.PLANNED
    expires_at: datetime | None = None
    expected_changes: tuple[DesiredFact, ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DesiredRepositoryState:
    """Purely derived repository expectations and valid transition intents."""

    repository_id: str
    facts: tuple[DesiredFact, ...] = ()
    transition_intents: tuple[TransitionIntent, ...] = ()


@dataclass(frozen=True, slots=True)
class ConsistencyFinding:
    """A stable, evidence-bearing divergence from desired state."""

    code: str
    scope: ConsistencyScope
    severity: FindingSeverity
    expected: Evidence
    observed: Evidence
    repairability: Repairability
    subject_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConsistencyReport:
    """Deterministically ordered output from a complete invariant evaluation."""

    repository_id: str
    findings: tuple[ConsistencyFinding, ...] = ()
    evaluated_invariants: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StateChanged:
    """A hint for later targeted scans; full scans remain authoritative."""

    scope: ConsistencyScope
    fields: tuple[str, ...]
    source: str
    occurred_at: datetime
    subject_id: str | None = None


@dataclass(frozen=True, slots=True)
class RepairCommand:
    """A typed repair request; constructing it performs no mutation."""

    code: str
    scope: ConsistencyScope
    idempotency_key: str
    subject_id: str | None = None
    parameters: tuple[tuple[str, FactValue], ...] = ()
    preconditions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RepairResult:
    """Immutable result returned by a side-effecting RepairExecutor."""

    command: RepairCommand
    status: RepairStatus
    diagnostics: tuple[str, ...] = ()
