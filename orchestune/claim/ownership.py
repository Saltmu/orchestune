"""Pure reservation construction and conflict evaluation for claim ownership."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from secrets import token_urlsafe
from typing import Protocol
from uuid import uuid4

from orchestune.claim.contracts import ClaimRequest, ClaimStage, ReservationKind
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.task_metadata import TaskMetadata


@dataclass(frozen=True, slots=True, repr=False)
class OwnerToken:
    """An owner credential whose display form never reveals its raw value."""

    value: str

    def __repr__(self) -> str:
        return "OwnerToken(***redacted***)"

    def __str__(self) -> str:
        return "***redacted***"


class ClaimConflictReason(str, Enum):
    """Machine-readable reasons that prevent a new reservation."""

    SAME_ISSUE = "same_issue"
    REPOSITORY_RESERVATION = "repository_reservation"
    FOOTPRINT_OVERLAP = "footprint_overlap"
    FORCED_SERIAL = "forced_serial"
    SHARED_CONTRACT = "shared_contract"


@dataclass(frozen=True, slots=True)
class ClaimConflict:
    """A conflict between a prospective reservation and an active reservation."""

    reason: ClaimConflictReason
    active: ActiveWorktree


class ClaimConflictView(Protocol):
    """The read-only task lookup needed for shared-contract evaluation."""

    def task(self, issue_number: int) -> TaskMetadata | None: ...


def new_claim_id() -> str:
    """Create an opaque, collision-resistant claim identifier."""
    return f"claim-{uuid4().hex}"


def new_owner_token() -> OwnerToken:
    """Create a high-entropy owner token with a redacted representation."""
    return OwnerToken(token_urlsafe(32))


def owner_token_digest(token: OwnerToken | str) -> str:
    """Return a one-way digest suitable for durable owner-token comparison."""
    value = token.value if isinstance(token, OwnerToken) else token
    return sha256(value.encode("utf-8")).hexdigest()


def build_reservation(
    request: ClaimRequest, task_metadata: TaskMetadata
) -> ActiveWorktree:
    """Build a pre-worktree reservation without persisting it or performing I/O."""
    footprint = tuple(task_metadata.footprint)
    token = (
        OwnerToken(request.owner_token) if request.owner_token else new_owner_token()
    )
    reservation_kind = (
        ReservationKind.FOOTPRINT if footprint else ReservationKind.REPOSITORY
    )
    return ActiveWorktree(
        issue_number=request.issue_number,
        branch="",
        worktree_path="",
        pid=None,
        started_at=None,
        declared_footprint=footprint,
        owner_kind=request.owner_kind.value,
        claim_id=new_claim_id(),
        claim_stage=ClaimStage.RESERVED.value,
        reservation_kind=reservation_kind.value,
        owner_token_digest=owner_token_digest(token),
    )


def _is_repository_reservation(reservation: ActiveWorktree) -> bool:
    return reservation.reservation_kind == ReservationKind.REPOSITORY.value


def _shared_contract_conflicts(
    reservation_task: TaskMetadata | None, active_task: TaskMetadata | None
) -> bool:
    if reservation_task is None or active_task is None:
        return False
    return (
        reservation_task.writes_shared_contract
        and active_task.writes_shared_contract
        and reservation_task.shared_contract is not None
        and reservation_task.shared_contract == active_task.shared_contract
    )


def evaluate_claim_conflicts(
    reservation: ActiveWorktree, run_state: RunState, view: ClaimConflictView
) -> ClaimConflict | None:
    """Return the first active reservation that excludes ``reservation``."""
    reservation_task = view.task(reservation.issue_number)
    for active in run_state.active_worktrees.values():
        if active.issue_number == reservation.issue_number:
            return ClaimConflict(ClaimConflictReason.SAME_ISSUE, active)
        if _is_repository_reservation(reservation) or _is_repository_reservation(
            active
        ):
            return ClaimConflict(ClaimConflictReason.REPOSITORY_RESERVATION, active)
        if set(reservation.declared_footprint) & set(active.declared_footprint):
            return ClaimConflict(ClaimConflictReason.FOOTPRINT_OVERLAP, active)
        if active.forced_serial:
            return ClaimConflict(ClaimConflictReason.FORCED_SERIAL, active)
        if _shared_contract_conflicts(reservation_task, view.task(active.issue_number)):
            return ClaimConflict(ClaimConflictReason.SHARED_CONTRACT, active)
    return None
