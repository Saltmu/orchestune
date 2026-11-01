"""Conservative completion exclusion shared by writers and dependency readers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from orchestune.labels import NOT_NEEDED_REVIEW_PENDING
from orchestune.ledger.completion_records import (
    CompletionJournalRecord,
    CompletionReplayReceipt,
    CompletionReservation,
    InvalidLedgerRecord,
)
from orchestune.ledger.run_state import load_run_state_readonly
from orchestune.ownership_contracts import CompleteStage

_PENDING_STAGES = frozenset(
    {
        CompleteStage.RESERVED,
        CompleteStage.OUTCOME_POSTED,
        CompleteStage.LABEL_CONFIRMED,
    }
)


def _records(state: Any, issue_number: int) -> list[CompletionReservation]:
    raw = getattr(state, "completion_reservations", {})
    if not isinstance(raw, dict):
        raise ValueError("Invalid completion reservations")
    records: list[CompletionReservation] = []
    for key, value in raw.items():
        if isinstance(value, InvalidLedgerRecord):
            if value.raw_json.get("issue_number") == issue_number:
                raise ValueError("Invalid completion reservation")
            continue
        if not isinstance(value, CompletionReservation):
            raise ValueError("Invalid completion reservation")
        if value.issue_number == issue_number:
            if key != value.reservation_key:
                raise ValueError("Invalid reservation key")
            records.append(value)
    return records


def completion_record(
    state: Any, issue_number: int
) -> CompletionJournalRecord | None:
    """Resolve an Issue reservation and require its immutable journal identity."""
    records = _records(state, issue_number)
    if not records:
        return None
    if len(records) != 1:
        raise ValueError("Ambiguous completion reservation")
    reservation = records[0]
    key = "::".join(
        str(value)
        for value in (
            reservation.repository_id,
            reservation.issue_number,
            reservation.generation_id,
            reservation.completion_id,
        )
    )
    record = getattr(state, "completion_journal", {}).get(key)
    if not isinstance(record, CompletionJournalRecord):
        raise ValueError("Completion journal identity mismatch")
    identity = (
        "repository_id",
        "issue_number",
        "generation_id",
        "completion_id",
        "owner_token_digest",
        "request_fingerprint",
        "result",
        "target_label",
        "outcome_payload",
    )
    if any(getattr(record, name) != getattr(reservation, name) for name in identity):
        raise ValueError("Completion journal identity mismatch")
    return record


def completion_reservation_status(state: Any, issue_number: int) -> str:
    """Return legacy, pending, handed_off, or invalid; unknown proof never releases."""
    try:
        record = completion_record(state, issue_number)
        if record is None:
            return "legacy"
        if record.stage in _PENDING_STAGES:
            return "pending"
        if record.stage is not CompleteStage.HANDED_OFF:
            return "invalid"
        posting, labels = record.posting_evidence, record.label_evidence
        if (
            not isinstance(posting, dict)
            or not posting.get("comment_id")
            or not posting.get("comment_url")
        ):
            return "invalid"
        if (
            not isinstance(labels, dict)
            or labels.get("status") != "confirmed"
            or record.target_label not in labels.get("observed_labels", [])
        ):
            return "invalid"
        receipt = getattr(state, "completion_replay_receipts", {}).get(record.receipt_key)
        if not isinstance(receipt, CompletionReplayReceipt):
            return "invalid"
        if replace(receipt.record, downstream_policy_records=()) != replace(
            record, downstream_policy_records=()
        ):
            return "invalid"
        return "handed_off"
    except (KeyError, TypeError, ValueError):
        return "invalid"


def completion_mutation_blocked(state: Any, issue_number: int) -> bool:
    return completion_reservation_status(state, issue_number) in {"pending", "invalid"}


def completion_mutation_blocked_fresh(
    state: Any, issue_number: int, state_path: Path | str
) -> bool:
    """Check current transaction data and persisted reservation after its lock boundary."""
    if completion_mutation_blocked(state, issue_number):
        return True
    try:
        return completion_mutation_blocked(
            load_run_state_readonly(state_path), issue_number
        )
    except (OSError, ValueError):
        return True


def dependency_completion_blocked(
    state: Any, issue_number: int, observed_labels: tuple[str, ...] = ()
) -> bool:
    if NOT_NEEDED_REVIEW_PENDING in observed_labels:
        return True
    if completion_mutation_blocked(state, issue_number):
        return True
    try:
        record = completion_record(state, issue_number)
        if record is None:
            return False
        return any(
            policy.policy_kind == "not-needed-review" and policy.status != "applied"
            for policy in record.downstream_policy_records
        )
    except (TypeError, ValueError):
        return True


def completion_handoff_matches_active(state: Any, active: Any) -> bool:
    if completion_reservation_status(state, active.core.issue_number) != "handed_off":
        return False
    record = completion_record(state, active.core.issue_number)
    assert record is not None
    posting = record.posting_evidence
    return bool(
        posting
        and all(
            (
                record.repository_id == active.claim.repository_id,
                record.generation_id == active.claim.claim_id,
                record.completion_id == active.completion.completion_id,
                record.result == active.completion.completion_result,
                record.owner_token_digest == active.claim.owner_token_digest,
                posting.get("comment_id") == active.completion.completion_comment_id,
                posting.get("comment_url") == active.completion.completion_comment_url,
            )
        )
    )


def completion_subject_mutation_blocked(
    state: Any, subject_id: str | None, state_path: Path | str
) -> bool:
    return bool(
        subject_id
        and subject_id.isdigit()
        and completion_mutation_blocked_fresh(state, int(subject_id), state_path)
    )


def dependency_completion_blocked_fresh(
    state: Any,
    issue_number: int,
    state_path: Path | str,
    observed_labels: tuple[str, ...] = (),
) -> bool:
    if dependency_completion_blocked(state, issue_number, observed_labels):
        return True
    try:
        return dependency_completion_blocked(
            load_run_state_readonly(state_path), issue_number, observed_labels
        )
    except (OSError, ValueError, TypeError):
        return True
