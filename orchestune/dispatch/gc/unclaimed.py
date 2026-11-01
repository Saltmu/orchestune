"""Discover completion subjects that have no active worktree for later policy GC."""

from typing import Any

from orchestune.dispatch.cycle_events import UnclaimedReservationHold, freeze_json
from orchestune.ledger.completion_reservations import (
    completion_record,
    completion_reservation_status,
    dependency_completion_blocked,
)
from orchestune.ledger.completion_records import InvalidLedgerRecord


def unclaimed_completion_events(state: Any) -> list[UnclaimedReservationHold]:
    events = []
    for reservation in state.completion_reservations.values():
        if isinstance(reservation, InvalidLedgerRecord):
            issue = reservation.raw_json.get("issue_number")
            if not isinstance(issue, int):
                continue
            completion_id = reservation.raw_json.get("completion_id", "")
            generation_id = reservation.raw_json.get("generation_id", "")
        else:
            issue = reservation.issue_number
            completion_id = reservation.completion_id
            generation_id = reservation.generation_id
        if str(issue) in state.active_worktrees:
            continue
        status = completion_reservation_status(state, issue)
        if status == "handed_off" and not dependency_completion_blocked(state, issue):
            continue
        record = completion_record(state, issue) if status != "invalid" else None
        records = (
            {
                policy.policy_key: policy.to_dict()
                for policy in record.downstream_policy_records
            }
            if record
            else {}
        )
        frozen = freeze_json(records)
        assert frozen is not None
        events.append(
            UnclaimedReservationHold(
                issue_number=issue,
                completion_id=completion_id,
                generation_id=generation_id,
                reason="not-needed-review-pending"
                if status == "handed_off"
                else status,
                downstream_policy_records=frozen,
            )
        )
    return events
