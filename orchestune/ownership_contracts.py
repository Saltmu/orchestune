"""Shared ownership and completion contracts with no higher-layer dependencies."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


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


class CompletionLabelStatus(str, Enum):
    """Outcome of a live, completion-specific status-label transition."""

    CONFIRMED = "confirmed"
    ADD_FAILED = "add_failed"
    CLEANUP_INCOMPLETE = "cleanup_incomplete"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class DownstreamPolicyRecord:
    """A generation-scoped downstream policy action awaiting or confirming apply."""

    repository_id: str
    issue_number: int
    generation_id: str
    completion_id: str
    policy_kind: str
    status: str = "pending"
    schema_version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", deepcopy(self.metadata))
        for name, value in (
            ("repository_id", self.repository_id),
            ("generation_id", self.generation_id),
            ("completion_id", self.completion_id),
            ("policy_kind", self.policy_kind),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if (
            not isinstance(self.issue_number, int)
            or isinstance(self.issue_number, bool)
            or self.issue_number <= 0
        ):
            raise ValueError("issue_number must be a positive integer")
        if not isinstance(self.metadata, dict):
            raise ValueError("policy metadata must be an object")
        try:
            json.dumps(self.metadata, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("policy metadata must contain JSON data") from error
        if self.status not in {"pending", "applied"}:
            raise ValueError("status must be 'pending' or 'applied'")
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != 1
        ):
            raise ValueError(
                f"unsupported downstream policy schema_version: {self.schema_version}"
            )

    @property
    def policy_key(self) -> str:
        return f"{self.repository_id}::{self.issue_number}::{self.generation_id}::{self.completion_id}::{self.policy_kind}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repository_id": self.repository_id,
            "issue_number": self.issue_number,
            "generation_id": self.generation_id,
            "completion_id": self.completion_id,
            "policy_kind": self.policy_kind,
            "status": self.status,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DownstreamPolicyRecord:
        return cls(**value)


_VALID_COMPLETE_STAGE_TRANSITIONS: dict[CompleteStage, frozenset[CompleteStage]] = {
    CompleteStage.INITIALIZING: frozenset({CompleteStage.PREFLIGHT_VALIDATING}),
    CompleteStage.PREFLIGHT_VALIDATING: frozenset({CompleteStage.EVIDENCE_VERIFYING}),
    CompleteStage.EVIDENCE_VERIFYING: frozenset({CompleteStage.JOURNALING}),
    CompleteStage.JOURNALING: frozenset(
        {CompleteStage.POSTING, CompleteStage.RESERVED}
    ),
    CompleteStage.POSTING: frozenset({CompleteStage.HANDED_OFF_TO_GC}),
    CompleteStage.HANDED_OFF_TO_GC: frozenset(),
    CompleteStage.RESERVED: frozenset({CompleteStage.OUTCOME_POSTED}),
    CompleteStage.OUTCOME_POSTED: frozenset({CompleteStage.LABEL_CONFIRMED}),
    CompleteStage.LABEL_CONFIRMED: frozenset({CompleteStage.HANDED_OFF}),
    CompleteStage.HANDED_OFF: frozenset(),
}


def can_transition(from_stage: CompleteStage, to_stage: CompleteStage) -> bool:
    """Return whether transitioning directly between completion stages is allowed."""
    return to_stage in _VALID_COMPLETE_STAGE_TRANSITIONS.get(from_stage, frozenset())


def required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def required_digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{name} must be a 64-character lowercase SHA-256 digest")
    return value


def validate_fixed_outcome(
    payload: dict[str, Any], issue_number: int, result: str
) -> None:
    if payload.get("result") != result:
        raise ValueError("outcome_payload result must match the reserved result")
    issue_values = [payload[key] for key in ("issue", "issue_number") if key in payload]
    if not issue_values or any(
        value != issue_number or isinstance(value, bool) for value in issue_values
    ):
        raise ValueError("outcome_payload issue must match the reserved issue")
