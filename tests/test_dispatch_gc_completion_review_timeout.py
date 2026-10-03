"""Tests for review-timeout and unknown reason handling in GC completion."""

from __future__ import annotations

import copy
from unittest.mock import MagicMock, patch

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.gc.completion import _finalize_completed_worktree
from orchestune.ledger.run_state import RunState, TaskReclaimRecord
from orchestune.outcome_record import OutcomeRecord, ReviewSummary
from tests.dispatch_gc_test_support import _active
from tests.dispatch_gc_test_support import _in_progress_task as _task


class TestDispatchGcCompletionReviewTimeout:
    def test_review_timeout_requeued_with_backoff(self, tmp_path):
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        outcome = OutcomeRecord(
            result="blocked",
            issue=280,
            pr=456,
            reason="review-timeout",
            review=ReviewSummary(bot="claude", rounds=1, verdict="timeout"),
            attempt=1,
        )
        fake_forge = MagicMock()
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:10Z"}
        ]
        fake_forge.list_prs.return_value = []
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            forge=fake_forge,
            max_review_timeout_retries=2,
            review_timeout_backoff_seconds=60,
        )
        run_state = RunState()
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.remove_worktree", autospec=True
            ) as mock_remove_worktree,
        ):
            event = _finalize_completed_worktree(
                active, task, config, run_state=run_state, now=1000.0
            )

        assert event["action"] in ("blocked_review_timeout", "review_timeout_requeued")
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        fake_forge.add_label.assert_any_call(280, "status:queued")
        fake_forge.remove_label.assert_called_once_with(280, "status:in-progress")
        fake_forge.add_comment.assert_called_once()
        record = run_state.task_reclaim_counts[280]
        assert record.review_timeout_retry_count == 1
        assert record.review_timeout_retry_at == 1060.0
        assert record.review_timeout_retry_pending is True

    def test_review_timeout_escalates_to_human_review_at_max_retries(self, tmp_path):
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        outcome = OutcomeRecord(
            result="blocked",
            issue=280,
            pr=456,
            reason="review-timeout",
            review=ReviewSummary(bot="claude", rounds=1, verdict="timeout"),
            attempt=2,
        )
        fake_forge = MagicMock()
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:10Z"}
        ]
        fake_forge.list_prs.return_value = []
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            forge=fake_forge,
            max_review_timeout_retries=2,
        )
        run_state = RunState(
            task_reclaim_counts={
                280: TaskReclaimRecord(
                    review_timeout_retry_count=1,
                    review_timeout_retry_at=1000.0,
                    review_timeout_retry_pending=False,
                )
            }
        )
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.remove_worktree", autospec=True
            ) as mock_remove_worktree,
        ):
            event = _finalize_completed_worktree(
                active, task, config, run_state=run_state, now=1100.0
            )

        assert event["action"] == "escalated_review_timeout"
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        fake_forge.add_label.assert_called_once_with(280, "status:blocked-human-review")
        fake_forge.remove_label.assert_any_call(280, "status:in-progress")
        call_comment = fake_forge.add_comment.call_args[0][1]
        assert "GitHub Actions" in call_comment or "actor" in call_comment

    def test_unknown_reason_blocked_without_escalation(self, tmp_path):
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        outcome = OutcomeRecord(
            result="blocked",
            issue=280,
            reason="some-custom-reason",
        )
        fake_forge = MagicMock()
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:10Z"}
        ]
        fake_forge.list_prs.return_value = []
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            forge=fake_forge,
        )
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.remove_worktree", autospec=True
            ) as mock_remove_worktree,
        ):
            event = _finalize_completed_worktree(active, task, config)

        assert event["action"] == "blocked_unknown_reason"
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        fake_forge.add_label.assert_called_once_with(280, "status:blocked")
        fake_forge.remove_label.assert_called_once_with(280, "status:in-progress")
        fake_forge.add_comment.assert_called_once()

    def test_review_timeout_ignores_worker_self_reported_attempt(self, tmp_path):
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        outcome = OutcomeRecord(
            result="blocked",
            issue=280,
            pr=456,
            reason="review-timeout",
            review=ReviewSummary(bot="claude", rounds=1, verdict="timeout"),
            attempt=99,
        )
        fake_forge = MagicMock()
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:10Z"}
        ]
        fake_forge.list_prs.return_value = []
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            forge=fake_forge,
            max_review_timeout_retries=2,
            review_timeout_backoff_seconds=60,
        )
        run_state = RunState()
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.remove_worktree", autospec=True
            ) as mock_remove_worktree,
        ):
            event = _finalize_completed_worktree(
                active, task, config, run_state=run_state, now=1000.0
            )

        assert event["action"] == "blocked_review_timeout"
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        fake_forge.add_label.assert_any_call(280, "status:queued")
        record = run_state.task_reclaim_counts[280]
        assert record.review_timeout_retry_count == 1

    def test_review_timeout_pending_retry_reuses_reserved_count(self, tmp_path):
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        outcome = OutcomeRecord(
            result="blocked",
            issue=280,
            pr=456,
            reason="review-timeout",
            review=ReviewSummary(bot="claude", rounds=1, verdict="timeout"),
            attempt=1,
        )
        fake_forge = MagicMock()
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:10Z"}
        ]
        fake_forge.list_prs.return_value = []
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            forge=fake_forge,
            max_review_timeout_retries=2,
            review_timeout_backoff_seconds=60,
        )
        run_state = RunState(
            task_reclaim_counts={
                280: TaskReclaimRecord(
                    review_timeout_retry_count=1,
                    review_timeout_retry_at=1060.0,
                    review_timeout_retry_pending=True,
                )
            }
        )
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.remove_worktree", autospec=True
            ) as mock_remove_worktree,
        ):
            event = _finalize_completed_worktree(
                active, task, config, run_state=run_state, now=1000.0
            )

        assert event["action"] == "blocked_review_timeout"
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        record = run_state.task_reclaim_counts[280]
        assert record.review_timeout_retry_count == 1
        assert record.review_timeout_retry_at == 1060.0

    def test_review_timeout_without_run_state_holds_as_blocked(self, tmp_path):
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        outcome = OutcomeRecord(
            result="blocked",
            issue=280,
            pr=456,
            reason="review-timeout",
            review=ReviewSummary(bot="claude", rounds=1, verdict="timeout"),
            attempt=1,
        )
        fake_forge = MagicMock()
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:10Z"}
        ]
        fake_forge.list_prs.return_value = []
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            forge=fake_forge,
        )
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.remove_worktree", autospec=True
            ) as mock_remove_worktree,
        ):
            event = _finalize_completed_worktree(active, task, config, run_state=None)

        assert event["action"] == "blocked_review_timeout"
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        fake_forge.add_label.assert_called_once_with(280, "status:blocked")
        fake_forge.remove_label.assert_called_once_with(280, "status:in-progress")
        fake_forge.add_comment.assert_called_once()


