"""Discover completion subjects that have no active worktree for later policy GC."""

from typing import Any

from orchestune.dispatch.cycle_events import UnclaimedReservationHold, freeze_json
from orchestune.ledger.completion_reservations import (
    completion_record,
    completion_reservation_status,
    dependency_completion_blocked,
)


def unclaimed_completion_events(state: Any) -> list[UnclaimedReservationHold]:
    events = []
    for raw in state.completion_reservations.values():
        issue = raw["issue_number"]
        if str(issue) in state.active_worktrees:
            continue
        status = completion_reservation_status(state, issue)
        if status == "handed_off" and not dependency_completion_blocked(state, issue):
            continue
        record = completion_record(state, issue) if status != "invalid" else None
        records = record.get("downstream_policy_records", {}) if record else {}
        frozen = freeze_json(records)
        assert frozen is not None
        events.append(
            UnclaimedReservationHold(
                issue_number=issue,
                completion_id=raw["completion_id"],
                generation_id=raw["generation_id"],
                reason="not-needed-review-pending"
                if status == "handed_off"
                else status,
                downstream_policy_records=frozen,
            )
        )
    return events
