import subprocess
from unittest.mock import MagicMock, patch

import pytest

from orchestune.dispatch_scoring import Task
from orchestune.dispatch_targets import (
    DispatchHandle,
    LocalProcessDispatchTarget,
    default_dry_run_command_builder,
)
from orchestune.dispatch_worktree import (
    _branch_exists,
    create_worktree_and_launch,
    file_lock,
)


def _task(
    issue_number,
    priority="medium",
    risk=False,
    progress_partial=False,
    created_at="2023-01-01T00:00:00+00:00",
    footprint=("src/foo.py",),
    depends_on=(),
):
    return Task(
        issue_number=issue_number,
        subtask_id=f"task-{issue_number}",
        footprint=footprint,
        symbols=(),
        risk=risk,
        priority=priority,
        progress_partial=progress_partial,
        status_labels=("status:queued",),
        created_at=created_at,
        depends_on=depends_on,
    )


class TestCreateWorktreeAndLaunch:
    def test_dry_run_does_not_call_subprocess(self, tmp_path):
        task = _task(1)
        dispatch_target = LocalProcessDispatchTarget(
            default_dry_run_command_builder, log_dir=tmp_path / "logs"
        )
        with (
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
        ):
            result = create_worktree_and_launch(
                task,
                branch_name="claude/issue-1-task-1",
                worktree_root=tmp_path / "worktrees",
                dispatch_target=dispatch_target,
                apply=False,
            )
        mock_run.assert_not_called()
        mock_popen.assert_not_called()
        assert result.launched is False

    def test_apply_creates_worktree_and_launches_process(self, tmp_path):
        task = _task(1)
        dispatch_target = LocalProcessDispatchTarget(
            default_dry_run_command_builder, log_dir=tmp_path / "logs"
        )
        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            mock_popen.return_value.pid = 4242
            result = create_worktree_and_launch(
                task,
                branch_name="claude/issue-1-task-1",
                worktree_root=tmp_path / "worktrees",
                dispatch_target=dispatch_target,
                apply=True,
            )
        assert mock_run.called
        assert mock_popen.called
        assert result.launched is True
        assert result.pid == 4242

    def test_rejects_invalid_branch_name(self, tmp_path):
        task = _task(1)
        dispatch_target = LocalProcessDispatchTarget(
            default_dry_run_command_builder, log_dir=tmp_path / "logs"
        )
        result = create_worktree_and_launch(
            task,
            branch_name="--upload-pack=evil",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=dispatch_target,
            apply=True,
        )
        assert result.launched is False
        assert (
            "無効な" in result.error_message
            or "Invalid" in result.error_message
            or "ブランチ名" in result.error_message
        )

    def test_apply_failure_returns_launched_false_with_error(self, tmp_path):
        task = _task(1)
        dispatch_target = LocalProcessDispatchTarget(
            default_dry_run_command_builder, log_dir=tmp_path / "logs"
        )
        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
        ):
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=128,
                cmd="git worktree add",
                stderr="fatal: branch 'claude/issue-1-task-1' already exists",
            )
            result = create_worktree_and_launch(
                task,
                branch_name="claude/issue-1-task-1",
                worktree_root=tmp_path / "worktrees",
                dispatch_target=dispatch_target,
                apply=True,
            )
        assert result.launched is False
        assert "fatal: branch" in result.error_message
        mock_popen.assert_not_called()

    def test_apply_uses_dispatch_target_and_captures_external_handle(self, tmp_path):
        """#215: 差し替えたDispatchTargetのlaunch()結果がLaunchResultへ反映される。"""
        task = _task(1)
        fake_target = MagicMock()
        fake_target.launch.return_value = DispatchHandle(
            external_id="session_1",
            external_url="https://claude.ai/code/session_1",
            branch_name="claude/issue-1-task-1",
        )
        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            result = create_worktree_and_launch(
                task,
                branch_name="claude/issue-1-task-1",
                worktree_root=tmp_path / "worktrees",
                dispatch_target=fake_target,
                apply=True,
            )
        assert fake_target.launch.called
        assert result.launched is True
        assert result.pid is None
        assert result.external_id == "session_1"
        assert result.external_url == "https://claude.ai/code/session_1"

    @patch("orchestune.dispatch_worktree._branch_exists", return_value=True)
    def test_apply_reuses_existing_branch_without_overwriting(
        self, mock_exists, tmp_path
    ):
        task = _task(1)
        dispatch_target = LocalProcessDispatchTarget(
            default_dry_run_command_builder, log_dir=tmp_path / "logs"
        )
        with (
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            mock_popen.return_value.pid = 4242
            result = create_worktree_and_launch(
                task,
                branch_name="claude/issue-1-task-1",
                worktree_root=tmp_path / "worktrees",
                dispatch_target=dispatch_target,
                apply=True,
            )
        assert result.launched is True
        # git worktree add に -B や -b が含まれていない（既存ブランチのチェックアウト）ことを確認
        worktree_add_call = mock_run.call_args_list[1]
        args = worktree_add_call.args[0]
        assert "add" in args
        assert "-B" not in args
        assert "-b" not in args
        assert "claude/issue-1-task-1" in args

    def test_dirty_existing_worktree_is_backed_up_before_recreation(self, tmp_path):
        """#213: 既存worktreeがdirtyな場合、削除前にWIPコミットとして退避する。"""
        task = _task(1)
        dispatch_target = LocalProcessDispatchTarget(
            default_dry_run_command_builder, log_dir=tmp_path / "logs"
        )
        worktree_path = tmp_path / "worktrees" / "claude-issue-1-task-1"
        worktree_path.mkdir(parents=True)

        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch(
                "orchestune.dispatch_worktree.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_worktree.dispatch_gc.backup_wip_commit",
                return_value=None,
            ) as mock_backup,
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            mock_popen.return_value.pid = 4242
            result = create_worktree_and_launch(
                task,
                branch_name="claude/issue-1-task-1",
                worktree_root=tmp_path / "worktrees",
                dispatch_target=dispatch_target,
                apply=True,
            )
        mock_backup.assert_called_once_with(
            worktree_path, "WIP: backup by Orchestune before worktree recreation"
        )
        assert result.launched is True
        # 退避成功後は従来通り削除され、mockされた`git worktree add`で再作成される
        # （実プロセスは起動しないため物理ディレクトリは残らない）
        assert not worktree_path.exists()
        worktree_add_calls = [c for c in mock_run.call_args_list if "add" in c.args[0]]
        assert worktree_add_calls

    def test_backup_failure_aborts_launch_without_deleting_worktree(self, tmp_path):
        """#213: WIP退避自体が失敗した場合、削除も再作成もせず起動を失敗させる。"""
        task = _task(1)
        dispatch_target = LocalProcessDispatchTarget(
            default_dry_run_command_builder, log_dir=tmp_path / "logs"
        )
        worktree_path = tmp_path / "worktrees" / "claude-issue-1-task-1"
        worktree_path.mkdir(parents=True)
        marker = worktree_path / "uncommitted.txt"
        marker.write_text("agent work in progress")

        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch(
                "orchestune.dispatch_worktree.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_worktree.dispatch_gc.backup_wip_commit",
                return_value="fatal: unable to write new index file",
            ),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
        ):
            result = create_worktree_and_launch(
                task,
                branch_name="claude/issue-1-task-1",
                worktree_root=tmp_path / "worktrees",
                dispatch_target=dispatch_target,
                apply=True,
            )

        assert result.launched is False
        assert "fatal: unable to write new index file" in result.error_message
        # git worktree prune 以降（add等の再作成コマンド）は一切実行されない
        worktree_add_calls = [c for c in mock_run.call_args_list if "add" in c.args[0]]
        assert not worktree_add_calls
        mock_popen.assert_not_called()
        assert marker.exists()  # 未コミット作業が残ったworktreeは削除されていない


