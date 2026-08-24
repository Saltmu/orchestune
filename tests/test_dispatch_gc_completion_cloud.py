"""dispatch_gc内の完了ワークツリー処理（dispatch_gc_completion）テスト。

`tests/test_dispatch_gc.py`の肥大化解消のため分割している（#345）。
Zombie・Timeout回収は`test_dispatch_gc_zombies.py`、gitプリミティブや
`dispatch_gc.py`自身のルール・エンドツーエンド統合テストは
`test_dispatch_gc.py`に残している。
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.gc.completion import (
    _finalize_abandoned_cloud_worktree,
    _local_pr_completion_status,
)
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import ActiveWorktree, RunState, TaskReclaimRecord
from orchestune.models import PrRecord
from orchestune.outcome_record import OutcomeRecord

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-state-"))


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
        status_labels=("status:not-needed",),
        created_at="2026-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return Task(**defaults)


class TestFinalizeAbandonedCloudWorktree:
    """#381レビュー対応(Codex P2): PRがマージされずクローズされた際の
    再キューイングが、中断した以前の遷移で取り残された一次status:*ラベルを
    正しく後始末することを検証する。"""

    def test_removes_stale_blocked_label_alongside_in_progress(
        self, tmp_path, fake_forge
    ):
        # stacked launch中断で取り残されたstatus:blockedも併せて除去し、
        # status:queuedへ確実に収束させなければならない。
        active = _active()
        task = _task(status_labels=("status:blocked", "status:in-progress"))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl", apply=True, forge=fake_forge
        )

        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch.gc.completion.remove_worktree"),
        ):
            event = _finalize_abandoned_cloud_worktree(active, task, config)

        assert event["action"] == "abandoned_pr_requeued"
        fake_forge.add_label.assert_called_once_with(280, "status:queued")
        fake_forge.remove_label.assert_any_call(280, "status:in-progress")
        fake_forge.remove_label.assert_any_call(280, "status:blocked")
        assert fake_forge.remove_label.call_count == 2

    def test_does_not_overwrite_terminal_escalation_label(self, tmp_path, fake_forge):
        # 中断した以前の遷移でstatus:blocked-human-reviewが既に付与されている
        # 場合、status:queuedへの書き換えは人間の確認要求を握りつぶして
        # しまうため、ラベルには一切触れてはならない。
        active = _active()
        task = _task(
            status_labels=("status:blocked-human-review", "status:in-progress")
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl", apply=True, forge=fake_forge
        )

        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch.gc.completion.remove_worktree"),
        ):
            event = _finalize_abandoned_cloud_worktree(active, task, config)

        assert event["action"] == "abandoned_pr_requeued"
        fake_forge.add_label.assert_not_called()
        fake_forge.remove_label.assert_not_called()
        fake_forge.add_comment.assert_called_once()

    def test_increments_task_reclaim_counts_and_requeues_within_limit(
        self, tmp_path, fake_forge
    ):
        active = _active()
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            max_task_reclaims=3,
            forge=fake_forge,
        )
        run_state = RunState(
            active_worktrees={"w1": active},
            task_reclaim_counts={
                280: TaskReclaimRecord(count=1, last_reclaimed_at=100.0)
            },
        )

        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch.gc.completion.remove_worktree"),
        ):
            event = _finalize_abandoned_cloud_worktree(
                active, task, config, run_state=run_state
            )

        assert event["action"] == "abandoned_pr_requeued"
        assert run_state.task_reclaim_counts[280].count == 2
        fake_forge.add_label.assert_called_once_with(280, "status:queued")
        fake_forge.remove_label.assert_called_once_with(280, "status:in-progress")
        comment = fake_forge.add_comment.call_args.args[1]
        assert "回収2回目 / 上限3回" in comment

    def test_escalates_to_human_review_when_max_task_reclaims_exceeded(
        self, tmp_path, fake_forge
    ):
        active = _active()
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            max_task_reclaims=2,
            forge=fake_forge,
        )
        run_state = RunState(
            active_worktrees={"w1": active},
            task_reclaim_counts={
                280: TaskReclaimRecord(count=2, last_reclaimed_at=100.0)
            },
        )

        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch.gc.completion.remove_worktree"),
        ):
            event = _finalize_abandoned_cloud_worktree(
                active, task, config, run_state=run_state
            )

        assert event["action"] == "escalated_reclaim_limit_exceeded"
        assert run_state.task_reclaim_counts[280].count == 3
        fake_forge.add_label.assert_called_once_with(280, "status:blocked-human-review")
        fake_forge.remove_label.assert_called_once_with(280, "status:in-progress")
        comment = fake_forge.add_comment.call_args.args[1]
        assert "上限（max_task_reclaims=2）を超えた（今回で3回目）" in comment

    def test_escalation_triggers_on_label_applied_callback(self, tmp_path, fake_forge):
        active = _active()
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            max_task_reclaims=1,
            forge=fake_forge,
        )
        run_state = RunState(
            active_worktrees={"w1": active},
            task_reclaim_counts={
                280: TaskReclaimRecord(count=1, last_reclaimed_at=100.0)
            },
        )
        callback_called = False

        def _on_label():
            nonlocal callback_called
            callback_called = True

        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch.gc.completion.remove_worktree"),
        ):
            event = _finalize_abandoned_cloud_worktree(
                active,
                task,
                config,
                run_state=run_state,
                on_label_applied=_on_label,
            )

        assert event["action"] == "escalated_reclaim_limit_exceeded"
        assert callback_called is True

    def test_requeue_triggers_on_label_applied_callback_and_tolerates_comment_failure(
        self, tmp_path, fake_forge
    ):
        active = _active()
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            max_task_reclaims=3,
            forge=fake_forge,
        )
        run_state = RunState(
            active_worktrees={"w1": active},
            task_reclaim_counts={
                280: TaskReclaimRecord(count=1, last_reclaimed_at=100.0)
            },
        )
        callback_called = False

        def _on_label():
            nonlocal callback_called
            callback_called = True

        fake_forge.add_comment.side_effect = RuntimeError("Comment API timeout")

        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch.gc.completion.remove_worktree"),
        ):
            event = _finalize_abandoned_cloud_worktree(
                active,
                task,
                config,
                run_state=run_state,
                on_label_applied=_on_label,
            )

        assert event["action"] == "abandoned_pr_requeued"
        assert callback_called is True
        assert run_state.task_reclaim_counts[280].count == 2

    def test_reclaim_triggers_on_reclaim_reserved_before_label_change(
        self, tmp_path, fake_forge
    ):
        active = _active()
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            max_task_reclaims=3,
            forge=fake_forge,
        )
        run_state = RunState(
            active_worktrees={"w1": active},
            task_reclaim_counts={
                280: TaskReclaimRecord(count=1, last_reclaimed_at=100.0)
            },
        )
        order: list[str] = []

        def _on_reserved():
            order.append("reserved")

        def _on_label():
            order.append("label_applied")

        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch.gc.completion.remove_worktree"),
        ):
            event = _finalize_abandoned_cloud_worktree(
                active,
                task,
                config,
                run_state=run_state,
                on_label_applied=_on_label,
                on_reclaim_reserved=_on_reserved,
            )

        assert event["action"] == "abandoned_pr_requeued"
        assert order == ["reserved", "label_applied"]
        assert run_state.task_reclaim_counts[280].pending is False

    def test_reclaim_reuses_existing_pending_reservation_on_retry(
        self, tmp_path, fake_forge
    ):
        active = _active()
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            max_task_reclaims=3,
            forge=fake_forge,
        )
        run_state = RunState(
            active_worktrees={"w1": active},
            task_reclaim_counts={
                280: TaskReclaimRecord(count=2, last_reclaimed_at=100.0, pending=True)
            },
        )

        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch.gc.completion.remove_worktree"),
        ):
            event = _finalize_abandoned_cloud_worktree(
                active,
                task,
                config,
                run_state=run_state,
            )

        assert event["action"] == "abandoned_pr_requeued"
        # Should stay 2, not increment to 3
        assert run_state.task_reclaim_counts[280].count == 2
        assert run_state.task_reclaim_counts[280].pending is False

    def test_reservation_failure_rolls_back_and_raises(self, tmp_path, fake_forge):
        import pytest

        active = _active()
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            max_task_reclaims=3,
            forge=fake_forge,
        )
        run_state = RunState(
            active_worktrees={"w1": active},
            task_reclaim_counts={
                280: TaskReclaimRecord(count=1, last_reclaimed_at=100.0, pending=False)
            },
        )

        def _fail_reserved():
            raise OSError("Disk full")

        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch.gc.completion.remove_worktree") as mock_rm,
        ):
            with pytest.raises(OSError, match="Disk full"):
                _finalize_abandoned_cloud_worktree(
                    active,
                    task,
                    config,
                    run_state=run_state,
                    on_reclaim_reserved=_fail_reserved,
                )

        assert run_state.task_reclaim_counts[280].count == 1
        assert run_state.task_reclaim_counts[280].pending is False
        mock_rm.assert_not_called()
        fake_forge.add_label.assert_not_called()

    def test_reclaim_settles_even_if_old_label_removal_fails(
        self, tmp_path, fake_forge
    ):
        import pytest

        active = _active()
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            max_task_reclaims=3,
            forge=fake_forge,
        )
        run_state = RunState(
            active_worktrees={"w1": active},
            task_reclaim_counts={
                280: TaskReclaimRecord(count=1, last_reclaimed_at=100.0, pending=False)
            },
        )
        settled = False

        def _on_label():
            nonlocal settled
            settled = True

        fake_forge.remove_label.side_effect = RuntimeError("Failed to remove old label")

        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch.gc.completion.remove_worktree"),
        ):
            with pytest.raises(RuntimeError, match="Failed to remove old label"):
                _finalize_abandoned_cloud_worktree(
                    active,
                    task,
                    config,
                    run_state=run_state,
                    on_label_applied=_on_label,
                )

        assert settled is True
        assert run_state.task_reclaim_counts[280].pending is False
        assert run_state.task_reclaim_counts[280].count == 2

    def test_stale_active_entry_discard_settles_pending_reservation(self, tmp_path):
        from orchestune.dispatch.gc import _apply_stale_active_entry_discard

        active = _active()
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl", apply=True, max_task_reclaims=3
        )
        run_state = RunState(
            active_worktrees={"w1": active},
            task_reclaim_counts={
                280: TaskReclaimRecord(count=2, last_reclaimed_at=100.0, pending=True)
            },
        )

        with (
            patch("os.path.exists", return_value=False),
            patch("orchestune.dispatch.gc.remove_worktree"),
        ):
            discarded = _apply_stale_active_entry_discard(
                run_state,
                "w1",
                active,
                "issue label is no longer status:in-progress",
                config,
            )

        assert discarded is True
        assert "w1" not in run_state.active_worktrees
        assert run_state.task_reclaim_counts[280].pending is False
        assert run_state.task_reclaim_counts[280].count == 2


class TestLocalPrCompletionStatusWithFakeForge:
    """#292: `mock.patch`によるグローバルなクラスメソッド差し替えではなく、
    `DispatcherConfig(forge=...)`への注入だけでテストが書けることを示す。"""

    def test_uses_injected_fake_forge_instead_of_patching(self, tmp_path):
        fake_forge = MagicMock()
        fake_forge.list_prs.return_value = [
            PrRecord(
                number=1,
                head_ref="claude/issue-1-task-1",
                changed_files=(),
                state="MERGED",
            )
        ]
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
        )
        active = ActiveWorktree(
            issue_number=1,
            branch="claude/issue-1-task-1",
            worktree_path=str(tmp_path / "worktrees/claude-issue-1-task-1"),
            pid=123,
            started_at=1700000000.0,
            declared_footprint=(),
        )

        status = _local_pr_completion_status(active, config)

        assert status == "completed"
        fake_forge.list_prs.assert_called_once_with(state="all")

    def test_open_pr_without_outcome_is_pending(self, tmp_path):
        fake_forge = MagicMock()
        fake_forge.list_prs.return_value = [
            PrRecord(
                number=1,
                head_ref="claude/issue-1-task-1",
                changed_files=(),
                state="OPEN",
            )
        ]
        fake_forge.list_comments.return_value = []
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
        )
        active = ActiveWorktree(
            issue_number=1,
            branch="claude/issue-1-task-1",
            worktree_path=str(tmp_path / "worktrees/claude-issue-1-task-1"),
            pid=123,
            started_at=1700000000.0,
            declared_footprint=(),
        )

        status = _local_pr_completion_status(active, config)

        assert status == "pending"

    def test_open_pr_with_outcome_done_is_completed(self, tmp_path):
        fake_forge = MagicMock()
        outcome = OutcomeRecord(result="done", issue=1, pr=1)
        fake_forge.list_prs.return_value = [
            PrRecord(
                number=1,
                head_ref="claude/issue-1-task-1",
                changed_files=(),
                state="OPEN",
            )
        ]
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
        ]
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
        )
        active = ActiveWorktree(
            issue_number=1,
            branch="claude/issue-1-task-1",
            worktree_path=str(tmp_path / "worktrees/claude-issue-1-task-1"),
            pid=123,
            started_at=1700000000.0,
            declared_footprint=(),
        )

        status = _local_pr_completion_status(active, config)

        assert status == "completed"

    def test_open_pr_with_forge_error_is_unknown(self, tmp_path):
        fake_forge = MagicMock()
        fake_forge.list_prs.return_value = [
            PrRecord(
                number=1,
                head_ref="claude/issue-1-task-1",
                changed_files=(),
                state="OPEN",
            )
        ]
        fake_forge.list_comments.side_effect = RuntimeError("network error")
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
        )
        active = ActiveWorktree(
            issue_number=1,
            branch="claude/issue-1-task-1",
            worktree_path=str(tmp_path / "worktrees/claude-issue-1-task-1"),
            pid=123,
            started_at=1700000000.0,
            declared_footprint=(),
        )

        status = _local_pr_completion_status(active, config)

        assert status == "unknown"

    def test_open_pr_with_blocked_outcome_is_completed(self, tmp_path):
        fake_forge = MagicMock()
        fake_forge.list_prs.return_value = [
            PrRecord(
                number=1,
                head_ref="claude/issue-1-task-1",
                changed_files=(),
                state="OPEN",
            )
        ]
        outcome = OutcomeRecord(
            result="blocked",
            issue=1,
            reason="base-branch-red",
            base_sha="abc1234",
            attempt=1,
        )
        fake_forge.list_comments.return_value = [
            {
                "body": outcome.render(),
                "created_at": "2026-01-01T00:00:10Z",
            }
        ]
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
        )
        active = ActiveWorktree(
            issue_number=1,
            branch="claude/issue-1-task-1",
            worktree_path=str(tmp_path / "worktrees/claude-issue-1-task-1"),
            pid=123,
            started_at=1700000000.0,
            declared_footprint=(),
        )

        status = _local_pr_completion_status(active, config)

        assert status == "completed"
