"""#1001: completion journal reservation and handoff-ready state transitions."""

from __future__ import annotations

import pytest

from orchestune.claim.ownership import owner_token_digest
from orchestune.complete.contracts import CompleteFailureReason
from orchestune.complete.journal import (
    CompletionJournalError,
    mark_handoff_ready,
    reserve_completion,
)
from orchestune.dispatch.state import ActiveWorktree, RunState, load_run_state
from orchestune.dispatch.state import save_run_state as save_run_state_unlocked
from orchestune.infra.process_utils import run_state_lock
from tests.dispatch_test_support import save_locked_run_state

OWNER_TOKEN = "owner-token-abc"


def _seed_active(tmp_path, **overrides):
    path = tmp_path / "run_state.json"
    fields = {
        "issue_number": 10,
        "branch": "claude/issue-10-x",
        "worktree_path": "worktrees/claude-issue-10-x",
        "pid": 12345,
        "started_at": 1700000000.0,
        "declared_footprint": (),
        "owner_kind": "interactive",
        "claim_id": "claim-10",
        "claim_stage": "completed",
        "base_ref": "origin/main",
        "base_sha": "a" * 40,
        "reservation_kind": "footprint",
        "repository_id": "Saltmu/orchestune",
        "claimed_at": 1700000001.0,
        "owner_token_digest": owner_token_digest(OWNER_TOKEN),
    }
    fields.update(overrides)
    active = ActiveWorktree(**fields)
    save_locked_run_state(RunState(active_worktrees={"10": active}), path)
    return path


def _reserve(path, **overrides):
    kwargs = {
        "issue_number": 10,
        "claim_id": "claim-10",
        "owner_token": OWNER_TOKEN,
        "result": "done",
        "state_path": path,
    }
    kwargs.update(overrides)
    return reserve_completion(**kwargs)


class TestReserveCompletion:
    def test_reserves_new_completion_and_persists_under_the_claim_lock(self, tmp_path):
        path = _seed_active(tmp_path)

        journal = _reserve(path)

        assert journal.issue_number == 10
        assert journal.claim_id == "claim-10"
        assert journal.result == "done"
        assert journal.stage == "journaling"
        assert journal.completion_id
        assert journal.handoff_ready is False

        persisted = load_run_state(path).active_worktrees["10"]
        assert persisted.completion_id == journal.completion_id
        assert persisted.completion_result == "done"
        assert persisted.completion_stage == "journaling"
        assert persisted.completion_handoff_ready is False

    def test_resuming_without_a_known_completion_id_is_idempotent(self, tmp_path):
        path = _seed_active(tmp_path)
        first = _reserve(path)
        second = _reserve(path)
        assert second == first

    def test_resuming_the_same_completion_id_and_result_is_idempotent(self, tmp_path):
        path = _seed_active(tmp_path)
        first = _reserve(path)
        second = _reserve(path, completion_id=first.completion_id)
        assert second == first

    def test_rejects_overwrite_with_a_different_result_under_the_same_completion_id(
        self, tmp_path
    ):
        path = _seed_active(tmp_path)
        first = _reserve(path, result="done")

        with pytest.raises(CompletionJournalError) as excinfo:
            _reserve(path, result="blocked", completion_id=first.completion_id)
        assert excinfo.value.reason == CompleteFailureReason.INVALID_STAGE_TRANSITION

        persisted = load_run_state(path).active_worktrees["10"]
        assert persisted.completion_result == "done"

    def test_rejects_a_different_result_even_without_an_explicit_completion_id(
        self, tmp_path
    ):
        path = _seed_active(tmp_path)
        _reserve(path, result="done")

        with pytest.raises(CompletionJournalError) as excinfo:
            _reserve(path, result="blocked")
        assert excinfo.value.reason == CompleteFailureReason.INVALID_STAGE_TRANSITION

    def test_rejects_a_second_concurrent_completion_with_a_different_completion_id(
        self, tmp_path
    ):
        path = _seed_active(tmp_path)
        _reserve(path)

        with pytest.raises(CompletionJournalError) as excinfo:
            _reserve(path, completion_id="some-other-id")
        assert excinfo.value.reason == CompleteFailureReason.CONCURRENT_COMPLETION

    def test_rejects_reservation_after_a_reclaim_changed_the_claim_id(self, tmp_path):
        path = _seed_active(tmp_path)

        with pytest.raises(CompletionJournalError) as excinfo:
            _reserve(path, claim_id="claim-stale")
        assert excinfo.value.reason == CompleteFailureReason.CLAIM_NOT_FOUND

    def test_rejects_unknown_issue(self, tmp_path):
        path = tmp_path / "run_state.json"
        save_locked_run_state(RunState(), path)

        with pytest.raises(CompletionJournalError) as excinfo:
            _reserve(path)
        assert excinfo.value.reason == CompleteFailureReason.CLAIM_NOT_FOUND

    def test_rejects_owner_token_mismatch(self, tmp_path):
        path = _seed_active(tmp_path)

        with pytest.raises(CompletionJournalError) as excinfo:
            _reserve(path, owner_token="wrong-token")
        assert excinfo.value.reason == CompleteFailureReason.OWNER_TOKEN_MISMATCH

    def test_persists_the_provided_completion_payload(self, tmp_path):
        path = _seed_active(tmp_path)

        journal = _reserve(path, payload={"pr": 42})

        assert journal.payload == {"pr": 42}
        assert load_run_state(path).active_worktrees["10"].completion_payload == {
            "pr": 42
        }

    def test_save_failure_does_not_leave_a_half_applied_reservation(
        self, tmp_path, monkeypatch
    ):
        path = _seed_active(tmp_path)
        import orchestune.complete.journal as journal_module

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(journal_module, "save_run_state", _boom)

        with pytest.raises(CompletionJournalError) as excinfo:
            _reserve(path)
        assert excinfo.value.reason == CompleteFailureReason.STATE_SAVE_FAILED

        persisted = load_run_state(path).active_worktrees["10"]
        assert persisted.completion_id is None

    def test_releases_the_lock_even_when_reservation_is_rejected(self, tmp_path):
        path = _seed_active(tmp_path)

        with pytest.raises(CompletionJournalError):
            _reserve(path, claim_id="claim-stale")

        # the lock must have been released by the rejected attempt above.
        with run_state_lock(path.with_suffix(".lock"), timeout=1.0):
            pass


