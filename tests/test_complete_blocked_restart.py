"""New claim generations and dry-run honor immutable completion reservations."""

from dataclasses import replace

import pytest
from complete_lifecycle_test_support import lifecycle_environment

from orchestune.complete.contracts import CompleteFailureReason, CompleteRequest
from orchestune.complete.journal import completion_journal_lock
from orchestune.complete.service import complete_task
from orchestune.ledger.active_records import ActiveCompletionJournal
from orchestune.ledger.run_state import load_run_state_readonly, save_run_state


@pytest.fixture
def blocked_history(tmp_path, monkeypatch):
    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch, result="blocked")
    result = complete_task(request, forge=forge)
    assert result.success, result.failure
    return request, forge, result


def test_fresh_id_dry_run_rejects_same_generation_reserved_completion(blocked_history):
    request, forge, _ = blocked_history
    candidate = CompleteRequest.done(
        request.issue_number,
        42,
        reviewer="skip",
        claim_id=request.claim_id,
        state_path=request.state_path,
        worktree_root=request.worktree_root,
        dry_run=True,
        completion_id="completion-new-attempt",
    )
    before = request.state_path.read_bytes()
    result = complete_task(candidate, forge=forge)
    assert not result.success
    assert result.failure.reason == CompleteFailureReason.CONCURRENT_COMPLETION
    assert request.state_path.read_bytes() == before


def test_new_generation_done_keeps_old_blocked_receipt(blocked_history):
    request, forge, old_result = blocked_history
    state = load_run_state_readonly(request.state_path)
    old_receipts = dict(state.completion_replay_receipts)
    active = state.active_worktrees[str(request.issue_number)]
    current = replace(
        active,
        claim=replace(active.claim, claim_id="claim-restarted"),
        completion=ActiveCompletionJournal(
            completion_policy_config=active.completion.completion_policy_config
        ),
    )
    state.active_worktrees[str(request.issue_number)] = current
    with completion_journal_lock(request.state_path):
        save_run_state(state, request.state_path)
    forge.labels = {"status:in-progress"}
    candidate = CompleteRequest.done(
        request.issue_number,
        42,
        reviewer="skip",
        claim_id=current.claim.claim_id,
        state_path=request.state_path,
        worktree_root=request.worktree_root,
    )
    result = complete_task(candidate, forge=forge)
    assert result.success, result.failure
    state = load_run_state_readonly(request.state_path)
    assert len(state.completion_replay_receipts) == 2
    for key, old in old_receipts.items():
        assert state.completion_replay_receipts[key] == old
    assert complete_task(candidate, forge=forge).completion_id == result.completion_id
    historical = complete_task(
        replace(request, completion_id=old_result.completion_id), forge=forge
    )
    assert historical.success and historical.result == "blocked"
