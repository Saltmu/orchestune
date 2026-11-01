"""Completion decision table for external recovery; never repairs publication."""

from __future__ import annotations

from orchestune.ledger.completion_reservations import (
    completion_record,
    completion_reservation_status,
)
from orchestune.ledger.completion_records import (
    CompletionJournalRecord,
    InvalidLedgerRecord,
)
from orchestune.ledger.run_state import ActiveWorktree, RunState


def completion_retention(active: ActiveWorktree, state: RunState) -> tuple[bool, str]:
    issue = active.core.issue_number
    status = completion_reservation_status(state, issue)
    if status == "invalid":
        raise ValueError("completion_state_invalid")
    reserved = completion_record(state, issue)
    journals = []
    for key, record in state.completion_journal.items():
        if isinstance(record, InvalidLedgerRecord):
            if record.raw_json.get("issue_number") == issue:
                raise ValueError("completion_state_invalid")
            continue
        if not isinstance(record, CompletionJournalRecord):
            raise ValueError("completion_state_invalid")
        if record.issue_number != issue:
            continue
        required = (record.repository_id, record.issue_number, record.generation_id, record.completion_id)
        if any(not value for value in required):
            raise ValueError("completion_state_invalid")
        if key != "::".join(str(value) for value in required):
            raise ValueError("completion_state_invalid")
        if record.generation_id == active.claim.claim_id:
            journals.append(record)
    if len(journals) > 1:
        raise ValueError("completion_state_invalid")
    for record in journals + ([reserved] if reserved else []):
        if (
            record.repository_id != active.claim.repository_id
            or record.generation_id != active.claim.claim_id
            or record.owner_token_digest != active.claim.owner_token_digest
            or (
                bool(active.completion.completion_id)
                and record.completion_id != active.completion.completion_id
            )
        ):
            raise ValueError("completion_state_invalid")
    if reserved:
        return True, "completion_retained"
    if journals:
        return True, "completion_resume_required"
    return bool(
        active.completion.completion_id
    ), "completion_retained" if active.completion.completion_id else "release"
