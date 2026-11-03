"""dispatch_gc内の完了ワークツリー処理（dispatch_gc_completion）テスト。

`tests/test_dispatch_gc.py`の肥大化解消のため分割している（#345）。
Zombie・Timeout回収は`test_dispatch_gc_zombies.py`、gitプリミティブや
`dispatch_gc.py`自身のルール・エンドツーエンド統合テストは
`test_dispatch_gc.py`に残している。
"""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.gc.completion import (
    _apply_early_death_retry,
    _finalize_completed_worktree,
    _finalize_not_needed_worktree,
)
from orchestune.dispatch.targets import (
    ClaudeCodeCloudRoutineDispatchTarget,
)
from orchestune.ledger.run_state import RunState, TaskReclaimRecord
from orchestune.models import IssueRecord
from orchestune.outcome_record import OutcomeRecord
from tests.dispatch_gc_test_support import _active, _task

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-state-"))


class TestFinalizeCompletedWorktree:
    """#74: プロセス終了検知後の完了処理。空コミット完了を実完了と誤判定しないこと。"""

    def test_pending_early_death_retry_reuses_reserved_count(
        self, tmp_path, fake_forge
    ):
        """GitHub反映失敗後の再試行は最後の自動再投入枠を二重消費しない。"""
        active = _active(started_at=100.0)
        record = TaskReclaimRecord(
            early_death_retry_count=2,
            early_death_retry_at=180.0,
            early_death_retry_pending=True,
        )
        run_state = RunState(task_reclaim_counts={280: record})
        config = DispatcherConfig(
            parent_issue_number=100,
            apply=False,
            max_early_death_retries=2,
            early_death_window_seconds=120,
            run_state_path=tmp_path / "state.json",
            events_log_path=tmp_path / "events.jsonl",
            forge=fake_forge,
        )

        event = _apply_early_death_retry(active, _task(), config, run_state, now=110.0)

        assert event is not None
        assert event.retry_at == 180.0
        assert run_state.task_reclaim_counts[280].early_death_retry_count == 2

    def test_no_new_commits_is_not_treated_as_completed(self, tmp_path, fake_forge):
        """#74再現: worktreeはcleanだがbase_branchに対して新規コミットが0件の場合、
        status:doneを付与せず、依存先タスクの誤昇格を防ぐためcompleted以外のアクションにする。"""
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
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

        assert event.to_dict()["action"] != "completed"
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        fake_forge.remove_label.assert_called_once_with(280, "status:in-progress")
        fake_forge.add_label.assert_called_once_with(280, "status:blocked-human-review")
        fake_forge.add_comment.assert_called_once()
        assert fake_forge.add_comment.call_args.args[0] == 280

    def test_new_commits_without_outcome_is_escalated_to_blocked_human_review(
        self, tmp_path, fake_forge
    ):
        """コミットがあってもoutcomeレコードが存在しない場合はcompletedにせずstatus:blocked-human-reviewに倒す。"""
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
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
                return_value=True,
            ),
            patch("orchestune.dispatch.gc.git.subprocess.run") as mock_run,
            patch(
                "orchestune.dispatch.gc.completion.remove_worktree", autospec=True
            ) as mock_remove_worktree,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="deadbeef\n", stderr=""
            )
            event = _finalize_completed_worktree(active, task, config)

        assert event.to_dict()["action"] == "completed_without_outcome"
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        fake_forge.remove_label.assert_called_once_with(280, "status:in-progress")
        fake_forge.add_label.assert_called_once_with(280, "status:blocked-human-review")
        fake_forge.add_comment.assert_called_once()
        assert (
            "完了宣言レコード（orchestune:outcome）が検出できませんでした"
            in fake_forge.add_comment.call_args.args[1]
        )

    def test_new_commits_with_outcome_done_is_treated_as_completed(
        self, tmp_path, fake_forge
    ):
        """base_branchに対する実コミットがありoutcome(done)があればcompleted+status:doneとする。"""
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            forge=fake_forge,
        )
        outcome = OutcomeRecord(result="done", issue=280)
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
        ]
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                autospec=True,
                return_value=True,
            ),
            patch("orchestune.dispatch.gc.git.subprocess.run") as mock_run,
            patch(
                "orchestune.dispatch.gc.completion.remove_worktree", autospec=True
            ) as mock_remove_worktree,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="deadbeef\n", stderr=""
            )
            event = _finalize_completed_worktree(active, task, config)

        assert event.to_dict()["action"] == "completed"
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        fake_forge.remove_label.assert_called_once_with(280, "status:in-progress")
        fake_forge.add_label.assert_called_once_with(280, "status:done")
        assert event.to_dict()["commit_sha"] == "deadbeef"

    def test_new_commits_with_outcome_not_needed_is_routed_to_not_needed(
        self, tmp_path, fake_forge
    ):
        """outcome(not-needed)の場合はnot-needed経路（クローズまたは検証レビュー）へ流す。"""
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        fake_forge.get_issue.return_value = IssueRecord(
            number=280,
            title="task",
            body="",
            labels=("status:not-needed",),
            created_at="",
            state="CLOSED",
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            forge=fake_forge,
        )
        outcome = OutcomeRecord(result="not-needed", issue=280)
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
        ]
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                autospec=True,
                return_value=True,
            ),
            patch("orchestune.dispatch.gc.git.subprocess.run") as mock_run,
            patch(
                "orchestune.dispatch.gc.completion.remove_worktree", autospec=True
            ) as mock_remove_worktree,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="deadbeef\n", stderr=""
            )
            event = _finalize_completed_worktree(active, task, config)

        assert event.to_dict()["action"] == "not_needed"
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        fake_forge.add_label.assert_called_once_with(280, "status:not-needed")
        fake_forge.remove_label.assert_any_call(280, "status:in-progress")
        fake_forge.close_issue.assert_called_once()
        assert fake_forge.close_issue.call_args.args[0] == 280

    def test_outcome_forge_error_skips_completion(self, tmp_path, fake_forge):
        """forgeエラー時は完了と判定せずスキップする（fail-closed）。"""
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            forge=fake_forge,
        )
        fake_forge.list_comments.side_effect = RuntimeError("connection error")
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                autospec=True,
                return_value=True,
            ),
            patch(
                "orchestune.dispatch.gc.completion.remove_worktree", autospec=True
            ) as mock_remove_worktree,
        ):
            event = _finalize_completed_worktree(active, task, config)

        assert event.to_dict()["action"] == "completion_skipped_forge_error"
        mock_remove_worktree.assert_not_called()

    def test_completed_also_removes_stale_queued_label(self, tmp_path, fake_forge):
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:queued", "status:in-progress"))
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            forge=fake_forge,
        )
        outcome = OutcomeRecord(result="done", issue=280)
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
        ]
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                autospec=True,
                return_value=True,
            ),
            patch("orchestune.dispatch.gc.git.subprocess.run") as mock_run,
            patch("orchestune.dispatch.gc.completion.remove_worktree", autospec=True),
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="deadbeef\n", stderr=""
            )
            event = _finalize_completed_worktree(active, task, config)

        assert event.to_dict()["action"] == "completed"
        fake_forge.add_label.assert_called_once_with(280, "status:done")
        fake_forge.remove_label.assert_any_call(280, "status:in-progress")
        fake_forge.remove_label.assert_any_call(280, "status:queued")
        assert fake_forge.remove_label.call_count == 2

    def test_completed_adds_done_before_removing_in_progress(
        self, tmp_path, fake_forge
    ):
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            forge=fake_forge,
        )
        outcome = OutcomeRecord(result="done", issue=280)
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
        ]
        call_order: list[tuple[str, str]] = []
        fake_forge.add_label.side_effect = lambda issue, label: call_order.append(
            ("add", label)
        )
        fake_forge.remove_label.side_effect = lambda issue, label: call_order.append(
            ("remove", label)
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
                return_value=True,
            ),
            patch("orchestune.dispatch.gc.git.subprocess.run") as mock_run,
            patch("orchestune.dispatch.gc.completion.remove_worktree", autospec=True),
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="deadbeef\n", stderr=""
            )
            _finalize_completed_worktree(active, task, config)

        assert call_order == [
            ("add", "status:done"),
            ("remove", "status:in-progress"),
        ]


