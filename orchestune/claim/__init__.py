"""Claim package providing domain contracts and orchestration for task claiming."""

from __future__ import annotations

from orchestune.claim.contracts import (
    ClaimExitCode,
    ClaimFailure,
    ClaimFailureReason,
    ClaimOutcome,
    ClaimRequest,
    ClaimStage,
    OwnerKind,
    ReservationKind,
    failure_reason_to_exit_code,
)

__all__ = [
    "ClaimExitCode",
    "ClaimFailure",
    "ClaimFailureReason",
    "ClaimOutcome",
    "ClaimRequest",
    "ClaimStage",
    "OwnerKind",
    "ReservationKind",
    "failure_reason_to_exit_code",
]
