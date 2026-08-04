"""dispatch_rebase内の実際のgit rebase実行（apply層）テスト。

`tests/test_dispatch_rebase.py`の肥大化解消のため分割している（#347）。
純粋なリベース判定ルール（decide層）は`test_dispatch_rebase_rules.py`、
通知処理・エンドツーエンド統合テストは`test_dispatch_rebase.py`に残している。
"""

import tempfile
from pathlib import Path
from unittest.mock import call, patch

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree, RunState

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-state-"))


def _task(**overrides):
    defaults = dict(
        issue_number=1,
        subtask_id="task-a",
        footprint=("src/foo.py",),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:in-progress",),
        created_at="2026-01-01T00:00:00+00:00",
        depends_on=(),
    )
    defaults.update(overrides)
    return Task(**defaults)


def _active(**overrides):
    defaults = dict(
        issue_number=1,
        branch="claude/issue-1-task-a",
        worktree_path="worktrees/w1",
        pid=111,
        started_at=1_699_999_000.0,
        declared_footprint=("src/foo.py",),
    )
    defaults.update(overrides)
    return ActiveWorktree(**defaults)


class TestWaitForProcessTerminate:
    """#274レビュー対応(P1): is_process_alive経由でポーリングする(os.killは直接呼ばない)。"""

    @patch("orchestune.dispatch_rebase.is_process_alive")
    @patch("orchestune.dispatch_rebase.time.sleep")
    def test_wait_immediate_exit(self, mock_sleep, mock_is_alive):
        mock_is_alive.return_value = False

        from orchestune.dispatch_rebase import _wait_for_process_terminate

        _wait_for_process_terminate(12345, timeout=1.0)

        mock_is_alive.assert_called_once_with(12345)
        mock_sleep.assert_not_called()

    @patch("orchestune.dispatch_rebase.is_process_alive")
    @patch("orchestune.dispatch_rebase.time.sleep")
    def test_wait_exit_after_polling(self, mock_sleep, mock_is_alive):
        # 1, 2回目は生存、3回目に非生存で終了
        mock_is_alive.side_effect = [True, True, False]

        from orchestune.dispatch_rebase import _wait_for_process_terminate

        _wait_for_process_terminate(12345, timeout=1.0)

        assert mock_is_alive.call_count == 3
        mock_is_alive.assert_has_calls([call(12345), call(12345), call(12345)])
        assert mock_sleep.call_count == 2
        mock_sleep.assert_has_calls([call(0.1), call(0.1)])

    @patch("orchestune.dispatch_rebase.is_process_alive")
    @patch("orchestune.dispatch_rebase.time.sleep")
    def test_wait_timeout(self, mock_sleep, mock_is_alive):
        # ずっとプロセスが存在している場合、タイムアウト時間経過で抜ける
        mock_is_alive.return_value = True

        from orchestune.dispatch_rebase import _wait_for_process_terminate

        with patch("orchestune.dispatch_rebase.time.time") as mock_time:
            # startの取得時で 0.0、その後のループ条件評価で 0.0, 0.05, 0.11
            mock_time.side_effect = [0.0, 0.0, 0.05, 0.11]
            _wait_for_process_terminate(12345, timeout=0.1)

        assert mock_is_alive.call_count >= 1


