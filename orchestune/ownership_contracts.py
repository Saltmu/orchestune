"""Dependency-free ownership contracts shared by claim, dispatch, complete, and ledger."""

from enum import Enum


class OwnerKind(str, Enum):
    """Identifies the entity responsible for an active claim session."""

    INTERACTIVE = "interactive"
    DISPATCH = "dispatch"


class ReservationKind(str, Enum):
    """Scope of task reservation applied during claim."""

    FOOTPRINT = "footprint"
    REPOSITORY = "repository"


class ClaimStage(str, Enum):
    """Progression stages of claim side-effects."""

    VALIDATING = "validating"
    FETCHED = "fetched"
    RESERVED = "reserved"
    WORKTREE_PREPARED = "worktree_prepared"
    ACTIVE_SAVED = "active_saved"
    LABELED = "labeled"
    COMPLETED = "completed"


class CompleteStage(str, Enum):
    """Progression stages of complete side-effects.

    The complete CLI advances through these stages up to GC handoff.
    Physical worktree removal and completion receipts are reserved
    exclusively for the GC subsystem. The persisted values are the single
    vocabulary shared by the complete journal and the ledger validators;
    transition rules stay in ``orchestune.complete.contracts``.
    """

    INITIALIZING = "initializing"
    PREFLIGHT_VALIDATING = "preflight_validating"
    EVIDENCE_VERIFYING = "evidence_verifying"
    JOURNALING = "journaling"
    POSTING = "posting"
    HANDED_OFF_TO_GC = "handed_off_to_gc"
    # Label-confirmed lifecycle stages. The legacy POSTING -> HANDED_OFF_TO_GC
    # path stays available until the new writer and readers switch together.
    RESERVED = "reserved"
    OUTCOME_POSTED = "outcome_posted"
    LABEL_CONFIRMED = "label_confirmed"
    HANDED_OFF = "handed_off"
