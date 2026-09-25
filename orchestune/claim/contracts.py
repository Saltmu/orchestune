"""Contracts and domain models for the task claiming lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from orchestune.exit_codes import TaskExitCode, claim_failure_exit_code


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


ClaimExitCode = TaskExitCode


class ClaimFailureReason(str, Enum):
    """Machine-readable failure reasons for rejected or faulted claims."""

    ISSUE_NOT_FOUND = "issue_not_found"
    ISSUE_CLOSED = "issue_closed"
    ALREADY_IN_PROGRESS = "already_in_progress"
    TERMINAL_ESCALATION = "terminal_escalation"
    UNRESOLVED_DEPENDENCIES = "unresolved_dependencies"
    EXTERNAL_LOCK_CONFLICT = "external_lock_conflict"
    INVALID_RESUME = "invalid_resume"
    INVALID_BRANCH_NAME = "invalid_branch_name"
    CLAIM_CONFLICT = "claim_conflict"
    EXISTING_CLAIM_UNRECOVERED = "existing_claim_unrecovered"
    STATE_LOCK_FAILED = "state_lock_failed"
    GIT_FETCH_FAILED = "git_fetch_failed"
    BASE_RESOLUTION_FAILED = "base_resolution_failed"
    WORKTREE_CREATION_FAILED = "worktree_creation_failed"
    STATE_SAVE_FAILED = "state_save_failed"
    LABEL_UPDATE_FAILED = "label_update_failed"


def failure_reason_to_exit_code(reason: ClaimFailureReason) -> ClaimExitCode:
    """Map a ClaimFailureReason to its corresponding ClaimExitCode."""
    if not isinstance(reason, ClaimFailureReason):
        raise KeyError(f"Unmapped claim failure reason: {reason!r}")
    return claim_failure_exit_code(reason.value)


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
    owner_token: str | None = field(default=None, repr=False)
    dry_run: bool = False
    timeout_seconds: float | None = None
    state_path: Path | None = None
    # #943レビュー対応(Codex P1, round4): dispatchは`worktree_root`を既定値
    # （`<repo>/worktrees`）以外へ設定できる。未指定の場合、claimは既定値へ
    # 固定してしまい、実際にagentが起動されるディレクトリとdispatch自身が
    # 参照する`config.worktree_root`が食い違う。
    worktree_root: Path | None = None


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
    owner_token: str | None = field(default=None, repr=False)
