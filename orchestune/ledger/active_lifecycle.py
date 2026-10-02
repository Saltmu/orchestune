"""Candidate lifecycle classification for the nested ActiveWorktree record.

Priority is completion, launch, recovery sentinel, interactive claim, then
reservation. The result describes a candidate stage only. In particular,
``HANDOFF_READY`` does not establish that a receipt or remote proof was
verified; the existing completion and GC checks remain authoritative.
"""

from __future__ import annotations

from enum import Enum
from hashlib import sha256

from orchestune.ledger.active_records import ActiveWorktree
from orchestune.ownership_contracts import ClaimStage, OwnerKind


class ActiveWorktreeLifecycle(str, Enum):
    """The highest-priority lifecycle phase suggested by persisted fields."""

    RESERVED = "reserved"
    LAUNCHING = "launching"
    RUNNING = "running"
    RECOVERY_REQUIRED = "recovery_required"
    CLAIMED = "claimed"
    COMPLETING = "completing"
    HANDOFF_READY = "handoff_ready"


# Keep persisted completion-stage values local: importing complete.contracts
# here would add a complete <-> ledger package cycle.
_HANDOFF_STAGES = frozenset({"handed_off_to_gc", "handed_off"})
_LAUNCHED_PHASE = "launched"
_RESERVED_CLAIM_STAGE = ClaimStage.RESERVED.value
_RECOVERED_CLAIM_PREFIX = "recovered-"


def lifecycle(active: ActiveWorktree) -> ActiveWorktreeLifecycle:
    """Return a candidate phase without verifying completion evidence.

    Precedence preserves overlapping baseline facts: completion takes priority
    over launch, launch over recovery sentinels, and recovery over claim state.
    A claim at its initial ``reserved`` stage remains a reservation.
    """
    completion = active.completion
    launch = active.launch
    claim = active.claim
    if (
        completion.completion_handoff_ready
        or completion.completion_stage in _HANDOFF_STAGES
    ):
        return ActiveWorktreeLifecycle.HANDOFF_READY
    if completion.completion_id is not None:
        return ActiveWorktreeLifecycle.COMPLETING

    if (
        launch.pid is not None
        or launch.external_id is not None
        or launch.launch_phase == _LAUNCHED_PHASE
    ):
        return ActiveWorktreeLifecycle.RUNNING
    if (
        launch.started_at is not None
        or launch.launch_attempt_id is not None
        or launch.launch_phase is not None
    ):
        return ActiveWorktreeLifecycle.LAUNCHING

    if _has_recovery_sentinel(active):
        return ActiveWorktreeLifecycle.RECOVERY_REQUIRED

    if (
        claim.owner_kind == OwnerKind.INTERACTIVE.value
        and claim.claim_stage is not None
        and claim.claim_stage != _RESERVED_CLAIM_STAGE
    ):
        return ActiveWorktreeLifecycle.CLAIMED

    return ActiveWorktreeLifecycle.RESERVED


def _has_recovery_sentinel(active: ActiveWorktree) -> bool:
    claim_id = active.claim.claim_id
    if claim_id is not None and claim_id.startswith(_RECOVERED_CLAIM_PREFIX):
        return True

    # Keep this digest derivation aligned with run_state.recovered_owner_token_digest.
    identity = claim_id or f"{_RECOVERED_CLAIM_PREFIX}{active.core.issue_number}"
    expected = sha256(f"recovered-unverifiable:{identity}".encode()).hexdigest()
    return active.claim.owner_token_digest == expected
