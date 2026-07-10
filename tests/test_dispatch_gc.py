import subprocess
from unittest.mock import patch

from orchestune.dispatch_gc import (
    _finalize_completed_worktree,
    _finalize_not_needed_worktree,
    is_process_alive,
    remove_worktree,
    worktree_has_new_commits,
    worktree_has_uncommitted_changes,
)
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree
from orchestune.dispatch_targets import (
    ClaudeCodeCloudRoutineDispatchTarget,
    DispatchHandle,
)
from orchestune.dispatcher import DispatcherConfig


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


class TestIsProcessAlive:
    """#193: pidのプロセス生存確認による完了判定。"""

    def test_none_pid_is_not_alive(self):
        assert is_process_alive(None) is False

    def test_alive_pid_returns_true(self):
        with patch("orchestune.dispatch_gc.os.kill") as mock_kill:
            mock_kill.return_value = None
            assert is_process_alive(12345) is True

    def test_missing_pid_returns_false(self):
        with patch("orchestune.dispatch_gc.os.kill", side_effect=ProcessLookupError):
            assert is_process_alive(12345) is False

    def test_permission_error_is_treated_as_alive(self):
        with patch("orchestune.dispatch_gc.os.kill", side_effect=PermissionError):
            assert is_process_alive(1) is True


class TestWorktreeHasUncommittedChanges:
    """#193: worktree削除前の未コミット変更確認（安全側フォールバック）。"""

    def test_clean_worktree_returns_false(self):
        with patch("orchestune.dispatch_gc.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            assert worktree_has_uncommitted_changes("worktrees/w1") is False

    def test_dirty_worktree_returns_true(self):
        with patch("orchestune.dispatch_gc.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=" M src/foo.py\n", stderr=""
            )
            assert worktree_has_uncommitted_changes("worktrees/w1") is True

    def test_git_error_defaults_to_clean(self):
        """存在しないworktreeなどgit statusが失敗する場合はクオータ解放を優先し、
        削除を妨げないようクリーン扱いとする。"""
        with patch(
            "orchestune.dispatch_gc.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, []),
        ):
            assert worktree_has_uncommitted_changes("worktrees/missing") is False


class TestWorktreeHasNewCommits:
    """#74: base_branchに対する実コミットの有無確認（空コミット完了の誤判定防止）。"""

    def test_returns_true_when_commits_ahead(self):
        with patch("orchestune.dispatch_gc.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="2\n", stderr=""
            )
            assert worktree_has_new_commits("worktrees/w1", "origin/main") is True

    def test_returns_false_when_no_commits_ahead(self):
        with patch("orchestune.dispatch_gc.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="0\n", stderr=""
            )
            assert worktree_has_new_commits("worktrees/w1", "origin/main") is False

    def test_git_error_falls_back_to_true(self):
        """比較不能時は安全側（実コミットありとみなし従来通り完了扱い）にフォールバックする。"""
        with patch(
            "orchestune.dispatch_gc.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, []),
        ):
            assert worktree_has_new_commits("worktrees/missing", "origin/main") is True


class TestFinalizeCompletedWorktree:
    """#74: プロセス終了検知後の完了処理。空コミット完了を実完了と誤判定しないこと。"""

    def test_no_new_commits_is_not_treated_as_completed(self):
        """#74再現: worktreeはcleanだがbase_branchに対して新規コミットが0件の場合、
        status:doneを付与せず、依存先タスクの誤昇格を防ぐためcompleted以外のアクションにする。"""
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(apply=True)
        with (
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc.worktree_has_new_commits",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_worktree,
            patch("orchestune.dispatch_gc.github.add_label") as mock_add_label,
            patch("orchestune.dispatch_gc.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_gc.github.add_comment") as mock_add_comment,
        ):
            event = _finalize_completed_worktree(active, task, config)

        assert event["action"] != "completed"
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        mock_remove_label.assert_called_once_with(280, "status:in-progress")
        mock_add_label.assert_called_once_with(280, "status:blocked-human-review")
        mock_add_comment.assert_called_once()
        assert mock_add_comment.call_args.args[0] == 280

    def test_new_commits_present_is_treated_as_completed(self):
        """base_branchに対する実コミットがあれば従来通りcompleted+status:doneとする。"""
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(apply=True)
        with (
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc.worktree_has_new_commits",
                return_value=True,
            ),
            patch("orchestune.dispatch_gc.subprocess.run") as mock_run,
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_worktree,
            patch("orchestune.dispatch_gc.github.add_label") as mock_add_label,
            patch("orchestune.dispatch_gc.github.remove_label") as mock_remove_label,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="deadbeef\n", stderr=""
            )
            event = _finalize_completed_worktree(active, task, config)

        assert event["action"] == "completed"
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        mock_remove_label.assert_called_once_with(280, "status:in-progress")
        mock_add_label.assert_called_once_with(280, "status:done")
        assert event["commit_sha"] == "deadbeef"


