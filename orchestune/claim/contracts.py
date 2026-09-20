"""Contracts and domain models for the task claiming lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from pathlib import Path


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


class ClaimExitCode(IntEnum):
    """Exit codes for claim operations, shared with future orchestration steps."""

    SUCCESS = 0
    GENERIC_ERROR = 1

    # Preflight / Validation errors (10-19)
    ISSUE_NOT_FOUND = 10
    ISSUE_CLOSED = 11
    ALREADY_IN_PROGRESS = 12
    TERMINAL_ESCALATION = 13
    UNRESOLVED_DEPENDENCIES = 14
    EXTERNAL_LOCK_CONFLICT = 15
    INVALID_RESUME = 16

    # Concurrency / State errors (20-29)
    CLAIM_CONFLICT = 20
    EXISTING_CLAIM_UNRECOVERED = 21
    STATE_LOCK_FAILED = 22

    # Infrastructure / Environment errors (30-39)
    GIT_FETCH_FAILED = 30
    BASE_RESOLUTION_FAILED = 31
    WORKTREE_CREATION_FAILED = 32
    STATE_SAVE_FAILED = 33
    LABEL_UPDATE_FAILED = 34


class ClaimFailureReason(str, Enum):
    """Machine-readable failure reasons for rejected or faulted claims."""

    ISSUE_NOT_FOUND = "issue_not_found"
    ISSUE_CLOSED = "issue_closed"
    ALREADY_IN_PROGRESS = "already_in_progress"
    TERMINAL_ESCALATION = "terminal_escalation"
    UNRESOLVED_DEPENDENCIES = "unresolved_dependencies"
    EXTERNAL_LOCK_CONFLICT = "external_lock_conflict"
    INVALID_RESUME = "invalid_resume"
    CLAIM_CONFLICT = "claim_conflict"
    EXISTING_CLAIM_UNRECOVERED = "existing_claim_unrecovered"
    STATE_LOCK_FAILED = "state_lock_failed"
    GIT_FETCH_FAILED = "git_fetch_failed"
    BASE_RESOLUTION_FAILED = "base_resolution_failed"
    WORKTREE_CREATION_FAILED = "worktree_creation_failed"
    STATE_SAVE_FAILED = "state_save_failed"
    LABEL_UPDATE_FAILED = "label_update_failed"


_FAILURE_REASON_TO_EXIT_CODE: dict[ClaimFailureReason, ClaimExitCode] = {
    ClaimFailureReason.ISSUE_NOT_FOUND: ClaimExitCode.ISSUE_NOT_FOUND,
    ClaimFailureReason.ISSUE_CLOSED: ClaimExitCode.ISSUE_CLOSED,
    ClaimFailureReason.ALREADY_IN_PROGRESS: ClaimExitCode.ALREADY_IN_PROGRESS,
    ClaimFailureReason.TERMINAL_ESCALATION: ClaimExitCode.TERMINAL_ESCALATION,
    ClaimFailureReason.UNRESOLVED_DEPENDENCIES: ClaimExitCode.UNRESOLVED_DEPENDENCIES,
    ClaimFailureReason.EXTERNAL_LOCK_CONFLICT: ClaimExitCode.EXTERNAL_LOCK_CONFLICT,
    ClaimFailureReason.INVALID_RESUME: ClaimExitCode.INVALID_RESUME,
    ClaimFailureReason.CLAIM_CONFLICT: ClaimExitCode.CLAIM_CONFLICT,
    ClaimFailureReason.EXISTING_CLAIM_UNRECOVERED: ClaimExitCode.EXISTING_CLAIM_UNRECOVERED,
    ClaimFailureReason.STATE_LOCK_FAILED: ClaimExitCode.STATE_LOCK_FAILED,
    ClaimFailureReason.GIT_FETCH_FAILED: ClaimExitCode.GIT_FETCH_FAILED,
    ClaimFailureReason.BASE_RESOLUTION_FAILED: ClaimExitCode.BASE_RESOLUTION_FAILED,
    ClaimFailureReason.WORKTREE_CREATION_FAILED: ClaimExitCode.WORKTREE_CREATION_FAILED,
    ClaimFailureReason.STATE_SAVE_FAILED: ClaimExitCode.STATE_SAVE_FAILED,
    ClaimFailureReason.LABEL_UPDATE_FAILED: ClaimExitCode.LABEL_UPDATE_FAILED,
}


def failure_reason_to_exit_code(reason: ClaimFailureReason) -> ClaimExitCode:
    """Map a ClaimFailureReason to its corresponding ClaimExitCode."""
    if (
        not isinstance(reason, ClaimFailureReason)
        or reason not in _FAILURE_REASON_TO_EXIT_CODE
    ):
        raise KeyError(f"Unmapped claim failure reason: {reason!r}")
    return _FAILURE_REASON_TO_EXIT_CODE[reason]


@dataclass(frozen=True)
class ClaimFailure:
    """Structured diagnostics for a failed claim attempt."""

    reason: ClaimFailureReason
    message: str
    conflicting_issue_number: int | None = None
    conflicting_branch: str | None = None
    conflicting_path: Path | None = None
    next_actions: tuple[str, ...] = ()

    @property
    def exit_code(self) -> ClaimExitCode:
        """Derive exit code directly from the machine-readable reason."""
        return failure_reason_to_exit_code(self.reason)


@dataclass(frozen=True)
class ClaimRequest:
    """Parameters required to claim an issue."""

    issue_number: int
    owner_kind: OwnerKind = OwnerKind.INTERACTIVE
    owner_id: str | None = None
    resume_claim_id: str | None = None
    owner_token: str | None = None
    dry_run: bool = False
    timeout_seconds: float | None = None
    state_path: Path | None = None


@dataclass(frozen=True)
class ClaimOutcome:
    """Outcome of a claim attempt."""

    success: bool
    issue_number: int
    claim_id: str | None = None
    branch: str | None = None
    worktree_path: Path | None = None
    base_ref: str | None = None
    owner_kind: OwnerKind | None = None
    reservation_kind: ReservationKind | None = None
    stage: ClaimStage | None = None
    failure: ClaimFailure | None = None
    owner_token: str | None = None