class TestFinalizeNotNeededWorktree:
    """#280: status:not-neededラベル検知による完全自動クローズ。"""

    def test_apply_removes_worktree_and_closes_issue(self, tmp_path, fake_forge):
        active = _active()
        task = _task()
        fake_forge.get_issue.return_value = IssueRecord(
            number=280,
            title="task",
            body="",
            labels=("status:not-needed",),
            created_at="",
            state="CLOSED",
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
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
                "orchestune.dispatch.gc.completion.remove_worktree", autospec=True
            ) as mock_remove_worktree,
        ):
            event = _finalize_not_needed_worktree(active, task, config)

        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        fake_forge.add_label.assert_called_once_with(280, "status:not-needed")
        fake_forge.remove_label.assert_any_call(280, "status:in-progress")
        fake_forge.close_issue.assert_called_once()
        fake_forge.get_issue.assert_called_once_with(280)
        calls = [call[0] for call in fake_forge.mock_calls]
        assert calls.index("add_label") < calls.index("remove_label")
        assert calls.index("remove_label") < calls.index("close_issue")
        assert calls.index("close_issue") < calls.index("get_issue")
        close_args = fake_forge.close_issue.call_args.args
        assert close_args[0] == 280
        assert close_args[1] == "not planned"
        assert event.to_dict() == {
            "issue_number": 280,
            "worktree_path": "worktrees/w1",
            "action": "not_needed",
            "subtask_id": "task-a",
        }

    def test_label_add_failure_keeps_old_status_and_does_not_close(
        self, tmp_path, fake_forge
    ):
        active = _active()
        task = _task()
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            forge=fake_forge,
        )
        fake_forge.add_label.side_effect = RuntimeError("forge unavailable")
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.remove_worktree",
            ),
            pytest.raises(RuntimeError, match="forge unavailable"),
        ):
            _finalize_not_needed_worktree(active, task, config)

        fake_forge.remove_label.assert_not_called()
        fake_forge.close_issue.assert_not_called()

    def test_dirty_worktree_is_not_closed(self, tmp_path, fake_forge):
        """未コミットの作業が残っている場合は、安全側に倒しクローズを見送る。"""
        active = _active()
        task = _task()
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            forge=fake_forge,
        )
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=True,
            ),
            patch(
                "orchestune.dispatch.gc.completion.remove_worktree", autospec=True
            ) as mock_remove_worktree,
        ):
            event = _finalize_not_needed_worktree(active, task, config)

        mock_remove_worktree.assert_not_called()
        fake_forge.remove_label.assert_not_called()
        fake_forge.close_issue.assert_not_called()
        assert event.to_dict()["action"] == "completion_skipped_dirty_worktree"

    def test_dry_run_does_not_call_github_or_mutate(self, tmp_path, fake_forge):
        active = _active()
        task = _task()
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            apply=False,
            forge=fake_forge,
        )
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.remove_worktree", autospec=True
            ) as mock_remove_worktree,
        ):
            event = _finalize_not_needed_worktree(active, task, config)

        mock_remove_worktree.assert_not_called()
        fake_forge.remove_label.assert_not_called()
        fake_forge.close_issue.assert_not_called()
        assert event.to_dict()["action"] == "not_needed"

    def test_none_task_defaults_subtask_id_to_empty_string(self, tmp_path, fake_forge):
        active = _active()
        fake_forge.get_issue.return_value = IssueRecord(
            number=280,
            title="task",
            body="",
            labels=("status:not-needed",),
            created_at="",
            state="CLOSED",
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            forge=fake_forge,
        )
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=False,
            ),
            patch("orchestune.dispatch.gc.completion.remove_worktree", autospec=True),
        ):
            event = _finalize_not_needed_worktree(active, None, config)
        assert event.to_dict()["subtask_id"] == ""