def _timeout_outcome() -> OutcomeRecord:
    return OutcomeRecord(
        result="blocked",
        issue=280,
        pr=456,
        reason="review-timeout",
        review=ReviewSummary(bot="claude", rounds=1, verdict="timeout"),
        attempt=1,
    )


def _legacy_route(tmp_path, max_attempts, backoff, record, now):
    """Run the legacy GC path; return its decision as (kind, count, retry_at)."""
    forge = MagicMock()
    forge.list_comments.return_value = [
        {"body": _timeout_outcome().render(), "created_at": "2026-01-01T00:00:10Z"}
    ]
    forge.list_prs.return_value = []
    config = DispatcherConfig(
        parent_issue_number=100,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "run_state.json",
        worktree_root=tmp_path / "worktrees",
        apply=True,
        forge=forge,
        max_review_timeout_retries=max_attempts,
        review_timeout_backoff_seconds=backoff,
    )
    run_state = RunState(task_reclaim_counts={280: record} if record else {})
    with (
        patch(
            "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
            autospec=True,
            return_value=False,
        ),
        patch(
            "orchestune.dispatch.gc.completion.worktree_has_new_commits",
            autospec=True,
            return_value=False,
        ),
        patch("orchestune.dispatch.gc.completion.remove_worktree", autospec=True),
    ):
        event = _finalize_completed_worktree(
            _active(base_branch="origin/main"),
            _task(status_labels=("status:in-progress",)),
            config,
            run_state=run_state,
            now=now,
        )
    saved = run_state.task_reclaim_counts.get(280)
    count = saved.review_timeout_retry_count if saved else 0
    if event["action"] == "escalated_review_timeout":
        return "escalate", count, None
    return "requeue", count, saved.review_timeout_retry_at


def _journal_route(tmp_path, max_attempts, backoff, record, now):
    """Run the journal path's reservation; return the same decision shape."""
    from orchestune.dispatch.gc.policies import _reserve_retry

    config = DispatcherConfig(
        parent_issue_number=100,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "run_state.json",
        worktree_root=tmp_path / "worktrees",
        max_review_timeout_retries=max_attempts,
        review_timeout_backoff_seconds=backoff,
        forge=MagicMock(),
    )
    state = RunState(task_reclaim_counts={280: record} if record else {})
    metadata = _reserve_retry(state, config, 280, now)
    if metadata["target_label"] == "status:blocked-human-review":
        return "escalate", metadata["retry_count"], None
    return "requeue", metadata["retry_count"], metadata["retry_at"]


def _record(count, at=0.0, pending=False):
    return TaskReclaimRecord(
        review_timeout_retry_count=count,
        review_timeout_retry_at=at,
        review_timeout_retry_pending=pending,
    )


class TestRetryDecisionIsSharedByBothRoutes:
    """#1189: the legacy GC path and the journal path reach the same decision."""

    @pytest.mark.parametrize(
        ("max_attempts", "backoff", "record", "expected"),
        [
            (2, 60, None, ("requeue", 1, 1060.0)),
            (2, 60, _record(1), ("escalate", 1, None)),
            (2, 60, _record(1, at=500.0, pending=True), ("requeue", 1, 500.0)),
            (1, 60, None, ("escalate", 0, None)),
            (0, 60, None, ("escalate", 0, None)),
            (3, 60, _record(1), ("requeue", 2, 1120.0)),
            (3, 30, _record(2), ("escalate", 2, None)),
            (2, 60, _record(5, at=77.0, pending=True), ("requeue", 5, 77.0)),
        ],
    )
    def test_same_policy_state_and_time_give_the_same_decision(
        self, tmp_path, max_attempts, backoff, record, expected
    ):
        legacy = _legacy_route(
            tmp_path,
            max_attempts,
            backoff,
            copy.deepcopy(record) if record else None,
            1000.0,
        )
        journal = _journal_route(
            tmp_path,
            max_attempts,
            backoff,
            copy.deepcopy(record) if record else None,
            1000.0,
        )
        assert legacy == expected
        assert journal == expected

    def test_a_reserved_retry_is_not_counted_twice_after_restart(self, tmp_path):
        reserved = _record(1, at=1060.0, pending=True)
        first = _journal_route(tmp_path, 2, 60, copy.deepcopy(reserved), 1000.0)
        later = _journal_route(tmp_path, 2, 60, copy.deepcopy(reserved), 5000.0)
        legacy = _legacy_route(tmp_path, 2, 60, copy.deepcopy(reserved), 5000.0)
        assert first == later == legacy == ("requeue", 1, 1060.0)
