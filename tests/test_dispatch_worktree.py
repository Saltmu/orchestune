import subprocess
from unittest.mock import MagicMock, patch

import pytest

from orchestune.dispatch.scoring import Task
from orchestune.dispatch.targets import (
    BranchReachabilityError,
    DispatchHandle,
    LocalProcessDispatchTarget,
    default_dry_run_command_builder,
)
from orchestune.dispatch.worktree import (
    _branch_exists,
    _cleanup_existing_worktree,
    _cleanup_failed_worktree,
    _create_worktree,
    _provision_and_launch,
    _resolve_worktree_path,
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
            patch("orchestune.dispatch.worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch.targets.subprocess.Popen") as mock_popen,
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
            patch("orchestune.dispatch.worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch.worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch.targets.subprocess.Popen") as mock_popen,
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

    def test_dispatch_started_at_is_captured_immediately_before_dispatch_launch(
        self, tmp_path
    ):
        """#262レビュー対応 Reproducer: `dispatch_started_at`はworktreeの
        prune/backup/add完了後、`dispatch_target.launch()`呼び出し直前の
        時刻でなければならない。より早い時刻（例: 関数冒頭）を使うと、
        worktree準備中に作成された無関係な既存PRを新sessionの成果物と
        誤認する窓が生まれる。"""
        task = _task(1)
        fake_target = MagicMock()
        fake_target.launch.return_value = DispatchHandle(
            branch_name="claude/issue-1-task-1"
        )
        dispatch_boundary_time = 12345.0
        with (
            patch("orchestune.dispatch.worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch.worktree.subprocess.run") as mock_run,
            patch(
                "orchestune.dispatch.worktree.time.time",
                return_value=dispatch_boundary_time,
            ),
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
        assert result.launched is True
        assert result.dispatch_started_at == dispatch_boundary_time

    def test_dispatch_started_at_is_none_when_launch_never_reached(self, tmp_path):
        """worktree準備自体が失敗しdispatch_target.launch()に到達しなかった
        場合、dispatch_started_atはNoneのままとなる。"""
        task = _task(1)
        dispatch_target = LocalProcessDispatchTarget(
            default_dry_run_command_builder, log_dir=tmp_path / "logs"
        )
        with (
            patch("orchestune.dispatch.worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch.worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch.targets.subprocess.Popen") as mock_popen,
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
        assert result.dispatch_started_at is None
        mock_popen.assert_not_called()

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
            patch("orchestune.dispatch.worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch.worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch.targets.subprocess.Popen") as mock_popen,
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
            patch("orchestune.dispatch.worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch.worktree.subprocess.run") as mock_run,
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

    def test_branch_reachability_error_from_dispatch_target_fails_launch(
        self, tmp_path
    ):
        """#244/#260: cloud-routine起動前のリモートブランチ到達性検証失敗
        （BranchReachabilityError）は、通常の起動失敗としてstatus:blocked経路に
        乗せる。汎用RuntimeErrorは捕捉対象に含めない（#260レビュー対応）。"""
        task = _task(1)
        fake_target = MagicMock()
        fake_target.launch.side_effect = BranchReachabilityError(
            "リモートブランチ 'claude/issue-1-task-1' の到達性を検証できませんでした"
        )
        with (
            patch("orchestune.dispatch.worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch.worktree.subprocess.run") as mock_run,
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
        assert result.launched is False
        assert "到達性を検証できませんでした" in result.error_message

    @patch("orchestune.dispatch.worktree._branch_exists", return_value=True)
    def test_apply_reuses_existing_branch_without_overwriting(
        self, mock_exists, tmp_path
    ):
        task = _task(1)
        dispatch_target = LocalProcessDispatchTarget(
            default_dry_run_command_builder, log_dir=tmp_path / "logs"
        )
        with (
            patch("orchestune.dispatch.worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch.targets.subprocess.Popen") as mock_popen,
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
            patch("orchestune.dispatch.worktree._branch_exists", return_value=False),
            patch(
                "orchestune.dispatch.worktree.dispatch_gc.backup_wip_commit",
                return_value=None,
            ) as mock_backup,
            patch("orchestune.dispatch.worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch.targets.subprocess.Popen") as mock_popen,
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
            patch("orchestune.dispatch.worktree._branch_exists", return_value=False),
            patch(
                "orchestune.dispatch.worktree.dispatch_gc.backup_wip_commit",
                return_value="fatal: unable to write new index file",
            ),
            patch("orchestune.dispatch.worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch.targets.subprocess.Popen") as mock_popen,
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

    def test_dirty_status_check_failure_is_fail_closed_and_aborts_launch(
        self, tmp_path
    ):
        """#213: `git status`自体が失敗し安全性が確認できない場合も、
        cleanと誤判定してrmtreeへ進まない（fail-closed）ことを直接
        `subprocess.run`のエラーを通じて検証する。"""
        task = _task(1)
        dispatch_target = LocalProcessDispatchTarget(
            default_dry_run_command_builder, log_dir=tmp_path / "logs"
        )
        worktree_path = tmp_path / "worktrees" / "claude-issue-1-task-1"
        worktree_path.mkdir(parents=True)
        marker = worktree_path / "uncommitted.txt"
        marker.write_text("agent work in progress")

        with (
            patch("orchestune.dispatch.worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch.worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch.targets.subprocess.Popen") as mock_popen,
        ):

            def run_mock(args, **kwargs):
                if "status" in args:
                    raise subprocess.CalledProcessError(
                        128, args, stderr="fatal: not a git repository"
                    )
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="")

            mock_run.side_effect = run_mock
            result = create_worktree_and_launch(
                task,
                branch_name="claude/issue-1-task-1",
                worktree_root=tmp_path / "worktrees",
                dispatch_target=dispatch_target,
                apply=True,
            )

        assert result.launched is False
        assert "fatal: not a git repository" in result.error_message
        worktree_add_calls = [c for c in mock_run.call_args_list if "add" in c.args[0]]
        assert not worktree_add_calls
        mock_popen.assert_not_called()
        assert marker.exists()  # dirty確認に失敗したworktreeは削除されていない
        mock_popen.assert_not_called()
        assert marker.exists()  # 未コミット作業が残ったworktreeは削除されていない

    def test_retry_after_launch_failure_with_real_git_repo(self, tmp_path, monkeypatch):
        """#248: launchが失敗した後に同じtask/branchで再試行した際、Git metadataが原因で
        worktree再作成が失敗しないことを実際のGitリポジトリで検証する。"""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo_dir,
            check=True,
        )
        (repo_dir / "README.md").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True
        )

        monkeypatch.chdir(repo_dir)

        task = _task(248)
        branch_name = "claude/issue-248-task-248"
        worktree_root = tmp_path / "worktrees"

        failing_target = MagicMock()
        failing_target.launch.side_effect = OSError("launch failed")

        # 1回目の実行（launch失敗）
        res1 = create_worktree_and_launch(
            task,
            branch_name=branch_name,
            worktree_root=worktree_root,
            dispatch_target=failing_target,
            apply=True,
        )
        assert res1.launched is False
        assert "launch failed" in res1.error_message

        # 2回目の実行（正常なDispatchTargetでの再試行）
        success_target = MagicMock()
        success_target.launch.return_value = DispatchHandle(
            external_id="session_248",
            external_url="https://example.com",
            branch_name=branch_name,
        )

        res2 = create_worktree_and_launch(
            task,
            branch_name=branch_name,
            worktree_root=worktree_root,
            dispatch_target=success_target,
            apply=True,
        )

        assert res2.launched is True
        assert res2.error_message is None

    def test_launch_failure_removes_created_worktree(self, tmp_path):
        """#248: git worktree add 成功後に launch() が失敗した場合、補償処理として
        作成済み worktree が Git 管理および物理パスから解除・削除されることを検証する。"""
        task = _task(248)
        failing_target = MagicMock()
        failing_target.launch.side_effect = OSError("launch failed")

        with (
            patch("orchestune.dispatch.worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch.worktree.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            result = create_worktree_and_launch(
                task,
                branch_name="claude/issue-248-task-248",
                worktree_root=tmp_path / "worktrees",
                dispatch_target=failing_target,
                apply=True,
            )

        assert result.launched is False
        assert "launch failed" in result.error_message

        # git worktree remove --force が補償処理として呼ばれたことを確認
        remove_calls = [
            call for call in mock_run.call_args_list if "remove" in call.args[0]
        ]
        assert len(remove_calls) >= 1
        assert "--force" in remove_calls[0].args[0]


class TestBranchExists:
    @patch("orchestune.dispatch.worktree.subprocess.run")
    def test_branch_exists_local(self, mock_run):
        # 1回目の subprocess.run が returncode=0 を返せば True
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        assert _branch_exists("my-branch") is True
        mock_run.assert_called_once()

    @patch("orchestune.dispatch.worktree.subprocess.run")
    def test_branch_exists_remote(self, mock_run):
        # 1回目が returncode=1（ローカル存在せず）、2回目が returncode=0（リモート存在）
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=1),
            subprocess.CompletedProcess(args=[], returncode=0),
        ]
        assert _branch_exists("my-branch") is True
        assert mock_run.call_count == 2

    @patch("orchestune.dispatch.worktree.subprocess.run")
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

    def test_file_lock_raises_error_when_fcntl_and_msvcrt_are_none(self, tmp_path):
        """fcntlおよびmsvcrtの両方がNoneの場合、RuntimeErrorを発生させて終了する。"""
        lock_path = tmp_path / "test.lock"
        with (
            patch("orchestune.infra.process_utils.fcntl", None),
            patch("orchestune.infra.process_utils.msvcrt", None),
        ):
            executed = False
            with pytest.raises(
                RuntimeError, match="Neither fcntl nor msvcrt is supported"
            ):
                with file_lock(lock_path):
                    executed = True
            assert not executed

    def test_file_lock_supports_msvcrt_when_fcntl_is_none(self, tmp_path):
        """fcntlがNoneでmsvcrtが存在する場合（Windows環境）、msvcrt経由でロック・競合検出・解放を行える。"""
        lock_path = tmp_path / "test.lock"
        mock_msvcrt = MagicMock()
        with (
            patch("orchestune.infra.process_utils.fcntl", None),
            patch("orchestune.infra.process_utils.msvcrt", mock_msvcrt),
        ):
            executed = False
            with file_lock(lock_path):
                executed = True
            assert executed
            assert mock_msvcrt.locking.call_count == 2
            # 第一引数fileno, 第二引数mode (NBLCK then UNLCK), 第三引数bytes (1)
            first_call = mock_msvcrt.locking.call_args_list[0]
            second_call = mock_msvcrt.locking.call_args_list[1]
            assert first_call[0][1] == mock_msvcrt.LK_NBLCK
            assert second_call[0][1] == mock_msvcrt.LK_UNLCK

    def test_file_lock_msvcrt_conflict_raises_runtime_error(self, tmp_path):
        """msvcrt使用時にPermissionError（ロック競合）が発生した場合、RuntimeErrorに変換される。"""
        lock_path = tmp_path / "test.lock"
        mock_msvcrt = MagicMock()
        mock_msvcrt.locking.side_effect = PermissionError("Permission denied")
        with (
            patch("orchestune.infra.process_utils.fcntl", None),
            patch("orchestune.infra.process_utils.msvcrt", mock_msvcrt),
        ):
            executed = False
            with pytest.raises(
                RuntimeError, match="Another instance is already running"
            ):
                with file_lock(lock_path):
                    executed = True
            assert not executed

    def test_file_lock_fcntl_conflict_raises_runtime_error(self, tmp_path):
        """#274レビュー対応(P2): fcntl使用時にBlockingIOError（ロック競合）が
        発生した場合、RuntimeErrorに変換される。"""
        lock_path = tmp_path / "test.lock"
        mock_fcntl = MagicMock()
        mock_fcntl.LOCK_EX = 1
        mock_fcntl.LOCK_NB = 2
        mock_fcntl.LOCK_UN = 3
        mock_fcntl.flock.side_effect = BlockingIOError(
            "Resource temporarily unavailable"
        )
        with patch("orchestune.infra.process_utils.fcntl", mock_fcntl):
            executed = False
            with pytest.raises(
                RuntimeError, match="Another instance is already running"
            ):
                with file_lock(lock_path):
                    executed = True
            assert not executed

    def test_file_lock_open_permission_error_propagates_unchanged(self, tmp_path):
        """#274レビュー対応(P2): open()自体がPermissionError（ACLや読み取り専用
        マウント等、ロック競合とは無関係の失敗）を送出した場合、
        「Another instance is already running」に化けさせず、そのまま伝播させる。"""
        lock_path = tmp_path / "test.lock"
        with (
            patch(
                "orchestune.infra.process_utils.open",
                side_effect=PermissionError("Permission denied"),
                create=True,
            ),
            pytest.raises(PermissionError, match="Permission denied"),
        ):
            with file_lock(lock_path):
                pass

    def test_file_lock_msvcrt_open_permission_error_propagates_unchanged(
        self, tmp_path
    ):
        """#274レビュー対応(P2): msvcrt経路でもopen()自体のPermissionErrorは
        ロック競合と誤認されず、そのまま伝播する。"""
        lock_path = tmp_path / "test.lock"
        mock_msvcrt = MagicMock()
        with (
            patch("orchestune.infra.process_utils.fcntl", None),
            patch("orchestune.infra.process_utils.msvcrt", mock_msvcrt),
            patch(
                "orchestune.infra.process_utils.open",
                side_effect=PermissionError("Permission denied"),
                create=True,
            ),
            pytest.raises(PermissionError, match="Permission denied"),
        ):
            with file_lock(lock_path):
                pass
        mock_msvcrt.locking.assert_not_called()


class TestResolveWorktreePath:
    def test_valid_branch_name(self, tmp_path):
        worktree_root = tmp_path / "worktrees"
        path = _resolve_worktree_path(worktree_root, "feature/issue-42")
        assert path == worktree_root / "feature-issue-42"

    def test_invalid_branch_name_raises_value_error(self, tmp_path):
        worktree_root = tmp_path / "worktrees"
        with pytest.raises(ValueError, match="ブランチ名が不正です"):
            _resolve_worktree_path(worktree_root, "--invalid-branch")


class TestCleanupExistingWorktree:
    def test_noop_when_path_does_not_exist(self, tmp_path):
        worktree_path = tmp_path / "nonexistent"
        err = _cleanup_existing_worktree(worktree_path, issue_number=1)
        assert err is None

    def test_successful_backup_and_removal(self, tmp_path):
        worktree_path = tmp_path / "worktrees" / "feature-1"
        worktree_path.mkdir(parents=True)
        (worktree_path / "dummy.txt").write_text("hello")

        with (
            patch(
                "orchestune.dispatch.worktree.dispatch_gc.backup_wip_commit",
                return_value=None,
            ) as mock_backup,
            patch("orchestune.dispatch.worktree.run_git") as mock_run_git,
        ):
            err = _cleanup_existing_worktree(worktree_path, issue_number=1)
            assert err is None
            mock_backup.assert_called_once_with(
                worktree_path, "WIP: backup by Orchestune before worktree recreation"
            )
            mock_run_git.assert_called_once_with(
                ["worktree", "remove", "--force", str(worktree_path)],
                cwd=None,
                check=False,
            )

    def test_backup_failure_returns_error_string(self, tmp_path):
        worktree_path = tmp_path / "worktrees" / "feature-1"
        worktree_path.mkdir(parents=True)

        with patch(
            "orchestune.dispatch.worktree.dispatch_gc.backup_wip_commit",
            return_value="backup error",
        ):
            err = _cleanup_existing_worktree(worktree_path, issue_number=1)
            assert err == "backup error"


class TestCreateWorktree:
    def test_create_worktree_existing_branch(self, tmp_path):
        worktree_root = tmp_path / "worktrees"
        worktree_path = worktree_root / "feature-1"
        with (
            patch("orchestune.dispatch.worktree._branch_exists", return_value=True),
            patch("orchestune.dispatch.worktree.run_git") as mock_run_git,
        ):
            _create_worktree(worktree_path, worktree_root, "feature-1")
            assert mock_run_git.call_count == 2
            assert mock_run_git.call_args_list[0].args[0] == ["worktree", "prune"]
            assert mock_run_git.call_args_list[1].args[0] == [
                "worktree",
                "add",
                str(worktree_path),
                "feature-1",
            ]

    def test_create_worktree_new_branch_with_base(self, tmp_path):
        worktree_root = tmp_path / "worktrees"
        worktree_path = worktree_root / "feature-1"
        with (
            patch("orchestune.dispatch.worktree._branch_exists", return_value=False),
            patch(
                "orchestune.dispatch.worktree.resolve_local_or_remote_branch",
                return_value="origin/parent/issue-10",
            ),
            patch("orchestune.dispatch.worktree.run_git") as mock_run_git,
        ):
            _create_worktree(
                worktree_path,
                worktree_root,
                "feature-1",
                base_branch="parent/issue-10",
            )
            assert mock_run_git.call_count == 2
            assert mock_run_git.call_args_list[1].args[0] == [
                "worktree",
                "add",
                "-b",
                "feature-1",
                str(worktree_path),
                "origin/parent/issue-10",
            ]


class TestProvisionAndLaunch:
    def test_launches_dispatch_target_and_records_start_time(self, tmp_path):
        task = _task(1)
        worktree_path = tmp_path / "worktrees" / "feature-1"
        fake_target = MagicMock()
        fake_handle = DispatchHandle(
            pid=999, external_id="ext-1", branch_name="feature/1"
        )
        fake_target.launch.return_value = fake_handle

        with patch("orchestune.dispatch.worktree.time.time", return_value=12345.67):
            handle, started_at = _provision_and_launch(
                fake_target, task, "feature/1", worktree_path
            )
            assert handle == fake_handle
            assert started_at == 12345.67
            fake_target.launch.assert_called_once_with(task, "feature/1", worktree_path)


class TestCleanupFailedWorktree:
    def test_removes_worktree_and_prunes(self, tmp_path):
        worktree_path = tmp_path / "worktrees" / "feature-1"
        worktree_path.mkdir(parents=True)

        with patch("orchestune.dispatch.worktree.run_git") as mock_run_git:
            _cleanup_failed_worktree(worktree_path)
            assert mock_run_git.call_count == 2
            assert mock_run_git.call_args_list[0].args[0] == [
                "worktree",
                "remove",
                "--force",
                str(worktree_path),
            ]
            assert mock_run_git.call_args_list[1].args[0] == ["worktree", "prune"]
            assert not worktree_path.exists()
