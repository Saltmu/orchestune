"""Tests for review-timeout and unknown reason handling in GC completion."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.gc.completion import _finalize_completed_worktree
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import ActiveWorktree, RunState, TaskReclaimRecord
from orchestune.outcome_record import OutcomeRecord, ReviewSummary


def _active(**overrides):
    defaults = dict(
        issue_number=280,
        branch="claude/issue-280-task-a",
        worktree_path="worktrees/w1",
        pid=111,
        started_at=1_699_999_000.0,
        declared_footprint=("src/foo.py",),
    )
    defaults.update(overrides)
    return ActiveWorktree(**defaults)


def _task(**overrides):
    defaults = dict(
        issue_number=280,
        subtask_id="task-a",
        footprint=("src/foo.py",),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:in-progress",),
        created_at="2026-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return Task(**defaults)


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
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.remove_worktree"
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
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.remove_worktree"
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
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            forge=fake_forge,
        )
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.remove_worktree"
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
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.remove_worktree"
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
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.remove_worktree"
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
