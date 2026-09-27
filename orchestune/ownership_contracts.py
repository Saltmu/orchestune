"""Dependency-free ownership contracts shared by claim, dispatch, and complete."""

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