class TestApplyAutoRebase:
    @patch("orchestune.dispatch_rebase.os.kill")
    @patch("orchestune.dispatch_rebase.subprocess.run")
    @patch(
        "orchestune.dispatch_rebase.resolve_local_or_remote_branch", return_value="main"
    )
    def test_updates_base_branch_on_success(self, mock_resolve, mock_run, mock_kill):
        from orchestune.dispatch_rebase import _apply_auto_rebase

        active = _active(base_branch="origin/main")
        task = _task()
        run_state = RunState(active_worktrees={"1": active})

        # mock CI pass
        mock_run.return_value.returncode = 0

        # mock launch target
        from unittest.mock import MagicMock

        mock_target = MagicMock()
        mock_target.launch.return_value = MagicMock(
            pid=222, external_id="ext-1", external_url="url-1"
        )
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=mock_target,
            apply=True,
        )

        _apply_auto_rebase(active, task, "1", run_state, "parent-branch", config)

        # Assert base_branch updated to parent-branch
        assert active.base_branch == "parent-branch"
        assert active.pid == 222

    @patch("orchestune.dispatch_rebase.os.kill")
    @patch("orchestune.dispatch_rebase.subprocess.run")
    @patch(
        "orchestune.dispatch_rebase.resolve_local_or_remote_branch", return_value="main"
    )
    def test_keeps_original_base_branch_on_failure(
        self, mock_resolve, mock_run, mock_kill
    ):
        import subprocess

        from orchestune.dispatch_rebase import _apply_auto_rebase

        active = _active(base_branch="origin/main")
        task = _task()
        run_state = RunState(active_worktrees={"1": active})

        # mock rebase fail (subprocess.CalledProcessError)
        mock_run.side_effect = subprocess.CalledProcessError(1, ["git", "rebase"])

        from unittest.mock import MagicMock

        mock_target = MagicMock()
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=mock_target,
            apply=True,
        )

        with (
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch("orchestune.forge.GitHubForge.add_label"),
            patch("orchestune.forge.GitHubForge.add_comment"),
        ):
            _apply_auto_rebase(active, task, "1", run_state, "parent-branch", config)

        # Assert base_branch is still origin/main (not updated)
        assert active.base_branch == "origin/main"

    @patch("orchestune.dispatch_rebase.os.kill")
    @patch("orchestune.dispatch_rebase.dispatch_gc.backup_wip_commit")
    @patch("orchestune.dispatch_rebase.subprocess.run")
    @patch(
        "orchestune.dispatch_rebase.resolve_local_or_remote_branch", return_value="main"
    )
    def test_backs_up_wip_before_rebase_when_dirty(
        self, mock_resolve, mock_run, mock_backup, mock_kill
    ):
        """#213: dirtyなworktreeでは、rebaseを試みる前にWIP退避が呼ばれること。"""
        from orchestune.dispatch_rebase import _apply_auto_rebase

        active = _active(base_branch="origin/main")
        task = _task()
        run_state = RunState(active_worktrees={"1": active})

        mock_backup.return_value = None  # 退避成功（またはclean）
        mock_run.return_value.returncode = 0

        from unittest.mock import MagicMock

        mock_target = MagicMock()
        mock_target.launch.return_value = MagicMock(
            pid=222, external_id="ext-1", external_url="url-1"
        )
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=mock_target,
            apply=True,
        )

        _apply_auto_rebase(active, task, "1", run_state, "parent-branch", config)

        mock_backup.assert_called_once_with(
            active.worktree_path, "WIP: backup by Orchestune auto-rebase"
        )
        # rebaseコマンドが実行されている（退避成功時は通常フローを継続する）
        rebase_calls = [
            call_args
            for call_args in mock_run.call_args_list
            if "rebase" in call_args.args[0]
        ]
        assert rebase_calls
        assert active.base_branch == "parent-branch"

    @patch("orchestune.dispatch_rebase.os.kill")
    @patch("orchestune.dispatch_rebase.dispatch_gc.backup_wip_commit")
    @patch("orchestune.dispatch_rebase.subprocess.run")
    def test_backup_failure_skips_rebase_and_escalates_to_manual_merge(
        self, mock_run, mock_backup, mock_kill
    ):
        """#213: WIP退避自体が失敗した場合、rebaseを試みずmanual-merge-requiredへ
        エスカレーションし、未コミット作業の消失を防ぐ。"""
        from orchestune.dispatch_rebase import _apply_auto_rebase

        active = _active(base_branch="origin/main")
        task = _task()
        run_state = RunState(active_worktrees={"1": active})

        mock_backup.return_value = "fatal: unable to write new index file"

        from unittest.mock import MagicMock

        mock_target = MagicMock()
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=mock_target,
            apply=True,
        )

        with (
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_comment,
        ):
            _apply_auto_rebase(active, task, "1", run_state, "parent-branch", config)

        mock_backup.assert_called_once_with(
            active.worktree_path, "WIP: backup by Orchestune auto-rebase"
        )
        mock_run.assert_not_called()  # rebaseは一切試みられない
        mock_remove.assert_called_once_with(active.issue_number, "status:in-progress")
        mock_add_label.assert_called_once_with(
            active.issue_number, "status:manual-merge-required"
        )
        mock_comment.assert_called_once()
        assert (
            "WIPバックアップコミットの作成に失敗しました"
            in mock_comment.call_args.args[1]
        )
        assert "1" not in run_state.active_worktrees
        assert active.base_branch == "origin/main"
