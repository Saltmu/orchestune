"""Dispatcher discovery and dependency holds for worktree-free policy subjects."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from test_complete_unclaimed import unclaimed as _unclaimed_fixture

from orchestune.complete.journal import CompletionJournalRecord, completion_journal_lock
from orchestune.complete.publication import update_downstream_policy_locked
from orchestune.complete.service import complete_task
from orchestune.dispatch.cycle_actions import _run_active_worktree_rules
from orchestune.dispatch.gc.unclaimed import unclaimed_completion_events
from orchestune.ledger.completion_reservations import dependency_completion_blocked
from orchestune.ledger.run_state import load_run_state_readonly

unclaimed = _unclaimed_fixture


def test_dispatcher_enumerates_unclaimed_review_pending_without_worktree(unclaimed):
    request, forge = unclaimed
    result = complete_task(request, forge=forge)
    assert result.success
    state = load_run_state_readonly(request.state_path)
    ctx = SimpleNamespace(run_state=state, queries=Mock())
    events, deviations, serial = _run_active_worktree_rules(ctx)
    assert len(events) == 1 and not deviations and not serial
    assert events[0]["issue_number"] == 1111
    assert events[0]["completion_id"] == result.completion_id
    assert events[0]["reason"] == "not-needed-review-pending"
    assert "worktree_path" not in events[0]
    assert dependency_completion_blocked(state, 1111)
    record = CompletionJournalRecord.from_dict(
        next(iter(state.completion_journal.values()))
    )
    applied = replace(record.downstream_policy_records[0], status="applied")
    with completion_journal_lock(request.state_path):
        update_downstream_policy_locked(request.state_path, record, applied)
    state = load_run_state_readonly(request.state_path)
    assert not dependency_completion_blocked(state, 1111)
    assert not unclaimed_completion_events(state)


def test_dispatcher_keeps_interrupted_reservation_without_active_worktree(unclaimed):
    request, forge = unclaimed
    forge.inject = (
        lambda op, after: (_ for _ in ()).throw(OSError("offline"))
        if op == "post"
        else None
    )
    result = complete_task(request, forge=forge)
    assert not result.success
    state = load_run_state_readonly(request.state_path)
    events = unclaimed_completion_events(state)
    assert events[0]["reason"] == "pending"
    assert dependency_completion_blocked(state, 1111)