class TestMarkHandoffReady:
    def test_marks_handoff_ready_and_persists_comment_and_payload(self, tmp_path):
        path = _seed_active(tmp_path)
        journal = _reserve(path)

        ready = mark_handoff_ready(
            journal,
            comment_id="999",
            comment_url="https://github.com/Saltmu/orchestune/issues/10#issuecomment-999",
            payload={"pr": 42, "review": {"bot": "codex", "rounds": 1}},
            state_path=path,
        )

        assert ready.handoff_ready is True
        assert ready.comment_id == "999"
        assert ready.stage == "handed_off_to_gc"

        persisted = load_run_state(path).active_worktrees["10"]
        assert persisted.completion_handoff_ready is True
        assert persisted.completion_comment_id == "999"
        assert persisted.completion_comment_url.endswith("999")
        assert persisted.completion_payload == {
            "pr": 42,
            "review": {"bot": "codex", "rounds": 1},
        }

    def test_does_not_hold_the_lock_between_reserve_and_handoff(self, tmp_path):
        path = _seed_active(tmp_path)
        journal = _reserve(path)

        # simulate a long-running CI step performed by the caller between
        # reservation and handoff: acquiring the lock here must not block,
        # proving reserve_completion already released it.
        with run_state_lock(path.with_suffix(".lock"), timeout=1.0):
            pass

        ready = mark_handoff_ready(
            journal, comment_id="1", comment_url="u", payload=None, state_path=path
        )
        assert ready.handoff_ready is True

    def test_rejects_marking_handoff_ready_without_any_posting_evidence(self, tmp_path):
        path = _seed_active(tmp_path)
        journal = _reserve(path)

        with pytest.raises(CompletionJournalError) as excinfo:
            mark_handoff_ready(journal, state_path=path)
        assert excinfo.value.reason == CompleteFailureReason.EVIDENCE_MISSING

        persisted = load_run_state(path).active_worktrees["10"]
        assert persisted.completion_handoff_ready is False
        assert persisted.completion_comment_id is None

    def test_rejects_marking_handoff_ready_with_only_a_comment_id(self, tmp_path):
        path = _seed_active(tmp_path)
        journal = _reserve(path)

        with pytest.raises(CompletionJournalError) as excinfo:
            mark_handoff_ready(journal, comment_id="1", state_path=path)
        assert excinfo.value.reason == CompleteFailureReason.EVIDENCE_MISSING

    def test_accepts_evidence_already_persisted_by_a_prior_call(self, tmp_path):
        path = _seed_active(tmp_path)
        journal = _reserve(path)
        # a prior process recorded evidence but crashed before flipping
        # handoff_ready; a resuming call with no new evidence should still
        # succeed because the persisted evidence already satisfies it.
        mark_handoff_ready(journal, comment_id="1", comment_url="u", state_path=path)

        ready = mark_handoff_ready(journal, state_path=path)
        assert ready.handoff_ready is True
        assert ready.comment_id == "1"

    def test_rejects_stale_resume_after_a_reclaim_replaced_the_completion(
        self, tmp_path
    ):
        path = _seed_active(tmp_path)
        journal = _reserve(path)

        with run_state_lock(path.with_suffix(".lock")):
            state = load_run_state(path)
            state.active_worktrees["10"] = ActiveWorktree(
                issue_number=10,
                branch="claude/issue-10-x",
                worktree_path="worktrees/claude-issue-10-x",
                pid=54321,
                started_at=1700000100.0,
                declared_footprint=(),
                owner_kind="interactive",
                claim_id="claim-10-b",
                claim_stage="completed",
                base_ref="origin/main",
                base_sha="b" * 40,
                reservation_kind="footprint",
                repository_id="Saltmu/orchestune",
                claimed_at=1700000101.0,
                owner_token_digest=owner_token_digest(OWNER_TOKEN),
            )
            save_run_state_unlocked(state, path)

        with pytest.raises(CompletionJournalError) as excinfo:
            mark_handoff_ready(
                journal, comment_id="1", comment_url="u", payload=None, state_path=path
            )
        assert excinfo.value.reason == CompleteFailureReason.CLAIM_NOT_FOUND

    def test_rejects_stale_resume_after_a_different_completion_was_reserved(
        self, tmp_path
    ):
        path = _seed_active(tmp_path)
        journal = _reserve(path)

        with run_state_lock(path.with_suffix(".lock")):
            state = load_run_state(path)
            active = state.active_worktrees["10"]
            active.completion_id = "completion-other"
            active.completion_result = "blocked"
            active.completion_stage = "journaling"
            save_run_state_unlocked(state, path)

        with pytest.raises(CompletionJournalError) as excinfo:
            mark_handoff_ready(
                journal, comment_id="1", comment_url="u", payload=None, state_path=path
            )
        assert excinfo.value.reason == CompleteFailureReason.INVALID_STAGE_TRANSITION

    def test_idempotent_resume_of_an_already_handed_off_completion(self, tmp_path):
        path = _seed_active(tmp_path)
        journal = _reserve(path)

        first = mark_handoff_ready(
            journal,
            comment_id="1",
            comment_url="u",
            payload={"pr": 1},
            state_path=path,
        )
        second = mark_handoff_ready(
            first, comment_id="1", comment_url="u", payload={"pr": 1}, state_path=path
        )
        assert second == first

    def test_save_failure_does_not_report_handoff_ready(self, tmp_path, monkeypatch):
        path = _seed_active(tmp_path)
        journal = _reserve(path)
        import orchestune.complete.journal as journal_module

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(journal_module, "save_run_state", _boom)

        with pytest.raises(CompletionJournalError) as excinfo:
            mark_handoff_ready(
                journal, comment_id="1", comment_url="u", payload=None, state_path=path
            )
        assert excinfo.value.reason == CompleteFailureReason.STATE_SAVE_FAILED

        persisted = load_run_state(path).active_worktrees["10"]
        assert persisted.completion_handoff_ready is False
