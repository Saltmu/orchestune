"""Candidate lifecycle classification for the flat ActiveWorktree record.

Priority is completion, launch, recovery sentinel, interactive claim, then
reservation. The result describes a candidate stage only. In particular,
``HANDOFF_READY`` does not establish that a receipt or remote proof was
verified; the existing completion and GC checks remain authoritative.
"""

from __future__ import annotations

from enum import Enum
from hashlib import sha256
from typing import Protocol


class _ActiveWorktreeState(Protocol):
    issue_number: int
    owner_kind: str
    claim_id: str | None
    claim_stage: str | None
    owner_token_digest: str | None
    pid: int | None
    started_at: float | None
    external_id: str | None
    launch_attempt_id: str | None
    launch_phase: str | None
    completion_id: str | None
    completion_stage: str | None
    completion_handoff_ready: bool


class ActiveWorktreeLifecycle(str, Enum):
    """The highest-priority lifecycle phase suggested by persisted fields."""

    RESERVED = "reserved"
    LAUNCHING = "launching"
    RUNNING = "running"
    RECOVERY_REQUIRED = "recovery_required"
    CLAIMED = "claimed"
    COMPLETING = "completing"
    HANDOFF_READY = "handoff_ready"


_HANDOFF_STAGES = frozenset({"handed_off_to_gc", "handed_off"})
_INTERACTIVE_OWNER = "interactive"
_RESERVED_CLAIM_STAGE = "reserved"
_RECOVERED_CLAIM_PREFIX = "recovered-"


def lifecycle(active: _ActiveWorktreeState) -> ActiveWorktreeLifecycle:
    """Return a candidate phase without verifying completion evidence.

    Precedence preserves overlapping baseline facts: completion takes priority
    over launch, launch over recovery sentinels, and recovery over claim state.
    A claim at its initial ``reserved`` stage remains a reservation.
    """
    if active.completion_handoff_ready or active.completion_stage in _HANDOFF_STAGES:
        return ActiveWorktreeLifecycle.HANDOFF_READY
    if active.completion_id is not None:
        return ActiveWorktreeLifecycle.COMPLETING

    if (
        active.pid is not None
        or active.external_id is not None
        or active.launch_phase == "launched"
    ):
        return ActiveWorktreeLifecycle.RUNNING
    if (
        active.started_at is not None
        or active.launch_attempt_id is not None
        or active.launch_phase is not None
    ):
        return ActiveWorktreeLifecycle.LAUNCHING

    if _has_recovery_sentinel(active):
        return ActiveWorktreeLifecycle.RECOVERY_REQUIRED

    if (
        active.owner_kind == _INTERACTIVE_OWNER
        and active.claim_stage != _RESERVED_CLAIM_STAGE
    ):
        return ActiveWorktreeLifecycle.CLAIMED

    return ActiveWorktreeLifecycle.RESERVED


def _has_recovery_sentinel(active: _ActiveWorktreeState) -> bool:
    claim_id = active.claim_id
    if claim_id is not None and claim_id.startswith(_RECOVERED_CLAIM_PREFIX):
        return True

    # Keep this digest derivation aligned with run_state.recovered_owner_token_digest.
    identity = claim_id or f"{_RECOVERED_CLAIM_PREFIX}{active.issue_number}"
    expected = sha256(f"recovered-unverifiable:{identity}".encode()).hexdigest()
    return active.owner_token_digest == expected