class TestFinalizeNotNeededWorktreeCloudRoutineReview:
    """#282: クラウドルーチン利用可能時は即時クローズせず独立検証レビューへ委譲する。"""

    def _cloud_config(self, tmp_path, **overrides):
        defaults = dict(
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            dispatch_target=ClaudeCodeCloudRoutineDispatchTarget("rid", "rtok"),
        )
        defaults.update(overrides)
        return DispatcherConfig(parent_issue_number=100, **defaults)

    def test_dispatches_review_instead_of_closing(self, tmp_path, fake_forge):
        active = _active()
        task = _task()
        config = self._cloud_config(
            tmp_path,
            not_needed_review_state_path=tmp_path / "state.json",
            forge=fake_forge,
        )
        dispatch_review = MagicMock()
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.remove_worktree", autospec=True
            ) as mock_remove_worktree,
        ):
            event = _finalize_not_needed_worktree(
                active,
                task,
                config,
                dispatch_not_needed_review=dispatch_review,
            )

        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        fake_forge.remove_label.assert_called_once_with(280, "status:in-progress")
        fake_forge.close_issue.assert_not_called()
        dispatch_review.assert_called_once_with(280, "task-a", config)
        assert event.to_dict()["action"] == "not_needed_review_dispatched"
        assert event.to_dict()["subtask_id"] == "task-a"

    def test_dirty_worktree_does_not_dispatch_review(self, tmp_path, fake_forge):
        active = _active()
        task = _task()
        config = self._cloud_config(
            tmp_path,
            not_needed_review_state_path=tmp_path / "state.json",
            forge=fake_forge,
        )
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=True,
            ),
            patch(
                "orchestune.integrator.coordinator.ClaudeCodeCloudRoutineDispatchTarget.fire_text",
                autospec=True,
            ) as mock_fire_text,
        ):
            event = _finalize_not_needed_worktree(active, task, config)

        mock_fire_text.assert_not_called()
        assert event.to_dict()["action"] == "completion_skipped_dirty_worktree"


class TestFinalizeBaseBranchRedWorktree:
    def test_base_branch_red_attempt_1_blocks_and_adds_marker(self, tmp_path):
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        outcome = OutcomeRecord(
            result="blocked",
            issue=280,
            reason="base-branch-red",
            base_sha="abc1234",
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
            event = _finalize_completed_worktree(active, task, config)

        assert event.to_dict()["action"] == "blocked_base_branch_red"
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        fake_forge.add_label.assert_any_call(280, "status:blocked")
        fake_forge.add_label.assert_any_call(280, "ci:base-branch-red")
        fake_forge.remove_label.assert_called_once_with(280, "status:in-progress")
        fake_forge.add_comment.assert_called_once()

    def test_base_branch_red_attempt_3_escalates_to_human_review(self, tmp_path):
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        outcome = OutcomeRecord(
            result="blocked",
            issue=280,
            reason="base-branch-red",
            base_sha="abc1234",
            attempt=3,
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

        assert event.to_dict()["action"] == "escalated_base_branch_red"
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        fake_forge.add_label.assert_called_once_with(280, "status:blocked-human-review")
        fake_forge.remove_label.assert_any_call(280, "status:in-progress")
        fake_forge.add_comment.assert_called_once()
