"""Completion decision table for external recovery; never repairs publication."""

from __future__ import annotations

from orchestune.ledger.completion_reservations import (
    completion_record,
    completion_reservation_status,
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
        if not isinstance(record, dict):
            raise ValueError("completion_state_invalid")
        if record.get("issue_number") != issue:
            continue
        required = ("repository_id", "issue_number", "generation_id", "completion_id")
        if any(not record.get(name) for name in required):
            raise ValueError("completion_state_invalid")
        if key != "::".join(str(record[name]) for name in required):
            raise ValueError("completion_state_invalid")
        if record["generation_id"] == active.claim.claim_id:
            journals.append(record)
    if len(journals) > 1:
        raise ValueError("completion_state_invalid")
    for record in journals + ([reserved] if reserved else []):
        if (
            record.get("repository_id") != active.claim.repository_id
            or record.get("generation_id") != active.claim.claim_id
            or record.get("owner_token_digest") != active.claim.owner_token_digest
            or (
                bool(active.completion.completion_id)
                and record.get("completion_id") != active.completion.completion_id
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