class TestBranchExists:
    @patch("orchestune.dispatch_worktree.subprocess.run")
    def test_branch_exists_local(self, mock_run):
        # 1回目の subprocess.run が returncode=0 を返せば True
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        assert _branch_exists("my-branch") is True
        mock_run.assert_called_once()

    @patch("orchestune.dispatch_worktree.subprocess.run")
    def test_branch_exists_remote(self, mock_run):
        # 1回目が returncode=1（ローカル存在せず）、2回目が returncode=0（リモート存在）
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=1),
            subprocess.CompletedProcess(args=[], returncode=0),
        ]
        assert _branch_exists("my-branch") is True
        assert mock_run.call_count == 2

    @patch("orchestune.dispatch_worktree.subprocess.run")
    def test_branch_does_not_exist(self, mock_run):
        # 1回目も2回目も returncode=1（存在せず）
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=1),
            subprocess.CompletedProcess(args=[], returncode=1),
        ]
        assert _branch_exists("my-branch") is False
        assert mock_run.call_count == 2


class TestFileLock:
    def test_file_lock_propagates_exception_raised_inside_body(self, tmp_path):
        """#227: dispatch cycle本体（`with file_lock(...):`のbody）で発生した例外は、
        ロック機構によってマスクされず、元の例外のまま呼び出し元に伝播しなければならない。
        GitHub Actions実行時、`gh issue edit --add-label`のCalledProcessErrorが
        `RuntimeError: generator didn't stop after throw()`に化けてしまう回帰を防ぐ。"""
        lock_path = tmp_path / "test.lock"

        with pytest.raises(ValueError, match="boom"):
            with file_lock(lock_path):
                raise ValueError("boom")

    def test_file_lock_raises_error_when_lock_acquisition_fails(self, tmp_path):
        """ロック取得（mkdir/open/flock）自体が失敗した場合は、例外を発生させて終了する。"""
        unwritable_dir = tmp_path / "no_such_parent"
        lock_path = unwritable_dir / "test.lock"

        with patch("pathlib.Path.mkdir", side_effect=OSError("boom-mkdir")):
            executed = False
            with pytest.raises(OSError, match="boom-mkdir"):
                with file_lock(lock_path):
                    executed = True
            assert not executed

    def test_file_lock_raises_error_when_fcntl_is_none(self, tmp_path):
        """fcntlがNone（非Linux環境）の場合、RuntimeErrorを発生させて終了する。"""
        lock_path = tmp_path / "test.lock"
        with patch("orchestune.dispatch_worktree.fcntl", None):
            executed = False
            with pytest.raises(RuntimeError, match="fcntl is not supported"):
                with file_lock(lock_path):
                    executed = True
            assert not executed