class TestRemoveWorktree:
    """#193: 完了したworktreeの削除。"""

    def test_calls_git_worktree_remove_without_force(self):
        with patch("orchestune.dispatch_gc.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            remove_worktree("worktrees/w1")
        args = mock_run.call_args.args[0]
        assert args == ["git", "worktree", "remove", "worktrees/w1"]
        assert "--force" not in args

    def test_swallows_error_when_already_removed(self):
        with patch(
            "orchestune.dispatch_gc.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, []),
        ):
            remove_worktree("worktrees/already-gone")  # 例外を送出しないこと


class TestFinalizeNotNeededWorktree:
    """#280: status:not-neededラベル検知による完全自動クローズ。"""

    def test_apply_removes_worktree_and_closes_issue(self):
        active = _active()
        task = _task()
        config = DispatcherConfig(apply=True)
        with (
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_worktree,
            patch("orchestune.dispatch_gc.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_gc.github.close_issue") as mock_close_issue,
        ):
            event = _finalize_not_needed_worktree(active, task, config)

        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        mock_remove_label.assert_called_once_with(280, "status:in-progress")
        mock_close_issue.assert_called_once()
        close_args = mock_close_issue.call_args.args
        assert close_args[0] == 280
        assert close_args[1] == "not planned"
        assert event == {
            "issue_number": 280,
            "worktree_path": "worktrees/w1",
            "action": "not_needed",
            "subtask_id": "task-a",
        }

    def test_dirty_worktree_is_not_closed(self):
        """未コミットの作業が残っている場合は、安全側に倒しクローズを見送る。"""
        active = _active()
        task = _task()
        config = DispatcherConfig(apply=True)
        with (
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_worktree,
            patch("orchestune.dispatch_gc.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_gc.github.close_issue") as mock_close_issue,
        ):
            event = _finalize_not_needed_worktree(active, task, config)

        mock_remove_worktree.assert_not_called()
        mock_remove_label.assert_not_called()
        mock_close_issue.assert_not_called()
        assert event["action"] == "completion_skipped_dirty_worktree"

    def test_dry_run_does_not_call_github_or_mutate(self):
        active = _active()
        task = _task()
        config = DispatcherConfig(apply=False)
        with (
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_worktree,
            patch("orchestune.dispatch_gc.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_gc.github.close_issue") as mock_close_issue,
        ):
            event = _finalize_not_needed_worktree(active, task, config)

        mock_remove_worktree.assert_not_called()
        mock_remove_label.assert_not_called()
        mock_close_issue.assert_not_called()
        assert event["action"] == "not_needed"

    def test_none_task_defaults_subtask_id_to_empty_string(self):
        active = _active()
        config = DispatcherConfig(apply=True)
        with (
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc.remove_worktree"),
            patch("orchestune.dispatch_gc.github.remove_label"),
            patch("orchestune.dispatch_gc.github.close_issue"),
        ):
            event = _finalize_not_needed_worktree(active, None, config)
        assert event["subtask_id"] == ""


class TestFinalizeNotNeededWorktreeCloudRoutineReview:
    """#282: クラウドルーチン利用可能時は即時クローズせず独立検証レビューへ委譲する。"""

    def _cloud_config(self, **overrides):
        defaults = dict(
            apply=True,
            dispatch_target=ClaudeCodeCloudRoutineDispatchTarget("rid", "rtok"),
        )
        defaults.update(overrides)
        return DispatcherConfig(**defaults)

    def test_dispatches_review_instead_of_closing(self, tmp_path):
        active = _active()
        task = _task()
        config = self._cloud_config(
            not_needed_review_state_path=tmp_path / "state.json"
        )
        handle = DispatchHandle(
            external_id="sess-1", external_url="https://claude.ai/code/s/sess-1"
        )
        with (
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_worktree,
            patch("orchestune.dispatch_gc.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_gc.github.close_issue") as mock_close_issue,
            patch(
                "orchestune.integration_coordinator.ClaudeCodeCloudRoutineDispatchTarget.fire_text",
                return_value=handle,
            ) as mock_fire_text,
        ):
            event = _finalize_not_needed_worktree(active, task, config)

        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        mock_remove_label.assert_called_once_with(280, "status:in-progress")
        mock_close_issue.assert_not_called()
        mock_fire_text.assert_called_once()
        assert "#280" in mock_fire_text.call_args.args[0]
        assert event["action"] == "not_needed_review_dispatched"
        assert event["subtask_id"] == "task-a"

        from orchestune.not_needed_review_state import load_not_needed_review_state

        state = load_not_needed_review_state(config.not_needed_review_state_path)
        assert len(state.pending) == 1
        assert state.pending[0].issue_number == 280
        assert state.pending[0].subtask_id == "task-a"
        assert state.pending[0].session_external_id == "sess-1"

    def test_dirty_worktree_does_not_dispatch_review(self, tmp_path):
        active = _active()
        task = _task()
        config = self._cloud_config(
            not_needed_review_state_path=tmp_path / "state.json"
        )
        with (
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch(
                "orchestune.integration_coordinator.ClaudeCodeCloudRoutineDispatchTarget.fire_text"
            ) as mock_fire_text,
        ):
            event = _finalize_not_needed_worktree(active, task, config)

        mock_fire_text.assert_not_called()
        assert event["action"] == "completion_skipped_dirty_worktree"
