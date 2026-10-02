"""Durable completion journaling under the common run-state lock.

Legacy helpers retain independent read-modify-write lock boundaries. The
label-confirmed publisher holds completion_journal_lock across reservation,
bounded remote I/O, evidence saves, and atomic receipt/handoff persistence.
CI validation runs before acquiring that lock.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, overload
from uuid import uuid4

from orchestune.complete.contracts import (
    CompleteFailureReason,
    CompleteStage,
    CompletionLabelStatus,
    CompletionLabelTransitionResult,
    DownstreamPolicyRecord,
)
from orchestune.complete.journal_models import (
    CompletionJournal,
    CompletionJournalRecord,
    CompletionReplayReceipt,
    CompletionReservation,
)
from orchestune.infra.process_utils import (
    FileLockContentionError,
    assert_run_state_lock_held,
    run_state_lock,
)
from orchestune.ledger.active_lifecycle import has_completion_reservation
from orchestune.ledger.active_records import (
    ActiveCompletionJournal,
    ActiveWorktree,
)
from orchestune.ledger.run_state import load_run_state, save_run_state


def thaw_json(value: Any) -> Any:
    """Copy immutable JSON views back into dict/list containers."""
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [thaw_json(item) for item in value]
    return value


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


def with_completion(
    active: ActiveWorktree, completion: ActiveCompletionJournal
) -> ActiveWorktree:
    """Return a copy replacing only completion-owned fields through the owner boundary."""
    if not isinstance(completion, ActiveCompletionJournal):
        raise TypeError("completion must be ActiveCompletionJournal")
    return replace(active, completion=completion)


def active_completion_from_record(
    record: CompletionJournalRecord,
    *,
    handoff: bool = False,
    completion_policy_config: Any | None = None,
) -> ActiveCompletionJournal:
    """Explicitly convert durable CompletionJournalRecord to transient ActiveCompletionJournal."""
    payload = {
        **record.outcome_payload,
        "outcome": record.outcome_payload["body"],
    }
    comment_id = None
    comment_url = None
    if record.posting_evidence:
        comment_id = record.posting_evidence.get("comment_id")
        comment_url = record.posting_evidence.get("comment_url")
    return ActiveCompletionJournal(
        completion_id=record.completion_id,
        completion_result=record.result,
        completion_stage=record.stage.value,
        completion_payload=payload,
        completion_comment_id=comment_id,
        completion_comment_url=comment_url,
        completion_handoff_ready=handoff,
        completion_policy_config=completion_policy_config,
    )


def apply_completion_record(
    active: ActiveWorktree,
    record: CompletionJournalRecord,
    *,
    handoff: bool = False,
) -> ActiveWorktree:
    """Replace all completion fields on active with the state derived from record."""
    completion = active_completion_from_record(
        record,
        handoff=handoff,
        completion_policy_config=active.completion.completion_policy_config,
    )
    if (
        completion.completion_comment_id is None
        and active.completion.completion_comment_id is not None
    ):
        completion = replace(
            completion,
            completion_comment_id=active.completion.completion_comment_id,
            completion_comment_url=active.completion.completion_comment_url,
        )
    return with_completion(active, completion)


def _to_journal(active: Any) -> CompletionJournal:
    completion = active.completion
    assert completion.completion_id is not None
    assert completion.completion_result is not None
    assert completion.completion_stage is not None
    return CompletionJournal(
        issue_number=active.core.issue_number,
        claim_id=active.claim.claim_id,
        completion_id=completion.completion_id,
        result=completion.completion_result,
        stage=completion.completion_stage,
        payload=dict(completion.completion_payload)
        if completion.completion_payload is not None
        else None,
        comment_id=completion.completion_comment_id,
        comment_url=completion.completion_comment_url,
        handoff_ready=completion.completion_handoff_ready,
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
    active: ActiveWorktree, payload: dict[str, Any] | None
) -> tuple[ActiveWorktree, bool]:
    """Apply a first-time payload, accept an identical resend as a no-op, or
    reject a differing one.

    Returns (active, True) if the payload changed, so the caller knows whether
    it still needs to persist the updated active worktree. Returns (active, False)
    if the payload is None or already identical.
    """
    if payload is None or active.completion.completion_payload == payload:
        return active, False
    if (
        active.completion.completion_handoff_ready
        or active.completion.completion_payload is not None
    ):
        raise CompletionJournalError(
            CompleteFailureReason.CONCURRENT_COMPLETION,
            "A different completion payload is already reserved for this completion",
        )
    new_comp = replace(active.completion, completion_payload=dict(payload))
    return with_completion(active, new_comp), True


def _resume_existing_reservation(
    active: ActiveWorktree,
    issue_number: int,
    completion_id: str | None,
    result: str,
    payload: dict[str, Any] | None,
    run_state: Any,
    state_path: Path,
) -> CompletionJournal:
    """Idempotently resume an already-reserved completion, or reject the attempt."""
    if completion_id is not None and completion_id != active.completion.completion_id:
        raise CompletionJournalError(
            CompleteFailureReason.CONCURRENT_COMPLETION,
            f"A different completion ({active.completion.completion_id!r}) is "
            f"already reserved for issue #{issue_number}",
        )
    if active.completion.completion_result != result:
        raise CompletionJournalError(
            CompleteFailureReason.INVALID_STAGE_TRANSITION,
            f"Cannot change reserved completion result from "
            f"{active.completion.completion_result!r} to {result!r}",
        )
    active, changed = _apply_or_reject_conflicting_payload(active, payload)
    if changed:
        run_state.active_worktrees[str(issue_number)] = active
        _save_or_raise(run_state, state_path)
    return _to_journal(active)


def _validate_resumable_claim(
    active: ActiveWorktree | None, journal: CompletionJournal
) -> None:
    """Reject a stale resume: the claim and reserved completion must be unchanged."""
    if active is None or active.claim.claim_id != journal.claim_id:
        raise CompletionJournalError(
            CompleteFailureReason.CLAIM_NOT_FOUND,
            f"No active claim {journal.claim_id!r} found for issue "
            f"#{journal.issue_number}",
        )
    if (
        active.completion.completion_id != journal.completion_id
        or active.completion.completion_result != journal.result
    ):
        raise CompletionJournalError(
            CompleteFailureReason.INVALID_STAGE_TRANSITION,
            "Completion reservation changed since it was reserved (stale resume)",
        )


def _apply_handoff_evidence(
    active: ActiveWorktree,
    comment_id: str | None,
    comment_url: str | None,
    payload: dict[str, Any] | None,
) -> tuple[ActiveWorktree, bool]:
    """Apply new posting evidence onto ``active``.

    Returns (active, True) if the completion was already terminal (handed off),
    in which case the caller should return the existing record unchanged.
    Returns (active, False) if new evidence was applied. Caller must store the
    returned active worktree on run_state.
    Raises if the supplied evidence conflicts with an already-terminal
    record instead of matching it exactly.
    """
    if active.completion.completion_handoff_ready:
        conflicts = (
            (
                comment_id is not None
                and comment_id != active.completion.completion_comment_id
            )
            or (
                comment_url is not None
                and comment_url != active.completion.completion_comment_url
            )
            or (payload is not None and payload != active.completion.completion_payload)
        )
        if conflicts:
            raise CompletionJournalError(
                CompleteFailureReason.INVALID_STAGE_TRANSITION,
                "Completion already handed off to GC with different evidence; "
                "retried evidence must match exactly",
            )
        return active, True

    cid = (
        comment_id
        if comment_id is not None
        else active.completion.completion_comment_id
    )
    curl = (
        comment_url
        if comment_url is not None
        else active.completion.completion_comment_url
    )
    new_comp = replace(
        active.completion,
        completion_comment_id=cid,
        completion_comment_url=curl,
    )
    active = with_completion(active, new_comp)
    active, _ = _apply_or_reject_conflicting_payload(active, payload)
    return active, False


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


def _invalid_completion_state(message: str, error: Exception) -> CompletionJournalError:
    return CompletionJournalError(
        CompleteFailureReason.INVALID_COMPLETION_STATE,
        f"Invalid persisted completion state: {message}",
    )


def _load_contract_state(state_path: Path) -> Any:
    try:
        return load_run_state(state_path)
    except (TypeError, ValueError) as error:
        raise _invalid_completion_state(str(error), error) from error


def _parse_contract_record(raw: dict[str, Any]) -> CompletionJournalRecord:
    try:
        return CompletionJournalRecord.from_dict(raw)
    except (KeyError, TypeError, ValueError) as error:
        raise _invalid_completion_state(str(error), error) from error


def _parse_contract_reservation(raw: dict[str, Any]) -> CompletionReservation:
    try:
        return CompletionReservation.from_dict(raw)
    except (KeyError, TypeError, ValueError) as error:
        raise _invalid_completion_state(str(error), error) from error


def _has_durable_handoff_receipt(state: Any, record: CompletionJournalRecord) -> bool:
    return (
        record.stage is CompleteStage.HANDED_OFF
        and state.completion_replay_receipts.get(record.receipt_key) == record.to_dict()
    )


def _new_contract_record(
    state: Any, candidate: CompletionJournalRecord
) -> CompletionJournalRecord | None:
    raw = state.completion_journal.get(candidate.journal_key)
    if raw is not None:
        return _parse_contract_record(raw)
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
        persisted = _parse_contract_record(value)
        if value.get("generation_id") != candidate.generation_id:
            if _has_durable_handoff_receipt(state, persisted):
                continue
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


def _resume_reserved_contract(
    record: CompletionJournalRecord,
    current: CompletionJournalRecord | None,
    raw_reservation: dict[str, Any],
    owner_token: str,
) -> CompletionJournalRecord:
    reservation = _parse_contract_reservation(raw_reservation)
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


def _allow_terminal_generation_replacement(
    state: Any,
    reservation: CompletionReservation,
    candidate: CompletionJournalRecord,
) -> None:
    if (reservation.repository_id, reservation.issue_number) != (
        candidate.repository_id,
        candidate.issue_number,
    ):
        raise CompletionJournalError(
            CompleteFailureReason.REPOSITORY_IDENTITY_MISMATCH,
            "Issue reservation belongs to a different repository or issue",
        )
    old_key = (
        f"{reservation.repository_id}::{reservation.issue_number}::"
        f"{reservation.generation_id}::{reservation.completion_id}"
    )
    raw = state.completion_journal.get(old_key)
    if raw is None:
        raise CompletionJournalError(
            CompleteFailureReason.INVALID_COMPLETION_STATE,
            "Issue reservation has no matching journal record",
        )
    previous = _parse_contract_record(raw)
    if (
        reservation.owner_token_digest != previous.owner_token_digest
        or reservation.request_fingerprint != previous.request_fingerprint
        or reservation.result != previous.result
        or reservation.target_label != previous.target_label
        or reservation.outcome_payload != previous.outcome_payload
    ):
        raise CompletionJournalError(
            CompleteFailureReason.INVALID_COMPLETION_STATE,
            "Issue reservation identity does not match its journal record",
        )
    if not _has_durable_handoff_receipt(state, previous):
        raise CompletionJournalError(
            CompleteFailureReason.GENERATION_MISMATCH,
            "A new generation cannot replace an incomplete completion reservation",
        )


def reserve_completion_locked(
    record: CompletionJournalRecord,
    *,
    owner_token: str = "",
    state_path: Path,
) -> CompletionJournalRecord:
    """Reserve a new-contract completion when the caller already holds the state lock."""
    assert_run_state_lock_held(_lock_path_for(state_path))
    _validate_reservation_request(record, owner_token)
    run_state = _load_contract_state(state_path)
    current = _new_contract_record(run_state, record)
    reservation_key = record.reservation_key
    raw_reservation = run_state.completion_reservations.get(reservation_key)
    if raw_reservation is not None:
        reservation = _parse_contract_reservation(raw_reservation)
        if reservation.generation_id == record.generation_id:
            return _resume_reserved_contract(
                record, current, raw_reservation, owner_token
            )
        _allow_terminal_generation_replacement(run_state, reservation, record)
        if current is not None:
            raise CompletionJournalError(
                CompleteFailureReason.INVALID_COMPLETION_STATE,
                "A new generation journal exists without its Issue reservation",
            )
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
        if active is None or active.claim.claim_id != claim_id:
            raise CompletionJournalError(
                CompleteFailureReason.CLAIM_NOT_FOUND,
                f"No active claim {claim_id!r} found for issue #{issue_number}",
            )
        if has_completion_reservation(active):
            return _resume_existing_reservation(
                active,
                issue_number,
                completion_id,
                result,
                payload,
                run_state,
                state_path,
            )
        new_comp = replace(
            active.completion,
            completion_id=completion_id or _new_completion_id(),
            completion_result=result,
            completion_stage=CompleteStage.JOURNALING.value,
            completion_payload=(
                dict(payload)
                if payload is not None
                else active.completion.completion_payload
            ),
        )
        active = with_completion(active, new_comp)
        run_state.active_worktrees[str(issue_number)] = active
        _save_or_raise(run_state, state_path)
        return _to_journal(active)
    finally:
        cm.__exit__(None, None, None)


@overload
def reserve_completion(
    *,
    record: CompletionJournalRecord,
    owner_token: str = "",
    state_path: Path,
    timeout_seconds: float = 0.0,
) -> CompletionJournalRecord: ...


@overload
def reserve_completion(
    *,
    issue_number: int,
    claim_id: str,
    owner_token: str = "",
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
    a claim-generation mismatch, a different ``completion_id`` already reserved
    (concurrent completion), and reserving a different ``result`` than what
    is already reserved for this claim (double-complete / overwrite).
    """
    if record is not None:
        with completion_journal_lock(state_path, timeout_seconds):
            return reserve_completion_locked(
                record, owner_token=owner_token or "", state_path=state_path
            )
    if issue_number is None or claim_id is None or result is None:
        raise TypeError(
            "legacy reserve_completion requires issue_number, claim_id, and result"
        )
    return _reserve_legacy_completion(
        issue_number=issue_number,
        claim_id=claim_id,
        owner_token=owner_token or "",
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

        active, ready = _apply_handoff_evidence(
            active, comment_id, comment_url, payload
        )
        if ready:
            return _to_journal(active)

        if not _has_posting_evidence(
            active.completion.completion_comment_id,
            active.completion.completion_comment_url,
        ):
            raise CompletionJournalError(
                CompleteFailureReason.EVIDENCE_MISSING,
                "Cannot mark handoff ready without a durably recorded Issue "
                "comment id and url",
            )

        new_comp = replace(
            active.completion,
            completion_handoff_ready=True,
            completion_stage=CompleteStage.HANDED_OFF_TO_GC.value,
        )
        active = with_completion(active, new_comp)
        run_state.active_worktrees[str(journal.issue_number)] = active
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
    reservation = _parse_contract_reservation(raw_reservation)
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
    owner_token: str = "",
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
        run_state = _load_contract_state(state_path)
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
    owner_token: str = "",
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
        "observed_labels": sorted(set(result.observed_labels)),
    }
    with completion_journal_lock(state_path, timeout_seconds):
        run_state = _load_contract_state(state_path)
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
        run_state = _load_contract_state(state_path)
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
    owner_token: str = "",
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
        if comment_id is not None or comment_url is not None or payload is not None:
            raise CompletionJournalError(
                CompleteFailureReason.INVALID_STAGE_TRANSITION,
                "Posting evidence and fixed outcome payload must be recorded separately",
            )
        return _mark_contract_handoff_ready(
            journal,
            owner_token=owner_token or "",
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
    "active_completion_from_record",
    "apply_completion_record",
    "completion_journal_lock",
    "mark_handoff_ready",
    "record_label_confirmation",
    "record_posting_evidence",
    "reserve_completion",
    "reserve_completion_locked",
    "thaw_json",
    "with_completion",
]
