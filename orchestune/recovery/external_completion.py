"""Completion decision table for external recovery; never repairs publication."""

from __future__ import annotations

from typing import Any

from orchestune.ledger.completion_records import (
    CompletionJournalRecord,
    InvalidLedgerRecord,
)
from orchestune.ledger.completion_reservations import (
    completion_record,
    completion_reservation_status,
)
from orchestune.ledger.run_state import ActiveWorktree, RunState


def _journals_for_issue(
    journal: dict[str, Any], issue: int, active_claim_id: str | None
) -> list[CompletionJournalRecord]:
    journals = []
    for key, record in journal.items():
        if isinstance(record, InvalidLedgerRecord):
            if record.raw_json.get("issue_number") == issue:
                raise ValueError("completion_state_invalid")
            continue
        if not isinstance(record, CompletionJournalRecord):
            raise ValueError("completion_state_invalid")
        if record.issue_number != issue:
            continue
        required = (
            record.repository_id,
            record.issue_number,
            record.generation_id,
            record.completion_id,
        )
        if any(not value for value in required):
            raise ValueError("completion_state_invalid")
        if key != "::".join(str(value) for value in required):
            raise ValueError("completion_state_invalid")
        if record.generation_id == active_claim_id:
            journals.append(record)
    if len(journals) > 1:
        raise ValueError("completion_state_invalid")
    return journals


def _assert_completion_repository(records: list[Any], active: ActiveWorktree) -> None:
    for record in records:
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


def completion_retention(active: ActiveWorktree, state: RunState) -> tuple[bool, str]:
    issue = active.core.issue_number
    status = completion_reservation_status(state, issue)
    if status == "invalid":
        raise ValueError("completion_state_invalid")
    reserved = completion_record(state, issue)
    journals = _journals_for_issue(
        state.completion_journal, issue, active.claim.claim_id
    )
    records = journals + ([reserved] if reserved else [])
    _assert_completion_repository(records, active)
    if reserved:
        return True, "completion_retained"
    if journals:
        return True, "completion_resume_required"
    return bool(
        active.completion.completion_id
    ), "completion_retained" if active.completion.completion_id else "release"
