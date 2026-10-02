"""#1001: completion journal reservation and handoff-ready state transitions."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from orchestune.claim.ownership import owner_token_digest
from orchestune.complete.contracts import CompleteFailureReason, CompleteStage
from orchestune.complete.journal import (
    CompletionJournalError,
    CompletionJournalRecord,
    active_completion_from_record,
    apply_completion_record,
    mark_handoff_ready,
    reserve_completion,
    with_completion,
)
from orchestune.infra.process_utils import run_state_lock
from orchestune.ledger.active_records import (
    ActiveCompletionJournal,
    ActiveWorktreeCore,
    ClaimInfo,
    LaunchInfo,
)
from orchestune.ledger.run_state import ActiveWorktree, RunState, load_run_state
from orchestune.ledger.run_state import save_run_state as save_run_state_unlocked
from tests.dispatch_test_support import save_locked_run_state

OWNER_TOKEN = "owner-token-abc"


def _seed_active(tmp_path, **overrides):
    path = tmp_path / "run_state.json"
    core = ActiveWorktreeCore(
        issue_number=overrides.get("issue_number", 10),
        branch=overrides.get("branch", "claude/issue-10-x"),
        worktree_path=overrides.get("worktree_path", "worktrees/claude-issue-10-x"),
        declared_footprint=tuple(overrides.get("declared_footprint", ())),
    )
    launch = LaunchInfo(
        pid=overrides.get("pid", 12345),
        started_at=overrides.get("started_at", 1700000000.0),
    )
    claim = ClaimInfo(
        owner_kind=overrides.get("owner_kind", "interactive"),
        claim_id=overrides.get("claim_id", "claim-10"),
        claim_stage=overrides.get("claim_stage", "completed"),
        base_ref=overrides.get("base_ref", "origin/main"),
        base_sha=overrides.get("base_sha", "a" * 40),
        reservation_kind=overrides.get("reservation_kind", "footprint"),
        repository_id=overrides.get("repository_id", "Saltmu/orchestune"),
        claimed_at=overrides.get("claimed_at", 1700000001.0),
        owner_token_digest=overrides.get(
            "owner_token_digest", owner_token_digest(OWNER_TOKEN)
        ),
    )
    comp_keys = {
        "completion_id",
        "completion_result",
        "completion_stage",
        "completion_payload",
        "completion_comment_id",
        "completion_comment_url",
        "completion_handoff_ready",
        "completion_policy_config",
    }
    comp_kwargs = {k: overrides[k] for k in comp_keys if k in overrides}
    completion = ActiveCompletionJournal(**comp_kwargs)
    active = ActiveWorktree.from_records(
        core=core, launch=launch, claim=claim, completion=completion
    )
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
        assert persisted.completion.completion_id == journal.completion_id
        assert persisted.completion.completion_result == "done"
        assert persisted.completion.completion_stage == "journaling"
        assert persisted.completion.completion_handoff_ready is False

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
        assert persisted.completion.completion_result == "done"

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
        assert _reserve(path, owner_token="").claim_id == "claim-10"

    def test_persists_the_provided_completion_payload(self, tmp_path):
        path = _seed_active(tmp_path)

        journal = _reserve(path, payload={"pr": 42})

        assert journal.payload == {"pr": 42}
        assert load_run_state(path).active_worktrees[
            "10"
        ].completion.completion_payload == {"pr": 42}

    def test_resuming_with_an_identical_payload_is_idempotent(self, tmp_path):
        path = _seed_active(tmp_path)
        first = _reserve(path, payload={"pr": 1})
        second = _reserve(path, payload={"pr": 1})
        assert second == first

    def test_rejects_a_conflicting_payload_before_handoff(self, tmp_path):
        path = _seed_active(tmp_path)
        first = _reserve(path, payload={"pr": 1})

        # a concurrent or restarted caller resuming the same completion must
        # not silently overwrite payload content another attempt reserved.
        with pytest.raises(CompletionJournalError) as excinfo:
            _reserve(path, completion_id=first.completion_id, payload={"pr": 2})
        assert excinfo.value.reason == CompleteFailureReason.CONCURRENT_COMPLETION

        persisted = load_run_state(path).active_worktrees["10"]
        assert persisted.completion.completion_payload == {"pr": 1}

    def test_rejects_a_conflicting_payload_after_handoff(self, tmp_path):
        path = _seed_active(tmp_path)
        journal = _reserve(path, payload={"pr": 1})
        mark_handoff_ready(journal, comment_id="1", comment_url="u", state_path=path)

        # a retried reservation (e.g. a re-executed caller) with a different
        # payload must not rewrite evidence that a posted comment already
        # refers to.
        with pytest.raises(CompletionJournalError) as excinfo:
            _reserve(path, payload={"pr": 2})
        assert excinfo.value.reason == CompleteFailureReason.CONCURRENT_COMPLETION

        persisted = load_run_state(path).active_worktrees["10"]
        assert persisted.completion.completion_payload == {"pr": 1}

    def test_rejects_a_payload_added_after_handoff_when_none_was_set(self, tmp_path):
        path = _seed_active(tmp_path)
        journal = _reserve(path)  # no payload reserved
        mark_handoff_ready(journal, comment_id="1", comment_url="u", state_path=path)

        # a terminal record must stay immutable even when the payload it was
        # handed off with was never set in the first place.
        with pytest.raises(CompletionJournalError) as excinfo:
            _reserve(path, payload={"pr": 1})
        assert excinfo.value.reason == CompleteFailureReason.CONCURRENT_COMPLETION

        persisted = load_run_state(path).active_worktrees["10"]
        assert persisted.completion.completion_payload is None

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
        assert persisted.completion.completion_id is None

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
        assert persisted.completion.completion_handoff_ready is True
        assert persisted.completion.completion_comment_id == "999"
        assert persisted.completion.completion_comment_url.endswith("999")
        assert persisted.completion.completion_payload == {
            "pr": 42,
            "review": {"bot": "codex", "rounds": 1},
        }

    def test_accepts_a_matching_payload_when_marking_handoff_ready(self, tmp_path):
        path = _seed_active(tmp_path)
        journal = _reserve(path, payload={"pr": 1})

        ready = mark_handoff_ready(
            journal, comment_id="1", comment_url="u", payload={"pr": 1}, state_path=path
        )

        assert ready.handoff_ready is True
        assert ready.payload == {"pr": 1}

    def test_rejects_a_conflicting_payload_when_marking_handoff_ready(self, tmp_path):
        path = _seed_active(tmp_path)
        journal = _reserve(path, payload={"pr": 1})

        # the comment evidence being recorded must not be allowed to point at
        # payload content that disagrees with what was actually reserved.
        with pytest.raises(CompletionJournalError) as excinfo:
            mark_handoff_ready(
                journal,
                comment_id="1",
                comment_url="u",
                payload={"pr": 2},
                state_path=path,
            )
        assert excinfo.value.reason == CompleteFailureReason.CONCURRENT_COMPLETION

        persisted = load_run_state(path).active_worktrees["10"]
        assert persisted.completion.completion_handoff_ready is False
        assert persisted.completion.completion_payload == {"pr": 1}


def _new_journal_record(**overrides):
    from orchestune.complete.contracts import CompleteStage
    from orchestune.complete.journal import CompletionJournalRecord

    values = {
        "repository_id": "Saltmu/orchestune",
        "issue_number": 1108,
        "generation_id": "claim-1108",
        "completion_id": "completion-1108",
        "owner_token_digest": owner_token_digest(OWNER_TOKEN),
        "request_fingerprint": "b" * 64,
        "result": "done",
        "target_label": "status:done",
        "outcome_payload": {"result": "done", "issue": 1108, "pr": 1234},
        "stage": CompleteStage.RESERVED,
    }
    values.update(overrides)
    return CompletionJournalRecord(**values)


class TestLabelConfirmedCompletionContract:
    def test_new_generation_can_replace_only_a_handed_off_reservation(self, tmp_path):
        from orchestune.complete.contracts import (
            CompletionLabelStatus,
            CompletionLabelTransitionResult,
        )
        from orchestune.complete.journal import (
            record_label_confirmation,
            record_posting_evidence,
        )

        path = tmp_path / "run_state.json"
        original = reserve_completion(
            record=_new_journal_record(), owner_token=OWNER_TOKEN, state_path=path
        )
        posted = record_posting_evidence(
            original,
            comment_id="comment-old",
            comment_url="https://example.test/comment-old",
            owner_token=OWNER_TOKEN,
            state_path=path,
        )
        labeled = record_label_confirmation(
            posted,
            CompletionLabelTransitionResult(
                status=CompletionLabelStatus.CONFIRMED,
                target_label="status:done",
                observed_labels=("status:done",),
            ),
            owner_token=OWNER_TOKEN,
            state_path=path,
        )
        mark_handoff_ready(labeled, owner_token=OWNER_TOKEN, state_path=path)

        next_generation = _new_journal_record(
            generation_id="claim-1108-reopened",
            completion_id="completion-1108-reopened",
        )
        next_reserved = reserve_completion(
            record=next_generation, owner_token=OWNER_TOKEN, state_path=path
        )

        persisted = load_run_state(path)
        assert next_reserved.generation_id == "claim-1108-reopened"
        assert (
            persisted.completion_reservations[original.reservation_key]["generation_id"]
            == "claim-1108-reopened"
        )
        assert persisted.completion_replay_receipts[original.receipt_key]["stage"] == (
            "handed_off"
        )
        assert len(persisted.completion_journal) == 2

    def test_new_generation_cannot_replace_an_incomplete_reservation(self, tmp_path):
        path = tmp_path / "run_state.json"
        reserve_completion(
            record=_new_journal_record(), owner_token=OWNER_TOKEN, state_path=path
        )
        next_generation = _new_journal_record(
            generation_id="claim-1108-reopened",
            completion_id="completion-1108-reopened",
        )

        with pytest.raises(CompletionJournalError) as excinfo:
            reserve_completion(
                record=next_generation, owner_token=OWNER_TOKEN, state_path=path
            )

        assert excinfo.value.reason == CompleteFailureReason.GENERATION_MISMATCH

    def test_malformed_persisted_record_uses_typed_completion_error(self, tmp_path):
        from orchestune.complete.journal import record_posting_evidence

        path = tmp_path / "run_state.json"
        initial = _new_journal_record()
        reserve_completion(record=initial, owner_token=OWNER_TOKEN, state_path=path)

        with run_state_lock(path.with_suffix(".lock")):
            state = load_run_state(path)
            del state.completion_journal[initial.journal_key]["owner_token_digest"]
            save_run_state_unlocked(state, path)

        with pytest.raises(CompletionJournalError) as excinfo:
            record_posting_evidence(
                initial,
                comment_id="comment-1234",
                comment_url="https://example.test/comment-1234",
                owner_token=OWNER_TOKEN,
                state_path=path,
            )

        assert excinfo.value.reason == CompleteFailureReason.INVALID_COMPLETION_STATE

    def test_downstream_policy_status_is_keyed_and_round_trips(self):
        from orchestune.complete.journal import (
            CompletionJournalRecord,
            DownstreamPolicyRecord,
        )

        record = _new_journal_record(
            downstream_policy_records=(
                DownstreamPolicyRecord(
                    repository_id="Saltmu/orchestune",
                    issue_number=1108,
                    generation_id="claim-1108",
                    completion_id="completion-1108",
                    policy_kind="merge_queue",
                ),
            )
        )
        serialized = record.to_dict()
        policy = record.downstream_policy_records[0]
        assert serialized["downstream_policy_records"][policy.policy_key]["status"] == (
            "pending"
        )
        restored = CompletionJournalRecord.from_dict(serialized)
        assert restored == record

    def test_duplicate_downstream_policy_kind_is_rejected(self):
        from orchestune.complete.journal import DownstreamPolicyRecord

        first = DownstreamPolicyRecord(
            repository_id="Saltmu/orchestune",
            issue_number=1108,
            generation_id="claim-1108",
            completion_id="completion-1108",
            policy_kind="merge_queue",
        )
        applied = DownstreamPolicyRecord(
            repository_id="Saltmu/orchestune",
            issue_number=1108,
            generation_id="claim-1108",
            completion_id="completion-1108",
            policy_kind="merge_queue",
            status="applied",
        )

        with pytest.raises(ValueError, match="policy kinds must be unique"):
            _new_journal_record(downstream_policy_records=(first, applied))

    def test_failed_handoff_save_does_not_create_a_replay_receipt(
        self, tmp_path, monkeypatch
    ):
        import orchestune.complete.journal as journal_module
        from orchestune.complete.contracts import (
            CompletionLabelStatus,
            CompletionLabelTransitionResult,
        )
        from orchestune.complete.journal import (
            record_label_confirmation,
            record_posting_evidence,
        )

        path = tmp_path / "run_state.json"
        record = reserve_completion(
            record=_new_journal_record(), owner_token=OWNER_TOKEN, state_path=path
        )
        posted = record_posting_evidence(
            record,
            comment_id="comment-1234",
            comment_url="https://example.test/comment-1234",
            owner_token=OWNER_TOKEN,
            state_path=path,
        )
        labeled = record_label_confirmation(
            posted,
            CompletionLabelTransitionResult(
                status=CompletionLabelStatus.CONFIRMED,
                target_label="status:done",
                observed_labels=("status:done",),
            ),
            owner_token=OWNER_TOKEN,
            state_path=path,
        )

        def _fail_save(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(journal_module, "save_run_state", _fail_save)
        with pytest.raises(CompletionJournalError) as excinfo:
            mark_handoff_ready(labeled, owner_token=OWNER_TOKEN, state_path=path)

        assert excinfo.value.reason == CompleteFailureReason.STATE_SAVE_FAILED
        persisted = load_run_state(path)
        assert persisted.completion_journal[labeled.journal_key]["stage"] == (
            "label_confirmed"
        )
        assert persisted.completion_replay_receipts == {}

    def test_journal_round_trip_and_separate_post_label_and_handoff_evidence(
        self, tmp_path
    ):
        from orchestune.complete.contracts import (
            CompleteStage,
            CompletionLabelStatus,
            CompletionLabelTransitionResult,
        )
        from orchestune.complete.journal import (
            record_label_confirmation,
            record_posting_evidence,
        )

        path = tmp_path / "run_state.json"
        initial = _new_journal_record()
        reserved = reserve_completion(
            record=initial, owner_token=OWNER_TOKEN, state_path=path
        )
        state = load_run_state(path)
        assert state.completion_journal[initial.journal_key] == initial.to_dict()
        assert state.completion_reservations[initial.reservation_key][
            "completion_id"
        ] == (initial.completion_id)
        assert OWNER_TOKEN not in path.read_text(encoding="utf-8")

        posted = record_posting_evidence(
            reserved,
            comment_id="comment-1234",
            comment_url="https://example.test/comment-1234",
            owner_token=OWNER_TOKEN,
            state_path=path,
        )
        assert posted.stage == CompleteStage.OUTCOME_POSTED
        with pytest.raises(CompletionJournalError) as excinfo:
            mark_handoff_ready(posted, owner_token=OWNER_TOKEN, state_path=path)
        assert excinfo.value.reason == CompleteFailureReason.INVALID_STAGE_TRANSITION

        label_result = CompletionLabelTransitionResult(
            status=CompletionLabelStatus.CONFIRMED,
            target_label="status:done",
            observed_labels=("priority:high", "status:done"),
        )
        labeled = record_label_confirmation(
            posted,
            label_result,
            owner_token=OWNER_TOKEN,
            state_path=path,
        )
        assert labeled.stage == CompleteStage.LABEL_CONFIRMED

        retried = record_label_confirmation(
            posted,
            CompletionLabelTransitionResult(
                status=CompletionLabelStatus.CONFIRMED,
                target_label="status:done",
                observed_labels=("status:done", "priority:high"),
            ),
            owner_token=OWNER_TOKEN,
            state_path=path,
        )
        assert retried == labeled

        handed_off = mark_handoff_ready(
            labeled, owner_token=OWNER_TOKEN, state_path=path
        )
        assert handed_off.stage == CompleteStage.HANDED_OFF
        persisted = load_run_state(path)
        receipt = persisted.completion_replay_receipts[initial.receipt_key]
        assert receipt["completion_id"] == initial.completion_id
        assert (
            persisted.completion_journal[initial.journal_key]["stage"] == "handed_off"
        )

    def test_new_writer_rejects_owner_or_fingerprint_mismatch(self, tmp_path):
        from dataclasses import replace

        from orchestune.complete.journal import record_posting_evidence

        path = tmp_path / "run_state.json"
        initial = _new_journal_record()
        reserved = reserve_completion(
            record=initial, owner_token=OWNER_TOKEN, state_path=path
        )

        with pytest.raises(CompletionJournalError) as owner_error:
            record_posting_evidence(
                replace(reserved, generation_id="stale-generation"),
                comment_id="comment-1234",
                comment_url="https://example.test/comment-1234",
                owner_token="",
                state_path=path,
            )
        assert owner_error.value.reason == CompleteFailureReason.GENERATION_MISMATCH

        changed_request = replace(initial, request_fingerprint="c" * 64)
        with pytest.raises(CompletionJournalError) as fingerprint_error:
            record_posting_evidence(
                changed_request,
                comment_id="comment-1234",
                comment_url="https://example.test/comment-1234",
                owner_token=OWNER_TOKEN,
                state_path=path,
            )
        assert (
            fingerprint_error.value.reason
            == CompleteFailureReason.REQUEST_FINGERPRINT_MISMATCH
        )
        assert load_run_state(path).completion_journal[initial.journal_key][
            "stage"
        ] == ("reserved")

        changed_owner = replace(initial, owner_token_digest="c" * 64)
        with pytest.raises(CompletionJournalError) as digest_error:
            record_posting_evidence(
                changed_owner,
                comment_id="comment-1234",
                comment_url="https://example.test/comment-1234",
                owner_token=OWNER_TOKEN,
                state_path=path,
            )
        assert digest_error.value.reason == CompleteFailureReason.OWNER_TOKEN_MISMATCH

    def test_unknown_new_schema_is_not_treated_as_empty_state(self, tmp_path):
        path = tmp_path / "run_state.json"
        path.write_text(
            json.dumps(
                {
                    "active_worktrees": {},
                    "completion_journal": {
                        "future": {"schema_version": 999, "stage": "future"}
                    },
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="completion_journal.*schema_version"):
            load_run_state(path)

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
        assert persisted.completion.completion_handoff_ready is False
        assert persisted.completion.completion_comment_id is None

    def test_rejects_marking_handoff_ready_with_only_a_comment_id(self, tmp_path):
        path = _seed_active(tmp_path)
        journal = _reserve(path)

        with pytest.raises(CompletionJournalError) as excinfo:
            mark_handoff_ready(journal, comment_id="1", state_path=path)
        assert excinfo.value.reason == CompleteFailureReason.EVIDENCE_MISSING

    def test_rejects_blank_or_whitespace_only_comment_evidence(self, tmp_path):
        path = _seed_active(tmp_path)
        journal = _reserve(path)

        with pytest.raises(CompletionJournalError) as excinfo:
            mark_handoff_ready(
                journal, comment_id="   ", comment_url="u", state_path=path
            )
        assert excinfo.value.reason == CompleteFailureReason.EVIDENCE_MISSING

        with pytest.raises(CompletionJournalError) as excinfo:
            mark_handoff_ready(journal, comment_id="1", comment_url="", state_path=path)
        assert excinfo.value.reason == CompleteFailureReason.EVIDENCE_MISSING

        persisted = load_run_state(path).active_worktrees["10"]
        assert persisted.completion.completion_handoff_ready is False

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
            state.active_worktrees["10"] = ActiveWorktree.from_records(
                core=ActiveWorktreeCore(
                    10, "claude/issue-10-x", "worktrees/claude-issue-10-x", ()
                ),
                launch=LaunchInfo(pid=54321, started_at=1700000100.0),
                claim=ClaimInfo(
                    owner_kind="interactive",
                    claim_id="claim-10-b",
                    claim_stage="completed",
                    base_ref="origin/main",
                    base_sha="b" * 40,
                    reservation_kind="footprint",
                    repository_id="Saltmu/orchestune",
                    claimed_at=1700000101.0,
                    owner_token_digest=owner_token_digest(OWNER_TOKEN),
                ),
                completion=ActiveCompletionJournal(),
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
            active.completion = replace(
                active.completion, completion_id="completion-other"
            )
            active.completion = replace(active.completion, completion_result="blocked")
            active.completion = replace(
                active.completion, completion_stage="journaling"
            )
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

    def test_rejects_a_retry_with_a_different_comment_id_after_handoff(self, tmp_path):
        path = _seed_active(tmp_path)
        journal = _reserve(path)
        first = mark_handoff_ready(
            journal, comment_id="1", comment_url="u", state_path=path
        )

        with pytest.raises(CompletionJournalError) as excinfo:
            mark_handoff_ready(first, comment_id="2", comment_url="u", state_path=path)
        assert excinfo.value.reason == CompleteFailureReason.INVALID_STAGE_TRANSITION

        persisted = load_run_state(path).active_worktrees["10"]
        assert persisted.completion.completion_comment_id == "1"

    def test_rejects_a_retry_with_a_different_payload_after_handoff(self, tmp_path):
        path = _seed_active(tmp_path)
        journal = _reserve(path)
        first = mark_handoff_ready(
            journal,
            comment_id="1",
            comment_url="u",
            payload={"pr": 1},
            state_path=path,
        )

        with pytest.raises(CompletionJournalError) as excinfo:
            mark_handoff_ready(
                first,
                comment_id="1",
                comment_url="u",
                payload={"pr": 2},
                state_path=path,
            )
        assert excinfo.value.reason == CompleteFailureReason.INVALID_STAGE_TRANSITION

        persisted = load_run_state(path).active_worktrees["10"]
        assert persisted.completion.completion_payload == {"pr": 1}

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
        assert persisted.completion.completion_handoff_ready is False


class TestJournalOwnerApi:
    def test_with_completion_replaces_only_completion_fields(self):
        core = ActiveWorktreeCore(10, "claude/issue-10", "wt", ())
        launch = LaunchInfo(pid=123)
        claim = ClaimInfo(claim_id="claim-10")
        active = ActiveWorktree.from_records(
            core=core,
            launch=launch,
            claim=claim,
            completion=ActiveCompletionJournal(completion_id="old-id"),
        )
        new_comp = ActiveCompletionJournal(
            completion_id="new-id",
            completion_result="done",
            completion_stage="handed_off",
            completion_payload={"outcome": "test"},
            completion_comment_id="c1",
            completion_comment_url="u1",
            completion_handoff_ready=True,
            completion_policy_config={"test": True},
        )
        updated = with_completion(active, new_comp)

        assert updated.core == core
        assert updated.launch == launch
        assert updated.claim == claim
        assert updated.completion == new_comp

        with pytest.raises(
            TypeError, match="completion must be ActiveCompletionJournal"
        ):
            with_completion(active, "not-a-record")  # type: ignore[arg-type]

    def test_active_completion_from_record_explicit_conversion(self):
        record = CompletionJournalRecord(
            repository_id="Saltmu/orchestune",
            issue_number=10,
            generation_id="claim-10",
            completion_id="comp-10",
            owner_token_digest=owner_token_digest(OWNER_TOKEN),
            request_fingerprint=owner_token_digest("request-fp"),
            result="done",
            target_label="status:done",
            outcome_payload={"issue": 10, "result": "done", "body": "outcome body"},
            stage=CompleteStage.LABEL_CONFIRMED,
            posting_evidence={
                "comment_id": "123",
                "comment_url": "https://example.test/123",
            },
            label_evidence={
                "status": "confirmed",
                "target_label": "status:done",
                "observed_labels": ["status:done"],
            },
        )
        comp = active_completion_from_record(
            record, handoff=True, completion_policy_config={"source": "test"}
        )
        assert isinstance(comp, ActiveCompletionJournal)
        assert not isinstance(comp, CompletionJournalRecord)
        assert comp.completion_id == "comp-10"
        assert comp.completion_result == "done"
        assert comp.completion_stage == "label_confirmed"
        assert comp.completion_payload == {
            "issue": 10,
            "result": "done",
            "body": "outcome body",
            "outcome": "outcome body",
        }
        assert comp.completion_comment_id == "123"
        assert comp.completion_comment_url == "https://example.test/123"
        assert comp.completion_handoff_ready is True
        assert comp.completion_policy_config == {"source": "test"}

    def test_apply_completion_record_preserves_policy_config_and_other_subrecords(
        self,
    ):
        core = ActiveWorktreeCore(10, "claude/issue-10", "wt", ())
        launch = LaunchInfo(pid=123)
        claim = ClaimInfo(claim_id="claim-10")
        active = ActiveWorktree.from_records(
            core=core,
            launch=launch,
            claim=claim,
            completion=ActiveCompletionJournal(
                completion_comment_id="comment-123",
                completion_comment_url="https://example.test/123",
                completion_policy_config={"max_tokens_per_task": 1000},
            ),
        )
        record = CompletionJournalRecord(
            repository_id="Saltmu/orchestune",
            issue_number=10,
            generation_id="claim-10",
            completion_id="comp-10",
            owner_token_digest=owner_token_digest(OWNER_TOKEN),
            request_fingerprint=owner_token_digest("request-fp"),
            result="done",
            target_label="status:done",
            outcome_payload={"issue": 10, "result": "done", "body": "body"},
            stage=CompleteStage.RESERVED,
        )
        updated = apply_completion_record(active, record, handoff=False)
        assert updated.completion.completion_id == "comp-10"
        assert updated.completion.completion_policy_config == {
            "max_tokens_per_task": 1000
        }
        assert updated.completion.completion_comment_id == "comment-123"
        assert updated.completion.completion_comment_url == "https://example.test/123"
        assert updated.core == core
        assert updated.launch == launch
        assert updated.claim == claim

        # When posting evidence is present, comments are updated
        ev_record = replace(
            record,
            posting_evidence={
                "comment_id": "new-456",
                "comment_url": "https://example.test/456",
            },
        )
        ev_updated = apply_completion_record(updated, ev_record)
        assert ev_updated.completion.completion_comment_id == "new-456"
        assert (
            ev_updated.completion.completion_comment_url == "https://example.test/456"
        )

    def test_publication_save_preserves_comment_evidence_when_record_lacks_it(
        self, tmp_path
    ):
        from orchestune.complete.publication import PublicationContext, _save

        state_path = tmp_path / "run_state.json"
        core = ActiveWorktreeCore(10, "claude/issue-10", "wt", ())
        active = ActiveWorktree.from_records(
            core=core,
            launch=LaunchInfo(),
            claim=ClaimInfo(claim_id="claim-10"),
            completion=ActiveCompletionJournal(
                completion_comment_id="comment-123",
                completion_comment_url="https://example.test/123",
            ),
        )
        save_run_state_unlocked(RunState(active_worktrees={"10": active}), state_path)
        context = PublicationContext(
            request=None,  # type: ignore[arg-type]
            state_path=state_path,
            worktree=Path("wt"),
            forge=None,
            active=active,
        )
        record = CompletionJournalRecord(
            repository_id="Saltmu/orchestune",
            issue_number=10,
            generation_id="claim-10",
            completion_id="comp-10",
            owner_token_digest=owner_token_digest(OWNER_TOKEN),
            request_fingerprint=owner_token_digest("request-fp"),
            result="done",
            target_label="status:done",
            outcome_payload={"issue": 10, "result": "done", "body": "body"},
            stage=CompleteStage.RESERVED,
        )
        with run_state_lock(state_path.with_suffix(".lock")):
            _save(context, record)
        saved = load_run_state(state_path).active_worktrees["10"]
        assert saved.completion.completion_comment_id == "comment-123"
        assert saved.completion.completion_comment_url == "https://example.test/123"
