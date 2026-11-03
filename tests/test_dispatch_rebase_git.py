"""dispatch_rebase内の実際のgit rebase実行（apply層）テスト。

`tests/test_dispatch_rebase.py`の肥大化解消のため分割している（#347）。
純粋なリベース判定ルール（decide層）は`test_dispatch_rebase_rules.py`、
通知処理・エンドツーエンド統合テストは`test_dispatch_rebase.py`に残している。
"""

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from orchestune.dispatch.config.model import DispatcherConfig
from orchestune.dispatch.rebase import RebaseContext
from orchestune.dispatch.scoring import Task
from orchestune.ledger.run_state import ActiveWorktree, RunState
from tests.dispatch_test_support import make_test_active_worktree, make_test_task


def _task(**overrides):
    """rebase系テストの既定Task（footprintを宣言した`status:in-progress`）。"""
    defaults = {"footprint": ("src/foo.py",)}
    defaults.update(overrides)
    return make_test_task(**defaults)


def _active(**overrides):
    """rebase系テストの既定ActiveWorktree（footprintを宣言済み）。"""
    defaults = {"declared_footprint": ("src/foo.py",)}
    defaults.update(overrides)
    return make_test_active_worktree(**defaults)


def _context(
    active: ActiveWorktree,
    task: Task,
    run_state: RunState,
    config: DispatcherConfig,
) -> RebaseContext:
    return RebaseContext(
        active=active,
        active_task=task,
        key="1",
        run_state=run_state,
        dependencies=MagicMock(),
        config=config,
    )


class TestWaitForProcessTerminate:
    """#274レビュー対応(P1): is_process_alive経由でポーリングする(os.killは直接呼ばない)。"""

    @patch("orchestune.dispatch.rebase.is_process_alive", autospec=True)
    @patch("orchestune.dispatch.rebase.time.sleep")
    def test_wait_immediate_exit(self, mock_sleep, mock_is_alive):
        mock_is_alive.return_value = False

        from orchestune.dispatch.rebase import _wait_for_process_terminate

        _wait_for_process_terminate(12345, timeout=1.0)

        mock_is_alive.assert_called_once_with(12345)
        mock_sleep.assert_not_called()

    @patch("orchestune.dispatch.rebase.is_process_alive", autospec=True)
    @patch("orchestune.dispatch.rebase.time.sleep")
    def test_wait_exit_after_polling(self, mock_sleep, mock_is_alive):
        # 1, 2回目は生存、3回目に非生存で終了
        mock_is_alive.side_effect = [True, True, False]

        from orchestune.dispatch.rebase import _wait_for_process_terminate

        _wait_for_process_terminate(12345, timeout=1.0)

        assert mock_is_alive.call_count == 3
        mock_is_alive.assert_has_calls([call(12345), call(12345), call(12345)])
        assert mock_sleep.call_count == 2
        mock_sleep.assert_has_calls([call(0.1), call(0.1)])

    @patch("orchestune.dispatch.rebase.is_process_alive", autospec=True)
    @patch("orchestune.dispatch.rebase.time.sleep")
    def test_wait_timeout(self, mock_sleep, mock_is_alive):
        # ずっとプロセスが存在している場合、タイムアウト時間経過で抜ける
        mock_is_alive.return_value = True

        from orchestune.dispatch.rebase import _wait_for_process_terminate

        with patch("orchestune.dispatch.rebase.time.time") as mock_time:
            # startの取得時で 0.0、その後のループ条件評価で 0.0, 0.05, 0.11
            mock_time.side_effect = [0.0, 0.0, 0.05, 0.11]
            _wait_for_process_terminate(12345, timeout=0.1)

        assert mock_is_alive.call_count >= 1


