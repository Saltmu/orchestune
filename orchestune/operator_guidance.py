"""Read-only guidance shared by status, workflow next, and local trial.

The types depend only on the standard library. They do not decide state,
start a process, or record completion.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EvidenceState(StrEnum):
    """How fresh the observation behind a guidance line is."""

    CURRENT = "current"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class NextOwner(StrEnum):
    """Who can move the work from the observation that was just read."""

    ENGINE = "engine"
    USER = "user"
    AGENT = "agent"
    UNKNOWN = "unknown"
    NONE = "none"


class ProgressOutlook(StrEnum):
    """Whether the work proceeds on its own, and under what limit."""

    IN_PROGRESS = "in_progress"
    CONDITIONAL = "conditional"
    MANUAL = "manual"
    UNKNOWN = "unknown"
    TERMINAL = "terminal"


@dataclass(frozen=True, kw_only=True)
class GuidanceAction:
    """One displayed candidate. ``argv`` is executable only when complete."""

    description: str
    argv: tuple[str, ...] | None = None
    preconditions: tuple[str, ...] = ()
    references: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class OperatorGuidance:
    """One human reading of an existing observation. Not a new decision."""

    target: str
    stage: str
    reason_codes: tuple[str, ...]
    explanation: str
    next_owner: NextOwner
    progress: ProgressOutlook
    progress_condition: str | None
    actions: tuple[GuidanceAction, ...]
    references: tuple[str, ...]
    evidence_state: EvidenceState
    evidence_note: str
