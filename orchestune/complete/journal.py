"""Durable completion journaling (#1001).

Persists a completion attempt's ``completion_id``, result, posting evidence
(comment id/url, payload) and handoff-readiness onto the same ``ActiveWorktree``
ledger entry that ``orchestune claim`` already owns, under the same
``run_state.json`` file lock. Each public function acquires that lock only for
the duration of its own read-modify-write — never across CI runs or network
calls — so ownership survives long-running work without holding the lock.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, overload
from uuid import uuid4

from orchestune.claim.ownership import owner_token_digest
from orchestune.complete.contracts import (
    CompleteFailureReason,
    CompleteStage,
    CompletionLabelStatus,
    CompletionLabelTransitionResult,
    can_transition,
)
from orchestune.infra.process_utils import (
    FileLockContentionError,
    assert_run_state_lock_held,
    run_state_lock,
)
from orchestune.ledger.run_state import load_run_state, save_run_state
from orchestune.outcome_record import VALID_RESULTS


@dataclass(frozen=True)
class CompletionJournal:
    """Durable identity and evidence for one completion attempt."""

    issue_number: int
    claim_id: str
    completion_id: str
    result: str
    stage: str
    payload: dict[str, Any] | None = None
    comment_id: str | None = None
    comment_url: str | None = None
    handoff_ready: bool = False


_COMPLETION_SCHEMA_VERSION = 1


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _required_digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{name} must be a 64-character lowercase SHA-256 digest")
    return value


def _validate_fixed_outcome(
    payload: dict[str, Any], issue_number: int, result: str
) -> None:
    if payload.get("result", result) != result:
        raise ValueError("outcome_payload result must match the reserved result")
    payload_issue = payload.get("issue", payload.get("issue_number", issue_number))
    if payload_issue != issue_number or isinstance(payload_issue, bool):
        raise ValueError("outcome_payload issue must match the reserved issue")


@dataclass(frozen=True)
class DownstreamPolicyRecord:
    """A generation-scoped downstream policy action awaiting or confirming apply."""

    repository_id: str
    issue_number: int
    generation_id: str
    completion_id: str
    policy_kind: str
    status: str = "pending"
    schema_version: int = _COMPLETION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _required_text(self.repository_id, "repository_id")
        _required_text(self.generation_id, "generation_id")
        _required_text(self.completion_id, "completion_id")
        _required_text(self.policy_kind, "policy_kind")
        if (
            not isinstance(self.issue_number, int)
            or isinstance(self.issue_number, bool)
            or self.issue_number <= 0
        ):
            raise ValueError("issue_number must be a positive integer")
        if self.status not in {"pending", "applied"}:
            raise ValueError("status must be 'pending' or 'applied'")
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != _COMPLETION_SCHEMA_VERSION
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
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DownstreamPolicyRecord:
        if value.get("schema_version") != _COMPLETION_SCHEMA_VERSION:
            raise ValueError("downstream policy schema_version is unsupported")
        return cls(**value)


@dataclass(frozen=True)
class CompletionJournalRecord:
    """Versioned completion identity and separately recorded publication proof."""

    repository_id: str
    issue_number: int
    generation_id: str
    completion_id: str
    owner_token_digest: str
    request_fingerprint: str
    result: str
    target_label: str
    outcome_payload: dict[str, Any]
    stage: CompleteStage
    posting_evidence: dict[str, Any] | None = None
    label_evidence: dict[str, Any] | None = None
    prepublication_policy_evidence: dict[str, Any] | None = None
    downstream_policy_records: tuple[DownstreamPolicyRecord, ...] = ()
    schema_version: int = _COMPLETION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self._validate_identity()
        self._validate_payload()
        self._validate_evidence()
        self._validate_policies()

    def _validate_identity(self) -> None:
        _required_text(self.repository_id, "repository_id")
        _required_text(self.generation_id, "generation_id")
        _required_text(self.completion_id, "completion_id")
        _required_text(self.target_label, "target_label")
        _required_digest(self.owner_token_digest, "owner_token_digest")
        _required_digest(self.request_fingerprint, "request_fingerprint")
        if (
            not isinstance(self.issue_number, int)
            or isinstance(self.issue_number, bool)
            or self.issue_number <= 0
        ):
            raise ValueError("issue_number must be a positive integer")
        if self.result not in VALID_RESULTS:
            raise ValueError(f"invalid completion result: {self.result!r}")

    def _validate_payload(self) -> None:
        if not isinstance(self.outcome_payload, dict):
            raise ValueError("outcome_payload must be an object")
        _validate_fixed_outcome(self.outcome_payload, self.issue_number, self.result)
        if not isinstance(self.stage, CompleteStage):
            try:
                object.__setattr__(self, "stage", CompleteStage(self.stage))
            except ValueError as error:
                raise ValueError(f"unknown completion stage: {self.stage!r}") from error
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != _COMPLETION_SCHEMA_VERSION
        ):
            raise ValueError(
                f"unsupported completion journal schema_version: {self.schema_version}"
            )

    def _validate_evidence(self) -> None:
        if self.posting_evidence is not None and not isinstance(
            self.posting_evidence, dict
        ):
            raise ValueError("posting_evidence must be an object or None")
        if self.label_evidence is not None and not isinstance(
            self.label_evidence, dict
        ):
            raise ValueError("label_evidence must be an object or None")
        if self.prepublication_policy_evidence is not None and not isinstance(
            self.prepublication_policy_evidence, dict
        ):
            raise ValueError("prepublication_policy_evidence must be an object or None")
        self._validate_stage_evidence()

    def _validate_policies(self) -> None:
        for policy in self.downstream_policy_records:
            if not isinstance(policy, DownstreamPolicyRecord):
                raise ValueError(
                    "downstream_policy_records must contain policy records"
                )
            if (
                policy.repository_id,
                policy.issue_number,
                policy.generation_id,
                policy.completion_id,
            ) != (
                self.repository_id,
                self.issue_number,
                self.generation_id,
                self.completion_id,
            ):
                raise ValueError("downstream policy identity must match its completion")

    def _validate_stage_evidence(self) -> None:
        if self.stage in {
            CompleteStage.OUTCOME_POSTED,
            CompleteStage.LABEL_CONFIRMED,
            CompleteStage.HANDED_OFF,
        }:
            evidence = self.posting_evidence
            if not evidence or not _has_posting_evidence(
                evidence.get("comment_id"), evidence.get("comment_url")
            ):
                raise ValueError(
                    "outcome-posted stage requires durable comment id and url"
                )
        if self.stage in {CompleteStage.LABEL_CONFIRMED, CompleteStage.HANDED_OFF}:
            evidence = self.label_evidence
            if (
                not evidence
                or evidence.get("status") != CompletionLabelStatus.CONFIRMED.value
            ):
                raise ValueError(
                    "label-confirmed stage requires confirmed label evidence"
                )
            observed = evidence.get("observed_labels")
            if (
                not isinstance(observed, list | tuple)
                or self.target_label not in observed
            ):
                raise ValueError(
                    "confirmed label evidence must include the target label"
                )

    @property
    def journal_key(self) -> str:
        return f"{self.repository_id}::{self.issue_number}::{self.generation_id}::{self.completion_id}"

    @property
    def reservation_key(self) -> str:
        return f"{self.repository_id}::{self.issue_number}"

    @property
    def receipt_key(self) -> str:
        return self.journal_key

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repository_id": self.repository_id,
            "issue_number": self.issue_number,
            "generation_id": self.generation_id,
            "completion_id": self.completion_id,
            "owner_token_digest": self.owner_token_digest,
            "request_fingerprint": self.request_fingerprint,
            "result": self.result,
            "target_label": self.target_label,
            "outcome_payload": self.outcome_payload,
            "stage": self.stage.value,
            "posting_evidence": self.posting_evidence,
            "label_evidence": self.label_evidence,
            "prepublication_policy_evidence": self.prepublication_policy_evidence,
            "downstream_policy_records": {
                record.policy_key: record.to_dict()
                for record in self.downstream_policy_records
            },
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CompletionJournalRecord:
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != _COMPLETION_SCHEMA_VERSION
        ):
            raise ValueError("completion_journal schema_version is unsupported")
        parsed = dict(value)
        parsed["stage"] = CompleteStage(parsed["stage"])
        raw_policies = parsed.get("downstream_policy_records", {})
        if not isinstance(raw_policies, dict):
            raise ValueError("downstream_policy_records must be a keyed object")
        policies = tuple(
            DownstreamPolicyRecord.from_dict(record) for record in raw_policies.values()
        )
        if any(
            policy.policy_key != key
            for key, policy in zip(raw_policies, policies, strict=True)
        ):
            raise ValueError(
                "downstream_policy_records key does not match its identity"
            )
        parsed["downstream_policy_records"] = policies
        return cls(**parsed)

    def advance(self, stage: CompleteStage, **evidence: Any) -> CompletionJournalRecord:
        if not can_transition(self.stage, stage):
            raise ValueError(
                f"invalid completion stage transition: {self.stage.value} -> {stage.value}"
            )
        allowed = {
            "posting_evidence",
            "label_evidence",
            "prepublication_policy_evidence",
            "downstream_policy_records",
        }
        if extra := set(evidence) - allowed:
            raise ValueError(f"unknown completion evidence fields: {sorted(extra)}")
        return replace(self, stage=stage, **evidence)


@dataclass(frozen=True)
class CompletionReservation:
    """Issue-level exclusive reservation, independent of worktree existence."""

    repository_id: str
    issue_number: int
    generation_id: str
    completion_id: str
    owner_token_digest: str
    request_fingerprint: str
    result: str
    target_label: str
    outcome_payload: dict[str, Any]
    schema_version: int = _COMPLETION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _required_text(self.repository_id, "repository_id")
        _required_text(self.generation_id, "generation_id")
        _required_text(self.completion_id, "completion_id")
        _required_text(self.target_label, "target_label")
        _required_digest(self.owner_token_digest, "owner_token_digest")
        _required_digest(self.request_fingerprint, "request_fingerprint")
        if (
            not isinstance(self.issue_number, int)
            or isinstance(self.issue_number, bool)
            or self.issue_number <= 0
        ):
            raise ValueError("issue_number must be a positive integer")
        if self.result not in VALID_RESULTS or not isinstance(
            self.outcome_payload, dict
        ):
            raise ValueError("reservation result or outcome_payload is invalid")
        _validate_fixed_outcome(self.outcome_payload, self.issue_number, self.result)
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != _COMPLETION_SCHEMA_VERSION
        ):
            raise ValueError(
                f"unsupported completion reservation schema_version: {self.schema_version}"
            )

    @property
    def reservation_key(self) -> str:
        return f"{self.repository_id}::{self.issue_number}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repository_id": self.repository_id,
            "issue_number": self.issue_number,
            "generation_id": self.generation_id,
            "completion_id": self.completion_id,
            "owner_token_digest": self.owner_token_digest,
            "request_fingerprint": self.request_fingerprint,
            "result": self.result,
            "target_label": self.target_label,
            "outcome_payload": self.outcome_payload,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CompletionReservation:
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != _COMPLETION_SCHEMA_VERSION
        ):
            raise ValueError("completion_reservations schema_version is unsupported")
        return cls(**value)

    @classmethod
    def from_journal(cls, record: CompletionJournalRecord) -> CompletionReservation:
        return cls(
            repository_id=record.repository_id,
            issue_number=record.issue_number,
            generation_id=record.generation_id,
            completion_id=record.completion_id,
            owner_token_digest=record.owner_token_digest,
            request_fingerprint=record.request_fingerprint,
            result=record.result,
            target_label=record.target_label,
            outcome_payload=record.outcome_payload,
        )


@dataclass(frozen=True)
class CompletionReplayReceipt:
    """Immutable replay data written only after label proof and durable handoff."""

    record: CompletionJournalRecord

    def __post_init__(self) -> None:
        if self.record.stage is not CompleteStage.HANDED_OFF:
            raise ValueError("replay receipt requires a handed_off completion record")

    @property
    def receipt_key(self) -> str:
        return self.record.receipt_key

    def to_dict(self) -> dict[str, Any]:
        return self.record.to_dict()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CompletionReplayReceipt:
        return cls(CompletionJournalRecord.from_dict(value))


class CompletionJournalError(RuntimeError):
    """Raised when a completion-journal transition is rejected or fails to persist."""

    def __init__(self, reason: CompleteFailureReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _new_completion_id() -> str:
    return f"completion-{uuid4().hex}"


def _has_posting_evidence(comment_id: str | None, comment_url: str | None) -> bool:
    return bool(comment_id and comment_id.strip()) and bool(
        comment_url and comment_url.strip()
    )


def _lock_path_for(state_path: Path) -> Path:
    # `save_run_state` always asserts the lock derived from `state_path` (never a
    # caller-supplied override), so the acquired lock must match it exactly.
    return Path(state_path).with_suffix(".lock")


def _to_journal(active: Any) -> CompletionJournal:
    assert active.completion_id is not None
    assert active.completion_result is not None
    assert active.completion_stage is not None
    return CompletionJournal(
        issue_number=active.issue_number,
        claim_id=active.claim_id,
        completion_id=active.completion_id,
        result=active.completion_result,
        stage=active.completion_stage,
        payload=dict(active.completion_payload)
        if active.completion_payload is not None
        else None,
        comment_id=active.completion_comment_id,
        comment_url=active.completion_comment_url,
        handoff_ready=active.completion_handoff_ready,
    )


def _save_or_raise(run_state: Any, state_path: Path) -> None:
    try:
        save_run_state(run_state, state_path)
    except Exception as e:
        raise CompletionJournalError(
            CompleteFailureReason.STATE_SAVE_FAILED,
            f"Failed to persist completion journal: {e}",
        ) from e


def _acquire_run_state_lock(lock_path: Path, timeout_seconds: float) -> Any:
    try:
        cm = run_state_lock(lock_path, timeout=timeout_seconds)
        cm.__enter__()
        return cm
    except (FileLockContentionError, RuntimeError) as e:
        raise CompletionJournalError(
            CompleteFailureReason.STATE_LOCK_FAILED,
            f"Could not acquire run_state lock: {e}",
        ) from e


def _apply_or_reject_conflicting_payload(
    active: Any, payload: dict[str, Any] | None
) -> bool:
    """Apply a first-time payload in-memory, accept an identical resend as a
    no-op, or reject a differing one — including once the completion has
    already been handed off (with or without a payload on record yet).

    A concurrent or restarted caller resuming the same completion/result, or
    recording handoff evidence for it, must not silently overwrite payload
    content another attempt already reserved (and may already have posted):
    the persisted payload is the one that evidence (comment id/url) will end
    up describing. Returns True if the in-memory payload changed, so the
    caller knows whether it still needs to persist it.
    """
    if payload is None or active.completion_payload == payload:
        return False
    if active.completion_handoff_ready or active.completion_payload is not None:
        raise CompletionJournalError(
            CompleteFailureReason.CONCURRENT_COMPLETION,
            "A different completion payload is already reserved for this " "completion",
        )
    active.completion_payload = dict(payload)
    return True


def _resume_existing_reservation(
    active: Any,
    issue_number: int,
    completion_id: str | None,
    result: str,
    payload: dict[str, Any] | None,
    run_state: Any,
    state_path: Path,
) -> CompletionJournal:
    """Idempotently resume an already-reserved completion, or reject the attempt."""
    if completion_id is not None and completion_id != active.completion_id:
        raise CompletionJournalError(
            CompleteFailureReason.CONCURRENT_COMPLETION,
            f"A different completion ({active.completion_id!r}) is "
            f"already reserved for issue #{issue_number}",
        )
    if active.completion_result != result:
        raise CompletionJournalError(
            CompleteFailureReason.INVALID_STAGE_TRANSITION,
            f"Cannot change reserved completion result from "
            f"{active.completion_result!r} to {result!r}",
        )
    if _apply_or_reject_conflicting_payload(active, payload):
        _save_or_raise(run_state, state_path)
    return _to_journal(active)


def _validate_resumable_claim(active: Any, journal: CompletionJournal) -> None:
    """Reject a stale resume: the claim and reserved completion must be unchanged."""
    if active is None or active.claim_id != journal.claim_id:
        raise CompletionJournalError(
            CompleteFailureReason.CLAIM_NOT_FOUND,
            f"No active claim {journal.claim_id!r} found for issue "
            f"#{journal.issue_number}",
        )
    if (
        active.completion_id != journal.completion_id
        or active.completion_result != journal.result
    ):
        raise CompletionJournalError(
            CompleteFailureReason.INVALID_STAGE_TRANSITION,
            "Completion reservation changed since it was reserved (stale resume)",
        )


def _apply_handoff_evidence(
    active: Any,
    comment_id: str | None,
    comment_url: str | None,
    payload: dict[str, Any] | None,
) -> bool:
    """Apply new posting evidence onto ``active``.

    Returns True if the completion was already terminal (handed off), in
    which case the caller should return the existing record unchanged.
    Raises if the supplied evidence conflicts with an already-terminal
    record instead of matching it exactly.
    """
    if active.completion_handoff_ready:
        conflicts = (
            (comment_id is not None and comment_id != active.completion_comment_id)
            or (
                comment_url is not None and comment_url != active.completion_comment_url
            )
            or (payload is not None and payload != active.completion_payload)
        )
        if conflicts:
            raise CompletionJournalError(
                CompleteFailureReason.INVALID_STAGE_TRANSITION,
                "Completion already handed off to GC with different evidence; "
                "retried evidence must match exactly",
            )
        return True

    if comment_id is not None:
        active.completion_comment_id = comment_id
    if comment_url is not None:
        active.completion_comment_url = comment_url
    _apply_or_reject_conflicting_payload(active, payload)
    return False


@contextmanager
def completion_journal_lock(
    state_path: Path, timeout_seconds: float = 0.0
) -> Iterator[None]:
    """Acquire the shared run_state lock for a journal read-modify-write."""
    cm = _acquire_run_state_lock(_lock_path_for(state_path), timeout_seconds)
    try:
        yield
    finally:
        cm.__exit__(None, None, None)


def _new_contract_record(
    state: Any, candidate: CompletionJournalRecord
) -> CompletionJournalRecord | None:
    raw = state.completion_journal.get(candidate.journal_key)
    if raw is not None:
        return CompletionJournalRecord.from_dict(raw)
    for key, value in state.completion_journal.items():
        if (
            not isinstance(value, dict)
            or value.get("issue_number") != candidate.issue_number
        ):
            continue
        if value.get("repository_id") != candidate.repository_id:
            raise CompletionJournalError(
                CompleteFailureReason.REPOSITORY_IDENTITY_MISMATCH,
                "Completion repository identity does not match the persisted reservation",
            )
        if value.get("generation_id") != candidate.generation_id:
            raise CompletionJournalError(
                CompleteFailureReason.GENERATION_MISMATCH,
                "Completion generation does not match the persisted reservation",
            )
        if key != candidate.journal_key:
            raise CompletionJournalError(
                CompleteFailureReason.CONCURRENT_COMPLETION,
                "A different completion id is already reserved for this generation",
            )
    return None


def _verify_contract_identity(
    candidate: CompletionJournalRecord,
    persisted: CompletionJournalRecord,
    owner_token: str,
) -> None:
    if candidate.repository_id != persisted.repository_id:
        reason = CompleteFailureReason.REPOSITORY_IDENTITY_MISMATCH
    elif candidate.generation_id != persisted.generation_id:
        reason = CompleteFailureReason.GENERATION_MISMATCH
    elif candidate.completion_id != persisted.completion_id:
        reason = CompleteFailureReason.CONCURRENT_COMPLETION
    elif owner_token_digest(owner_token) != persisted.owner_token_digest:
        reason = CompleteFailureReason.OWNER_TOKEN_MISMATCH
    elif candidate.owner_token_digest != persisted.owner_token_digest:
        reason = CompleteFailureReason.OWNER_TOKEN_MISMATCH
    elif candidate.request_fingerprint != persisted.request_fingerprint:
        reason = CompleteFailureReason.REQUEST_FINGERPRINT_MISMATCH
    elif candidate.result != persisted.result:
        reason = CompleteFailureReason.INVALID_STAGE_TRANSITION
    elif (
        candidate.target_label != persisted.target_label
        or candidate.outcome_payload != persisted.outcome_payload
    ):
        reason = CompleteFailureReason.CONCURRENT_COMPLETION
    else:
        return
    raise CompletionJournalError(
        reason, f"Completion identity check failed: {reason.value}"
    )


def _validate_reservation_request(
    record: CompletionJournalRecord, owner_token: str
) -> None:
    if record.stage is not CompleteStage.RESERVED:
        raise CompletionJournalError(
            CompleteFailureReason.INVALID_STAGE_TRANSITION,
            "A new completion reservation must begin at the reserved stage",
        )
    if owner_token_digest(owner_token) != record.owner_token_digest:
        raise CompletionJournalError(
            CompleteFailureReason.OWNER_TOKEN_MISMATCH,
            "Owner token does not match the completion reservation",
        )


def _resume_reserved_contract(
    record: CompletionJournalRecord,
    current: CompletionJournalRecord | None,
    raw_reservation: dict[str, Any],
    owner_token: str,
) -> CompletionJournalRecord:
    reservation = CompletionReservation.from_dict(raw_reservation)
    if reservation.repository_id != record.repository_id:
        reason = CompleteFailureReason.REPOSITORY_IDENTITY_MISMATCH
    elif reservation.generation_id != record.generation_id:
        reason = CompleteFailureReason.GENERATION_MISMATCH
    elif reservation.completion_id != record.completion_id:
        reason = CompleteFailureReason.CONCURRENT_COMPLETION
    elif reservation.request_fingerprint != record.request_fingerprint:
        reason = CompleteFailureReason.REQUEST_FINGERPRINT_MISMATCH
    elif reservation.owner_token_digest != record.owner_token_digest:
        reason = CompleteFailureReason.OWNER_TOKEN_MISMATCH
    elif (
        reservation.result != record.result
        or reservation.target_label != record.target_label
        or reservation.outcome_payload != record.outcome_payload
    ):
        reason = CompleteFailureReason.CONCURRENT_COMPLETION
    else:
        if current is None:
            raise CompletionJournalError(
                CompleteFailureReason.STATE_SAVE_FAILED,
                "Reservation exists without its completion journal record",
            )
        _verify_contract_identity(record, current, owner_token)
        return current
    raise CompletionJournalError(reason, f"Issue reservation conflict: {reason.value}")


def reserve_completion_locked(
    record: CompletionJournalRecord,
    *,
    owner_token: str,
    state_path: Path,
) -> CompletionJournalRecord:
    """Reserve a new-contract completion when the caller already holds the state lock."""
    assert_run_state_lock_held(_lock_path_for(state_path))
    _validate_reservation_request(record, owner_token)
    run_state = load_run_state(state_path)
    current = _new_contract_record(run_state, record)
    reservation_key = record.reservation_key
    raw_reservation = run_state.completion_reservations.get(reservation_key)
    if raw_reservation is not None:
        return _resume_reserved_contract(record, current, raw_reservation, owner_token)
    if current is not None:
        _verify_contract_identity(record, current, owner_token)
        return current
    reservation = CompletionReservation.from_journal(record)
    run_state.completion_reservations[reservation_key] = reservation.to_dict()
    run_state.completion_journal[record.journal_key] = record.to_dict()
    _save_or_raise(run_state, state_path)
    return record


def _reserve_legacy_completion(
    *,
    issue_number: int,
    claim_id: str,
    owner_token: str,
    result: str,
    completion_id: str | None,
    payload: dict[str, Any] | None,
    state_path: Path,
    timeout_seconds: float,
) -> CompletionJournal:
    cm = _acquire_run_state_lock(_lock_path_for(state_path), timeout_seconds)
    try:
        run_state = load_run_state(state_path)
        active = run_state.active_worktrees.get(str(issue_number))
        if active is None or active.claim_id != claim_id:
            raise CompletionJournalError(
                CompleteFailureReason.CLAIM_NOT_FOUND,
                f"No active claim {claim_id!r} found for issue #{issue_number}",
            )
        if owner_token_digest(owner_token) != active.owner_token_digest:
            raise CompletionJournalError(
                CompleteFailureReason.OWNER_TOKEN_MISMATCH,
                "Owner token does not match the active claim",
            )
        if active.completion_id is not None:
            return _resume_existing_reservation(
                active,
                issue_number,
                completion_id,
                result,
                payload,
                run_state,
                state_path,
            )
        active.completion_id = completion_id or _new_completion_id()
        active.completion_result = result
        active.completion_stage = CompleteStage.JOURNALING.value
        if payload is not None:
            active.completion_payload = dict(payload)
        _save_or_raise(run_state, state_path)
        return _to_journal(active)
    finally:
        cm.__exit__(None, None, None)


@overload
def reserve_completion(
    *,
    record: CompletionJournalRecord,
    owner_token: str,
    state_path: Path,
    timeout_seconds: float = 0.0,
) -> CompletionJournalRecord: ...


@overload
def reserve_completion(
    *,
    issue_number: int,
    claim_id: str,
    owner_token: str,
    result: str,
    completion_id: str | None = None,
    payload: dict[str, Any] | None = None,
    state_path: Path,
    timeout_seconds: float = 0.0,
) -> CompletionJournal: ...


def reserve_completion(
    *,
    issue_number: int | None = None,
    claim_id: str | None = None,
    owner_token: str | None = None,
    result: str | None = None,
    completion_id: str | None = None,
    payload: dict[str, Any] | None = None,
    state_path: Path,
    timeout_seconds: float = 0.0,
    record: CompletionJournalRecord | None = None,
) -> CompletionJournal | CompletionJournalRecord:
    """Reserve (or idempotently resume) a completion under the claim's state lock.

    Rejects: the issue's claim no longer matching ``claim_id`` (re-claim),
    an owner-token mismatch, a different ``completion_id`` already reserved
    (concurrent completion), and reserving a different ``result`` than what
    is already reserved for this claim (double-complete / overwrite).
    """
    if record is not None:
        if owner_token is None:
            raise CompletionJournalError(
                CompleteFailureReason.OWNER_TOKEN_MISMATCH,
                "Owner token is required to reserve a completion",
            )
        with completion_journal_lock(state_path, timeout_seconds):
            return reserve_completion_locked(
                record, owner_token=owner_token, state_path=state_path
            )
    if (
        issue_number is None
        or claim_id is None
        or owner_token is None
        or result is None
    ):
        raise TypeError(
            "legacy reserve_completion requires issue_number, claim_id, owner_token, and result"
        )
    return _reserve_legacy_completion(
        issue_number=issue_number,
        claim_id=claim_id,
        owner_token=owner_token,
        result=result,
        completion_id=completion_id,
        payload=payload,
        state_path=state_path,
        timeout_seconds=timeout_seconds,
    )


def _mark_legacy_handoff_ready(
    journal: CompletionJournal,
    *,
    comment_id: str | None = None,
    comment_url: str | None = None,
    payload: dict[str, Any] | None = None,
    state_path: Path,
    timeout_seconds: float = 0.0,
) -> CompletionJournal:
    """Persist posting evidence and mark the completion ready for GC handoff.

    Rejects a stale resume: the claim must still be ``journal.claim_id`` and the
    persisted completion must still match ``journal.completion_id``/``result`` —
    otherwise something changed underneath this attempt (a reclaim, or a
    different completion winning the reservation) since it was reserved.
    Rejects marking handoff-ready unless a durable Issue comment id and url are
    on record (freshly supplied here, or already persisted from a prior call) —
    otherwise GC could treat an unposted outcome as safely handed off. A
    completion already handed off is terminal: an identical retry returns the
    existing record unchanged, and a retry with conflicting evidence is
    rejected rather than silently overwritten.
    """
    cm = _acquire_run_state_lock(_lock_path_for(state_path), timeout_seconds)
    try:
        run_state = load_run_state(state_path)
        active = run_state.active_worktrees.get(str(journal.issue_number))
        _validate_resumable_claim(active, journal)
        assert active is not None  # narrowed by _validate_resumable_claim above

        if _apply_handoff_evidence(active, comment_id, comment_url, payload):
            return _to_journal(active)

        if not _has_posting_evidence(
            active.completion_comment_id, active.completion_comment_url
        ):
            raise CompletionJournalError(
                CompleteFailureReason.EVIDENCE_MISSING,
                "Cannot mark handoff ready without a durably recorded Issue "
                "comment id and url",
            )

        active.completion_handoff_ready = True
        active.completion_stage = CompleteStage.HANDED_OFF_TO_GC.value
        _save_or_raise(run_state, state_path)
        return _to_journal(active)
    finally:
        cm.__exit__(None, None, None)


def _load_contract_for_update(
    run_state: Any,
    candidate: CompletionJournalRecord,
    owner_token: str,
) -> CompletionJournalRecord:
    persisted = _new_contract_record(run_state, candidate)
    if persisted is None:
        raise CompletionJournalError(
            CompleteFailureReason.COMPLETION_RESERVATION_NOT_FOUND,
            "No matching completion reservation exists",
        )
    _verify_contract_identity(candidate, persisted, owner_token)
    raw_reservation = run_state.completion_reservations.get(candidate.reservation_key)
    if raw_reservation is None:
        raise CompletionJournalError(
            CompleteFailureReason.COMPLETION_RESERVATION_NOT_FOUND,
            "No Issue-level completion reservation exists",
        )
    reservation = CompletionReservation.from_dict(raw_reservation)
    if reservation.generation_id != persisted.generation_id:
        reason = CompleteFailureReason.GENERATION_MISMATCH
    elif reservation.completion_id != persisted.completion_id:
        reason = CompleteFailureReason.CONCURRENT_COMPLETION
    elif reservation.request_fingerprint != persisted.request_fingerprint:
        reason = CompleteFailureReason.REQUEST_FINGERPRINT_MISMATCH
    elif reservation.owner_token_digest != persisted.owner_token_digest:
        reason = CompleteFailureReason.OWNER_TOKEN_MISMATCH
    else:
        return persisted
    raise CompletionJournalError(
        reason, f"Persisted reservation check failed: {reason.value}"
    )


def record_posting_evidence(
    record: CompletionJournalRecord,
    *,
    comment_id: str,
    comment_url: str,
    owner_token: str,
    state_path: Path,
    timeout_seconds: float = 0.0,
) -> CompletionJournalRecord:
    """Persist Issue outcome-comment evidence without claiming label proof."""
    if not _has_posting_evidence(comment_id, comment_url):
        raise CompletionJournalError(
            CompleteFailureReason.EVIDENCE_MISSING,
            "Posting evidence requires a non-empty comment id and url",
        )
    with completion_journal_lock(state_path, timeout_seconds):
        run_state = load_run_state(state_path)
        persisted = _load_contract_for_update(run_state, record, owner_token)
        evidence = {"comment_id": comment_id, "comment_url": comment_url}
        if persisted.stage is not CompleteStage.RESERVED:
            if persisted.posting_evidence != evidence:
                raise CompletionJournalError(
                    CompleteFailureReason.INVALID_STAGE_TRANSITION,
                    "Posting evidence conflicts with the already-recorded comment",
                )
            return persisted
        updated = persisted.advance(
            CompleteStage.OUTCOME_POSTED, posting_evidence=evidence
        )
        run_state.completion_journal[updated.journal_key] = updated.to_dict()
        _save_or_raise(run_state, state_path)
        return updated


def record_label_confirmation(
    record: CompletionJournalRecord,
    result: CompletionLabelTransitionResult,
    *,
    owner_token: str,
    state_path: Path,
    timeout_seconds: float = 0.0,
) -> CompletionJournalRecord:
    """Persist positive live label proof as a stage separate from posting."""
    if result.status is not CompletionLabelStatus.CONFIRMED:
        reason = result.failure_reason or CompleteFailureReason.LABEL_STATE_UNKNOWN
        raise CompletionJournalError(
            reason, f"Completion label transition failed: {reason.value}"
        )
    if result.target_label != record.target_label:
        raise CompletionJournalError(
            CompleteFailureReason.LABEL_CONFLICT,
            "Observed target label does not match the reserved completion label",
        )
    evidence = {
        "status": result.status.value,
        "target_label": result.target_label,
        "observed_labels": list(result.observed_labels),
    }
    with completion_journal_lock(state_path, timeout_seconds):
        run_state = load_run_state(state_path)
        persisted = _load_contract_for_update(run_state, record, owner_token)
        if persisted.stage in {CompleteStage.LABEL_CONFIRMED, CompleteStage.HANDED_OFF}:
            if persisted.label_evidence != evidence:
                raise CompletionJournalError(
                    CompleteFailureReason.LABEL_CONFLICT,
                    "A different label proof is already recorded",
                )
            return persisted
        if persisted.stage is not CompleteStage.OUTCOME_POSTED:
            raise CompletionJournalError(
                CompleteFailureReason.INVALID_STAGE_TRANSITION,
                "Label confirmation requires a durably posted outcome",
            )
        updated = persisted.advance(
            CompleteStage.LABEL_CONFIRMED, label_evidence=evidence
        )
        run_state.completion_journal[updated.journal_key] = updated.to_dict()
        _save_or_raise(run_state, state_path)
        return updated


def _mark_contract_handoff_ready(
    record: CompletionJournalRecord,
    *,
    owner_token: str,
    state_path: Path,
    timeout_seconds: float,
) -> CompletionJournalRecord:
    with completion_journal_lock(state_path, timeout_seconds):
        run_state = load_run_state(state_path)
        persisted = _load_contract_for_update(run_state, record, owner_token)
        if persisted.stage is CompleteStage.HANDED_OFF:
            raw_receipt = run_state.completion_replay_receipts.get(
                persisted.receipt_key
            )
            if raw_receipt != persisted.to_dict():
                raise CompletionJournalError(
                    CompleteFailureReason.STATE_SAVE_FAILED,
                    "Handed-off completion does not have a matching replay receipt",
                )
            return persisted
        if persisted.stage is not CompleteStage.LABEL_CONFIRMED:
            raise CompletionJournalError(
                CompleteFailureReason.INVALID_STAGE_TRANSITION,
                "Completion handoff requires confirmed target-label evidence",
            )
        handed_off = persisted.advance(CompleteStage.HANDED_OFF)
        receipt = CompletionReplayReceipt(handed_off)
        run_state.completion_journal[handed_off.journal_key] = handed_off.to_dict()
        run_state.completion_replay_receipts[receipt.receipt_key] = receipt.to_dict()
        _save_or_raise(run_state, state_path)
        return handed_off


@overload
def mark_handoff_ready(
    journal: CompletionJournal,
    *,
    comment_id: str | None = None,
    comment_url: str | None = None,
    payload: dict[str, Any] | None = None,
    state_path: Path,
    timeout_seconds: float = 0.0,
) -> CompletionJournal: ...


@overload
def mark_handoff_ready(
    journal: CompletionJournalRecord,
    *,
    owner_token: str,
    state_path: Path,
    timeout_seconds: float = 0.0,
) -> CompletionJournalRecord: ...


def mark_handoff_ready(
    journal: CompletionJournal | CompletionJournalRecord,
    *,
    comment_id: str | None = None,
    comment_url: str | None = None,
    payload: dict[str, Any] | None = None,
    owner_token: str | None = None,
    state_path: Path,
    timeout_seconds: float = 0.0,
) -> CompletionJournal | CompletionJournalRecord:
    """Use the legacy GC handoff or the label-confirmed replay handoff contract."""
    if isinstance(journal, CompletionJournalRecord):
        if owner_token is None:
            raise CompletionJournalError(
                CompleteFailureReason.OWNER_TOKEN_MISMATCH,
                "Owner token is required for completion handoff",
            )
        if comment_id is not None or comment_url is not None or payload is not None:
            raise CompletionJournalError(
                CompleteFailureReason.INVALID_STAGE_TRANSITION,
                "Posting evidence and fixed outcome payload must be recorded separately",
            )
        return _mark_contract_handoff_ready(
            journal,
            owner_token=owner_token,
            state_path=state_path,
            timeout_seconds=timeout_seconds,
        )
    return _mark_legacy_handoff_ready(
        journal,
        comment_id=comment_id,
        comment_url=comment_url,
        payload=payload,
        state_path=state_path,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "CompletionJournalRecord",
    "CompletionJournal",
    "CompletionJournalError",
    "CompletionReplayReceipt",
    "CompletionReservation",
    "DownstreamPolicyRecord",
    "completion_journal_lock",
    "mark_handoff_ready",
    "record_label_confirmation",
    "record_posting_evidence",
    "reserve_completion",
    "reserve_completion_locked",
]