class TestApplyAutoRebase:
    @pytest.fixture(autouse=True)
    def _worker_already_exited(self):
        with patch("orchestune.dispatch.rebase.is_process_alive", return_value=False):
            yield

    @patch("orchestune.dispatch.rebase.os.kill")
    @patch("orchestune.dispatch.rebase.subprocess.run")
    @patch(
        "orchestune.dispatch.rebase.resolve_local_or_remote_branch",
        autospec=True,
        return_value="main",
    )
    def test_updates_base_branch_on_success(
        self, mock_resolve, mock_run, mock_kill, tmp_path
    ):
        from orchestune.dispatch.rebase import _apply_auto_rebase

        active = _active(base_branch="origin/main")
        task = _task()
        run_state = RunState(active_worktrees={"1": active})

        # mock CI pass
        mock_run.return_value.returncode = 0

        # mock launch target
        from unittest.mock import MagicMock

        mock_target = MagicMock()
        mock_target.launch.return_value = MagicMock(
            pid=4_200_222, external_id="ext-1", external_url="url-1"
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=mock_target,
            apply=True,
        )

        _apply_auto_rebase(_context(active, task, run_state, config), "parent-branch")

        # Assert base_branch updated to parent-branch
        assert run_state.active_worktrees["1"].core.base_branch == "parent-branch"
        assert run_state.active_worktrees["1"].launch.pid == 4_200_222
        # 再launch時はforce_push=Trueを明示的に渡し、base_branchも伝達する。
        mock_target.launch.assert_called_once_with(
            task,
            active.core.branch,
            Path(active.core.worktree_path),
            force_push=True,
            execution_selection=None,
            base_branch="parent-branch",
        )

    @patch("orchestune.dispatch.rebase.os.kill")
    @patch("orchestune.dispatch.rebase.subprocess.run")
    @patch(
        "orchestune.dispatch.rebase.resolve_local_or_remote_branch",
        autospec=True,
        return_value="main",
    )
    def test_push_failure_after_successful_rebase_is_reported_distinctly(
        self, mock_resolve, mock_run, mock_kill, tmp_path
    ):
        """#384: rebase自体は成功した後のpush失敗（non-fast-forward等）は、
        「コンフリクト」ではなくpush失敗として原因をIssueへ正しく報告すること。"""
        import subprocess

        from orchestune.dispatch.rebase import _apply_auto_rebase

        active = _active(base_branch="origin/main")
        task = _task()
        run_state = RunState(active_worktrees={"1": active})

        # rebase・local-ci.shはいずれも成功
        mock_run.return_value.returncode = 0

        from unittest.mock import MagicMock

        mock_target = MagicMock()
        mock_target.launch.side_effect = subprocess.CalledProcessError(
            1,
            [
                "git",
                "push",
                "--force-with-lease",
                "--set-upstream",
                "origin",
                active.core.branch,
            ],
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=mock_target,
            apply=True,
        )

        with (
            patch("fake_forge_proxy.active_fake_forge.remove_label"),
            patch("fake_forge_proxy.active_fake_forge.add_label"),
            patch("fake_forge_proxy.active_fake_forge.add_comment") as mock_comment,
        ):
            _apply_auto_rebase(
                _context(active, task, run_state, config), "parent-branch"
            )

        posted_message = mock_comment.call_args.args[1]
        assert "push" in posted_message
        assert "自動リベース中にコンフリクトが発生しました" not in posted_message
        assert "1" not in run_state.active_worktrees

    @patch("orchestune.dispatch.rebase.os.kill")
    @patch("orchestune.dispatch.rebase.subprocess.run")
    @patch(
        "orchestune.dispatch.rebase.resolve_local_or_remote_branch",
        autospec=True,
        return_value="main",
    )
    def test_keeps_original_base_branch_on_failure(
        self, mock_resolve, mock_run, mock_kill, tmp_path
    ):
        import subprocess

        from orchestune.dispatch.rebase import _apply_auto_rebase

        active = _active(base_branch="origin/main")
        task = _task()
        run_state = RunState(active_worktrees={"1": active})

        # mock rebase fail (subprocess.CalledProcessError)
        mock_run.side_effect = subprocess.CalledProcessError(1, ["git", "rebase"])

        from unittest.mock import MagicMock

        mock_target = MagicMock()
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=mock_target,
            apply=True,
        )

        with (
            patch("fake_forge_proxy.active_fake_forge.remove_label"),
            patch("fake_forge_proxy.active_fake_forge.add_label"),
            patch("fake_forge_proxy.active_fake_forge.add_comment"),
        ):
            _apply_auto_rebase(
                _context(active, task, run_state, config), "parent-branch"
            )

        # Assert base_branch is still origin/main (not updated)
        assert active.core.base_branch == "origin/main"

    @patch("orchestune.dispatch.rebase.os.kill")
    @patch("orchestune.dispatch.rebase.subprocess.run")
    @patch(
        "orchestune.dispatch.rebase.resolve_local_or_remote_branch",
        autospec=True,
        return_value="main",
    )
    def test_failure_adds_manual_merge_before_removing_in_progress(
        self, mock_resolve, mock_run, mock_kill, tmp_path
    ):
        # #381: 途中でクラッシュしてもIssueが必ずいずれかのstatus:*ラベルを
        # 持ち続けるよう、addがremoveより先に呼ばれなければならない。
        import subprocess

        from orchestune.dispatch.rebase import _apply_auto_rebase

        active = _active(base_branch="origin/main")
        task = _task()
        run_state = RunState(active_worktrees={"1": active})
        mock_run.side_effect = subprocess.CalledProcessError(1, ["git", "rebase"])

        from unittest.mock import MagicMock

        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=MagicMock(),
            apply=True,
        )
        call_order: list[tuple[str, str]] = []

        with (
            patch(
                "fake_forge_proxy.active_fake_forge.remove_label",
                side_effect=lambda issue, label: call_order.append(("remove", label)),
            ),
            patch(
                "fake_forge_proxy.active_fake_forge.add_label",
                side_effect=lambda issue, label: call_order.append(("add", label)),
            ),
            patch("fake_forge_proxy.active_fake_forge.add_comment"),
        ):
            _apply_auto_rebase(
                _context(active, task, run_state, config), "parent-branch"
            )

        assert call_order == [
            ("add", "status:manual-merge-required"),
            ("remove", "status:in-progress"),
        ]

    @patch("orchestune.dispatch.rebase.os.kill")
    @patch("orchestune.dispatch.rebase.subprocess.run")
    @patch(
        "orchestune.dispatch.rebase.default_ci_command",
        autospec=True,
        return_value=[
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "scripts/local-ci.ps1",
        ],
    )
    @patch(
        "orchestune.dispatch.rebase.resolve_local_or_remote_branch",
        autospec=True,
        return_value="main",
    )
    def test_ci_failure_after_successful_rebase_is_reported_distinctly_windows(
        self, mock_resolve, mock_ci_command, mock_run, mock_kill, tmp_path
    ):
        """#427: WindowsではdefaultCiCommand()がpowershell経由のコマンドを
        返すため、cmd_args[0]（"powershell"）だけを見ると「local-ci.sh」に
        一致せず、CI失敗が誤って「コンフリクト」として報告されてしまう。
        cmd全体を見て正しくCI失敗と分類すること。"""
        import subprocess

        from orchestune.dispatch.rebase import _apply_auto_rebase

        active = _active(base_branch="origin/main")
        task = _task()
        run_state = RunState(active_worktrees={"1": active})

        # WIP退避チェック（clean）→ rebase成功 → ローカルCI（Windows: powershell経由）が失敗
        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=["git", "status", "--porcelain"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "rebase", "main"], returncode=0, stdout="", stderr=""
            ),
            subprocess.CalledProcessError(
                1,
                mock_ci_command.return_value,
                stderr="CI failed",
            ),
        ]

        from unittest.mock import MagicMock

        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=MagicMock(),
            apply=True,
        )

        with (
            patch("fake_forge_proxy.active_fake_forge.remove_label"),
            patch("fake_forge_proxy.active_fake_forge.add_label"),
            patch("fake_forge_proxy.active_fake_forge.add_comment") as mock_comment,
        ):
            _apply_auto_rebase(
                _context(active, task, run_state, config), "parent-branch"
            )

        posted_message = mock_comment.call_args.args[1]
        assert "自動リベース後のローカルCI実行に失敗しました" in posted_message
        assert "自動リベース中にコンフリクトが発生しました" not in posted_message
        assert "CI failed" not in posted_message
        assert "1" not in run_state.active_worktrees

    @patch("orchestune.dispatch.rebase.os.kill")
    @patch("orchestune.dispatch.rebase.subprocess.run")
    @patch(
        "orchestune.dispatch.rebase.default_ci_command",
        autospec=True,
        return_value=["./scripts/local-ci.sh"],
    )
    @patch(
        "orchestune.dispatch.rebase.resolve_local_or_remote_branch",
        autospec=True,
        return_value="main",
    )
    def test_ci_failure_after_successful_rebase_is_reported_distinctly_posix(
        self, mock_resolve, mock_ci_command, mock_run, mock_kill, tmp_path
    ):
        """POSIX側（cmd_args == ["./scripts/local-ci.sh"]）でも引き続き
        CI失敗として正しく分類されること（回帰防止）。"""
        import subprocess

        from orchestune.dispatch.rebase import _apply_auto_rebase

        active = _active(base_branch="origin/main")
        task = _task()
        run_state = RunState(active_worktrees={"1": active})

        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=["git", "status", "--porcelain"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "rebase", "main"], returncode=0, stdout="", stderr=""
            ),
            subprocess.CalledProcessError(
                1,
                mock_ci_command.return_value,
                stderr="CI failed",
            ),
        ]

        from unittest.mock import MagicMock

        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=MagicMock(),
            apply=True,
        )

        with (
            patch("fake_forge_proxy.active_fake_forge.remove_label"),
            patch("fake_forge_proxy.active_fake_forge.add_label"),
            patch("fake_forge_proxy.active_fake_forge.add_comment") as mock_comment,
        ):
            _apply_auto_rebase(
                _context(active, task, run_state, config), "parent-branch"
            )

        posted_message = mock_comment.call_args.args[1]
        assert "自動リベース後のローカルCI実行に失敗しました" in posted_message
        assert "自動リベース中にコンフリクトが発生しました" not in posted_message
        assert "CI failed" not in posted_message
        assert "1" not in run_state.active_worktrees

    @patch("orchestune.dispatch.rebase.os.kill")
    @patch("orchestune.dispatch.rebase.subprocess.run")
    @patch(
        "orchestune.dispatch.rebase.default_ci_command",
        autospec=True,
        return_value=["./scripts/local-ci.sh"],
    )
    @patch(
        "orchestune.dispatch.rebase.resolve_local_or_remote_branch",
        autospec=True,
        return_value="main",
    )
    def test_rebase_conflict_against_branch_named_like_ci_command_is_not_misclassified(
        self, mock_resolve, mock_ci_command, mock_run, mock_kill, tmp_path
    ):
        """レビュー指摘(#440): rebase対象のブランチ名がたまたま"local-ci"を含む
        場合でも、cmd全体をdefault_ci_command()と部分一致ではなく厳密一致で
        比較しているため、CI失敗と誤分類されずコンフリクトとして扱われること。"""
        import subprocess

        from orchestune.dispatch.rebase import _apply_auto_rebase

        active = _active(base_branch="origin/main")
        task = _task()
        run_state = RunState(active_worktrees={"1": active})

        # WIP退避チェック（clean）→ rebaseが"local-ci"を含むブランチ名との
        # コンフリクトで失敗（CIはまだ実行されていない）
        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=["git", "status", "--porcelain"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            subprocess.CalledProcessError(
                1, ["git", "rebase", "fix-local-ci-flakiness"]
            ),
        ]

        from unittest.mock import MagicMock

        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=MagicMock(),
            apply=True,
        )

        with (
            patch("fake_forge_proxy.active_fake_forge.remove_label"),
            patch("fake_forge_proxy.active_fake_forge.add_label"),
            patch("fake_forge_proxy.active_fake_forge.add_comment") as mock_comment,
        ):
            _apply_auto_rebase(
                _context(active, task, run_state, config), "parent-branch"
            )

        posted_message = mock_comment.call_args.args[1]
        assert "自動リベース中にコンフリクトが発生しました" in posted_message
        assert "自動リベース後のローカルCI実行に失敗しました" not in posted_message

    @patch("orchestune.dispatch.rebase.os.kill")
    @patch("orchestune.dispatch.rebase.dispatch_gc.backup_wip_commit", autospec=True)
    @patch("orchestune.dispatch.rebase.subprocess.run")
    @patch(
        "orchestune.dispatch.rebase.resolve_local_or_remote_branch",
        autospec=True,
        return_value="main",
    )
    def test_backs_up_wip_before_rebase_when_dirty(
        self, mock_resolve, mock_run, mock_backup, mock_kill, tmp_path
    ):
        """#213: dirtyなworktreeでは、rebaseを試みる前にWIP退避が呼ばれること。"""
        from orchestune.dispatch.rebase import _apply_auto_rebase

        active = _active(base_branch="origin/main")
        task = _task()
        run_state = RunState(active_worktrees={"1": active})

        mock_backup.return_value = None  # 退避成功（またはclean）
        mock_run.return_value.returncode = 0

        from unittest.mock import MagicMock

        mock_target = MagicMock()
        mock_target.launch.return_value = MagicMock(
            pid=4_200_222, external_id="ext-1", external_url="url-1"
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=mock_target,
            apply=True,
        )

        _apply_auto_rebase(_context(active, task, run_state, config), "parent-branch")

        mock_backup.assert_called_once_with(
            active.core.worktree_path, "WIP: backup by Orchestune auto-rebase"
        )
        # rebaseコマンドが実行されている（退避成功時は通常フローを継続する）
        rebase_calls = [
            call_args
            for call_args in mock_run.call_args_list
            if "rebase" in call_args.args[0]
        ]
        assert rebase_calls
        assert run_state.active_worktrees["1"].core.base_branch == "parent-branch"

    @patch("orchestune.dispatch.rebase.os.kill")
    @patch("orchestune.dispatch.rebase.dispatch_gc.backup_wip_commit", autospec=True)
    @patch("orchestune.dispatch.rebase.subprocess.run")
    def test_backup_failure_skips_rebase_and_escalates_to_manual_merge(
        self, mock_run, mock_backup, mock_kill, tmp_path
    ):
        """#213: WIP退避自体が失敗した場合、rebaseを試みずmanual-merge-requiredへ
        エスカレーションし、未コミット作業の消失を防ぐ。"""
        from orchestune.dispatch.rebase import _apply_auto_rebase

        active = _active(base_branch="origin/main")
        task = _task()
        run_state = RunState(active_worktrees={"1": active})

        mock_backup.return_value = "fatal: unable to write new index file"

        from unittest.mock import MagicMock

        mock_target = MagicMock()
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=mock_target,
            apply=True,
        )

        with (
            patch("fake_forge_proxy.active_fake_forge.remove_label") as mock_remove,
            patch("fake_forge_proxy.active_fake_forge.add_label") as mock_add_label,
            patch("fake_forge_proxy.active_fake_forge.add_comment") as mock_comment,
        ):
            _apply_auto_rebase(
                _context(active, task, run_state, config), "parent-branch"
            )

        mock_backup.assert_called_once_with(
            active.core.worktree_path, "WIP: backup by Orchestune auto-rebase"
        )
        mock_run.assert_not_called()  # rebaseは一切試みられない
        mock_remove.assert_called_once_with(
            active.core.issue_number, "status:in-progress"
        )
        mock_add_label.assert_called_once_with(
            active.core.issue_number, "status:manual-merge-required"
        )
        mock_comment.assert_called_once()
        assert (
            "WIPバックアップコミットの作成に失敗しました"
            in mock_comment.call_args.args[1]
        )
        assert "1" not in run_state.active_worktrees
        assert active.core.base_branch == "origin/main"

    @patch("orchestune.dispatch.rebase.os.kill")
    @patch("orchestune.dispatch.rebase.dispatch_gc.backup_wip_commit", autospec=True)
    @patch("orchestune.dispatch.rebase.subprocess.run")
    def test_backup_failure_adds_manual_merge_before_removing_in_progress(
        self, mock_run, mock_backup, mock_kill, tmp_path
    ):
        # #381: 途中でクラッシュしてもIssueが必ずいずれかのstatus:*ラベルを
        # 持ち続けるよう、addがremoveより先に呼ばれなければならない。
        from orchestune.dispatch.rebase import _apply_auto_rebase

        active = _active(base_branch="origin/main")
        task = _task()
        run_state = RunState(active_worktrees={"1": active})
        mock_backup.return_value = "fatal: unable to write new index file"

        from unittest.mock import MagicMock

        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=MagicMock(),
            apply=True,
        )
        call_order: list[tuple[str, str]] = []

        with (
            patch(
                "fake_forge_proxy.active_fake_forge.remove_label",
                side_effect=lambda issue, label: call_order.append(("remove", label)),
            ),
            patch(
                "fake_forge_proxy.active_fake_forge.add_label",
                side_effect=lambda issue, label: call_order.append(("add", label)),
            ),
            patch("fake_forge_proxy.active_fake_forge.add_comment"),
        ):
            _apply_auto_rebase(
                _context(active, task, run_state, config), "parent-branch"
            )

        assert call_order == [
            ("add", "status:manual-merge-required"),
            ("remove", "status:in-progress"),
        ]

    @patch("orchestune.dispatch.rebase.os.kill")
    @patch("orchestune.dispatch.rebase.subprocess.run")
    @patch(
        "orchestune.dispatch.rebase.resolve_local_or_remote_branch",
        autospec=True,
        return_value="main",
    )
    def test_configured_ci_command_failure_is_not_misclassified_as_conflict(
        self, mock_resolve, mock_run, mock_kill, tmp_path
    ):
        """設定された ci_command が失敗したとき、cmd と default_ci_command() の不一致によって
        コンフリクトと誤認されず、CI失敗メッセージが投稿されること。"""
        import subprocess

        from orchestune.dispatch.rebase import _apply_auto_rebase

        active = _active(base_branch="origin/main")
        task = _task()
        run_state = RunState(active_worktrees={"1": active})

        custom_ci = ["uv", "run", "pytest", "-q"]
        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=["git", "status", "--porcelain"], returncode=0, stdout=""
            ),
            subprocess.CompletedProcess(
                args=["git", "rebase", "main"], returncode=0, stdout=""
            ),
            subprocess.CalledProcessError(1, custom_ci, stderr="Tests failed"),
        ]

        from unittest.mock import MagicMock

        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=MagicMock(),
            ci_command=custom_ci,
            apply=True,
        )

        with (
            patch("fake_forge_proxy.active_fake_forge.remove_label"),
            patch("fake_forge_proxy.active_fake_forge.add_label"),
            patch("fake_forge_proxy.active_fake_forge.add_comment") as mock_comment,
        ):
            _apply_auto_rebase(
                _context(active, task, run_state, config), "parent-branch"
            )

        posted_message = mock_comment.call_args.args[1]
        assert "自動リベース後のローカルCI実行に失敗しました" in posted_message
        assert "自動リベース中にコンフリクトが発生しました" not in posted_message
        assert "CI failed" not in posted_message
        assert "1" not in run_state.active_worktrees

    @patch("orchestune.dispatch.rebase.os.kill")
    @patch("orchestune.dispatch.rebase.subprocess.run")
    @patch(
        "orchestune.dispatch.rebase.resolve_local_or_remote_branch",
        autospec=True,
        return_value="main",
    )
    def test_rebase_ci_uses_configured_ci_command(
        self, mock_resolve, mock_run, mock_kill, tmp_path
    ):
        """DispatcherConfig.ci_command が設定されている場合、そのコマンドが実行されること。"""
        import subprocess

        from orchestune.dispatch.rebase import _apply_auto_rebase

        active = _active(base_branch="origin/main")
        task = _task()
        run_state = RunState(active_worktrees={"1": active})

        custom_ci = ["my-ci-runner", "--fast"]
        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=["git", "status", "--porcelain"], returncode=0, stdout=""
            ),
            subprocess.CompletedProcess(
                args=["git", "rebase", "main"], returncode=0, stdout=""
            ),
            subprocess.CompletedProcess(args=custom_ci, returncode=0, stdout=""),
        ]

        from unittest.mock import MagicMock

        mock_target = MagicMock()
        mock_target.launch.return_value = MagicMock(
            pid=4_200_222, external_id="ext-1", external_url="url-1"
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=mock_target,
            ci_command=custom_ci,
            apply=True,
        )

        _apply_auto_rebase(_context(active, task, run_state, config), "parent-branch")

        ci_call_args = mock_run.call_args_list[-1][0][0]
        assert ci_call_args == custom_ci

    @patch("orchestune.dispatch.rebase.os.kill")
    @patch("orchestune.dispatch.rebase.subprocess.run")
    @patch(
        "orchestune.dispatch.rebase.resolve_local_or_remote_branch",
        autospec=True,
        return_value="main",
    )
    def test_rebase_ci_timeout_reported_as_ci_failure(
        self, mock_resolve, mock_run, mock_kill, tmp_path
    ):
        """CIタイムアウトが発生した場合、プロセスが停止しCI失敗コメントが投稿されること。サイクルは中断しない。"""
        import subprocess

        from orchestune.dispatch.rebase import _apply_auto_rebase

        active = _active(base_branch="origin/main")
        task = _task()
        run_state = RunState(active_worktrees={"1": active})

        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=["git", "status", "--porcelain"], returncode=0, stdout=""
            ),
            subprocess.CompletedProcess(
                args=["git", "rebase", "main"], returncode=0, stdout=""
            ),
            subprocess.TimeoutExpired(cmd=["ci"], timeout=10.0),
        ]

        from unittest.mock import MagicMock

        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=MagicMock(),
            apply=True,
        )

        with (
            patch("fake_forge_proxy.active_fake_forge.remove_label"),
            patch("fake_forge_proxy.active_fake_forge.add_label"),
            patch("fake_forge_proxy.active_fake_forge.add_comment") as mock_comment,
            patch("orchestune.dispatch.rebase.logger.warning") as mock_log_warn,
        ):
            _apply_auto_rebase(
                _context(active, task, run_state, config), "parent-branch"
            )

        posted_message = mock_comment.call_args.args[1]
        assert "自動リベース後のローカルCI実行に失敗しました" in posted_message
        assert "1" not in run_state.active_worktrees
        mock_log_warn.assert_called()

    @patch("orchestune.dispatch.rebase.os.kill")
    @patch("orchestune.dispatch.rebase.subprocess.run")
    @patch(
        "orchestune.dispatch.rebase.resolve_local_or_remote_branch",
        autospec=True,
        return_value="main",
    )
    def test_rebase_ci_sync_dependencies_failure_reported_as_ci_failure(
        self, mock_resolve, mock_run, mock_kill, tmp_path
    ):
        """pyproject.toml が存在して uv sync が失敗した場合も、CI失敗コメントとして処理されること。"""
        import subprocess

        from orchestune.dispatch.rebase import _apply_auto_rebase

        wt_path = tmp_path / "wt"
        wt_path.mkdir(parents=True, exist_ok=True)
        (wt_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")

        active = _active(
            base_branch="origin/main",
            worktree_path=str(wt_path),
        )
        task = _task()
        run_state = RunState(active_worktrees={"1": active})

        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=["git", "status", "--porcelain"], returncode=0, stdout=""
            ),
            subprocess.CompletedProcess(
                args=["git", "rebase", "main"], returncode=0, stdout=""
            ),
            # uv sync failure
            subprocess.CalledProcessError(1, ["uv", "sync"], stderr="sync failed"),
        ]

        from unittest.mock import MagicMock

        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=MagicMock(),
            apply=True,
        )

        with (
            patch("fake_forge_proxy.active_fake_forge.remove_label"),
            patch("fake_forge_proxy.active_fake_forge.add_label"),
            patch("fake_forge_proxy.active_fake_forge.add_comment") as mock_comment,
        ):
            _apply_auto_rebase(
                _context(active, task, run_state, config), "parent-branch"
            )

        posted_message = mock_comment.call_args.args[1]
        assert "自動リベース後のローカルCI実行に失敗しました" in posted_message
        assert "1" not in run_state.active_worktrees

    @patch("orchestune.dispatch.rebase.os.kill")
    @patch("orchestune.dispatch.rebase.subprocess.run")
    @patch(
        "orchestune.dispatch.rebase.resolve_local_or_remote_branch",
        autospec=True,
        return_value="main",
    )
    def test_rebase_ci_environment_variables(
        self, mock_resolve, mock_run, mock_kill, tmp_path, monkeypatch
    ):
        """ORCHESTUNE_* は ORCHESTUNE_TEST_WORKERS 以外が除去され、PYTHON_KEYRING_BACKEND が設定されること。"""
        import subprocess

        from orchestune.dispatch.rebase import _apply_auto_rebase

        monkeypatch.setenv("ORCHESTUNE_FOO", "secret")
        monkeypatch.setenv("ORCHESTUNE_TEST_WORKERS", "4")

        active = _active(base_branch="origin/main")
        task = _task()
        run_state = RunState(active_worktrees={"1": active})

        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=["git", "status", "--porcelain"], returncode=0, stdout=""
            ),
            subprocess.CompletedProcess(
                args=["git", "rebase", "main"], returncode=0, stdout=""
            ),
            subprocess.CompletedProcess(args=["local-ci"], returncode=0, stdout=""),
        ]

        from unittest.mock import MagicMock

        mock_target = MagicMock()
        mock_target.launch.return_value = MagicMock(
            pid=4_200_222, external_id="ext-1", external_url="url-1"
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=mock_target,
            apply=True,
        )

        _apply_auto_rebase(_context(active, task, run_state, config), "parent-branch")

        ci_call = mock_run.call_args_list[-1]
        ci_env = ci_call[1].get("env") or {}
        assert "ORCHESTUNE_FOO" not in ci_env
        assert ci_env.get("ORCHESTUNE_TEST_WORKERS") == "4"
        assert ci_env.get("PYTHON_KEYRING_BACKEND") == "keyring.backends.null.Keyring"


def test_rebase_py_does_not_contain_subprocess_run_or_get_ci_env():
    """rebase.py に subprocess.run および _get_ci_env が残っていないこと。"""
    import inspect

    import orchestune.dispatch.rebase as rebase_mod

    assert not hasattr(rebase_mod, "_get_ci_env")
    source = inspect.getsource(rebase_mod)
    assert "subprocess.run(" not in source
