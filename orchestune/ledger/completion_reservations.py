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
    journal_key_for,
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
_COMPLETION_IDENTITY_FIELDS = (
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


def _invalid_reservation_applies(
    key: object, raw_json: dict[str, Any], issue_number: int
) -> bool:
    """Fail closed when identity is unattributable or the key and payload conflict.

    An unattributable record, or contradictory issue numbers, blocks every
    Issue because the affected reservation cannot be determined safely. The
    key parser expects a reservation key (<repository>::<issue>), not a journal
    key whose issue number is not the final component.
    """
    raw_issue = raw_json.get("issue_number")
    raw_is_valid = (
        isinstance(raw_issue, int) and not isinstance(raw_issue, bool) and raw_issue > 0
    )
    key_issue: int | None = None
    if isinstance(key, str):
        suffix = key.rsplit("::", 1)[-1]
        if suffix.isdecimal():
            parsed = int(suffix)
            if parsed > 0 and str(parsed) == suffix:
                key_issue = parsed

    if raw_is_valid and key_issue is not None and raw_issue != key_issue:
        return True
    if raw_is_valid:
        return raw_issue == issue_number or key_issue == issue_number
    if key_issue is not None:
        return key_issue == issue_number
    return True


def _records(state: Any, issue_number: int) -> list[CompletionReservation]:
    raw = getattr(state, "completion_reservations", {})
    if not isinstance(raw, dict):
        raise ValueError("Invalid completion reservations")
    records: list[CompletionReservation] = []
    for key, value in raw.items():
        if isinstance(value, InvalidLedgerRecord):
            if _invalid_reservation_applies(key, value.raw_json, issue_number):
                raise ValueError(
                    f"Invalid completion reservation at map key {key!r}; "
                    "repair the run-state ledger before retrying"
                )
            continue
        if not isinstance(value, CompletionReservation):
            raise ValueError("Invalid completion reservation")
        if value.issue_number == issue_number:
            if key != value.reservation_key:
                raise ValueError("Invalid reservation key")
            records.append(value)
    return records


def completion_record(state: Any, issue_number: int) -> CompletionJournalRecord | None:
    """Resolve an Issue reservation and require its immutable journal identity."""
    records = _records(state, issue_number)
    if not records:
        return None
    if len(records) != 1:
        raise ValueError("Ambiguous completion reservation")
    reservation = records[0]
    journal = getattr(state, "completion_journal", {})
    if not isinstance(journal, dict):
        raise ValueError("Invalid completion journal")
    journal_key = journal_key_for(
        reservation.repository_id,
        reservation.issue_number,
        reservation.generation_id,
        reservation.completion_id,
    )
    record = journal.get(journal_key)
    if not isinstance(record, CompletionJournalRecord):
        # Preserve a useful identity-mismatch diagnostic for corrupt map keys.
        record = next(
            (
                value
                for value in journal.values()
                if isinstance(value, CompletionJournalRecord)
                and value.reservation_key == reservation.reservation_key
                and value.generation_id == reservation.generation_id
                and value.completion_id == reservation.completion_id
            ),
            None,
        )
    if not isinstance(record, CompletionJournalRecord):
        raise ValueError("Completion journal identity mismatch")
    if journal.get(record.journal_key) is not record:
        raise ValueError("Completion journal key does not match its identity")
    if any(
        getattr(record, name) != getattr(reservation, name)
        for name in _COMPLETION_IDENTITY_FIELDS
    ):
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
        receipt = getattr(state, "completion_replay_receipts", {}).get(
            record.receipt_key
        )
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
