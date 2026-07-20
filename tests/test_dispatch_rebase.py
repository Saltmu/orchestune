from unittest.mock import ANY, call, patch

from orchestune.dag import FootprintConflict
from orchestune.dispatch_rebase import (
    _decide_footprint_deviation_outcome,
    _decide_rebase_needed,
    _decide_rebase_target,
    _try_auto_rebase,
    notify_force_serial,
    notify_recompute,
)
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree, RunState
from orchestune.dispatcher import DispatcherConfig


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


class TestNotifyRecompute:
    def test_dry_run_reports_without_calling_github(self):
        conflict = FootprintConflict(
            subtask_id="task-a",
            other_subtask_id="task-b",
            similarity=0.5,
            blocked_subtask_id="task-b",
        )
        with (
            patch("orchestune.dispatch_rebase.github.add_comment") as mock_comment,
            patch("orchestune.dispatch_rebase.github.add_label") as mock_label,
        ):
            bodies = notify_recompute(
                conflict,
                "作業内容の要約",
                parent_issue_number=181,
                apply=False,
                issue_number_by_subtask_id={"task-a": 1, "task-b": 2},
            )
        mock_comment.assert_not_called()
        mock_label.assert_not_called()
        assert len(bodies) >= 2

    def test_apply_posts_comments_and_labels_blocked_subtask(self):
        conflict = FootprintConflict(
            subtask_id="task-a",
            other_subtask_id="task-b",
            similarity=0.5,
            blocked_subtask_id="task-b",
        )
        with (
            patch("orchestune.dispatch_rebase.github.add_comment") as mock_comment,
            patch("orchestune.dispatch_rebase.github.add_label") as mock_label,
            patch("orchestune.dispatch_rebase.github.remove_label"),
        ):
            notify_recompute(
                conflict,
                "作業内容の要約",
                parent_issue_number=181,
                apply=True,
                issue_number_by_subtask_id={"task-a": 1, "task-b": 2},
            )
        assert mock_comment.call_count >= 3  # task-a issue, task-b issue, parent issue
        mock_label.assert_any_call(2, "status:blocked-recompute")

    def test_apply_removes_queued_and_adds_blocked_labels(self):
        conflict = FootprintConflict(
            subtask_id="task-a",
            other_subtask_id="task-b",
            similarity=0.5,
            blocked_subtask_id="task-b",
        )
        with (
            patch("orchestune.dispatch_rebase.github.add_comment"),
            patch("orchestune.dispatch_rebase.github.add_label") as mock_add_label,
            patch(
                "orchestune.dispatch_rebase.github.remove_label"
            ) as mock_remove_label,
        ):
            notify_recompute(
                conflict,
                "作業内容の要約",
                parent_issue_number=181,
                apply=True,
                issue_number_by_subtask_id={"task-a": 1, "task-b": 2},
            )
        mock_remove_label.assert_any_call(2, "status:queued")
        mock_add_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:blocked-recompute")


class TestNotifyForceSerial:
    """#200: リトライ上限超過時の強制直列化フォールバック通知。"""

    def test_dry_run_does_not_call_github(self):
        with patch("orchestune.dispatch_rebase.github.add_comment") as mock_comment:
            body = notify_force_serial(
                "task-a",
                issue_number=1,
                parent_issue_number=181,
                retry_count=2,
                apply=False,
            )
        mock_comment.assert_not_called()
        assert "task-a" in body

    def test_apply_posts_comment_to_parent_issue(self):
        with patch("orchestune.dispatch_rebase.github.add_comment") as mock_comment:
            notify_force_serial(
                "task-a",
                issue_number=1,
                parent_issue_number=181,
                retry_count=2,
                apply=True,
            )
        mock_comment.assert_called_once_with(181, ANY)

    def test_apply_without_parent_issue_skips_comment(self):
        with patch("orchestune.dispatch_rebase.github.add_comment") as mock_comment:
            notify_force_serial(
                "task-a",
                issue_number=1,
                parent_issue_number=None,
                retry_count=2,
                apply=True,
            )
        mock_comment.assert_not_called()


class TestWaitForProcessTerminate:
    @patch("orchestune.dispatch_rebase.os.kill")
    @patch("orchestune.dispatch_rebase.time.sleep")
    def test_wait_immediate_exit(self, mock_sleep, mock_kill):
        # 最初の os.kill(pid, 0) で ProcessLookupError が発生すれば即座に終了する
        mock_kill.side_effect = ProcessLookupError()

        from orchestune.dispatch_rebase import _wait_for_process_terminate

        _wait_for_process_terminate(12345, timeout=1.0)

        mock_kill.assert_called_once_with(12345, 0)
        mock_sleep.assert_not_called()

    @patch("orchestune.dispatch_rebase.os.kill")
    @patch("orchestune.dispatch_rebase.time.sleep")
    def test_wait_exit_after_polling(self, mock_sleep, mock_kill):
        # 1, 2回目は存在（例外なし）、3回目に ProcessLookupError で終了
        mock_kill.side_effect = [None, None, ProcessLookupError()]

        from orchestune.dispatch_rebase import _wait_for_process_terminate

        _wait_for_process_terminate(12345, timeout=1.0)

        assert mock_kill.call_count == 3
        mock_kill.assert_has_calls([call(12345, 0), call(12345, 0), call(12345, 0)])
        assert mock_sleep.call_count == 2
        mock_sleep.assert_has_calls([call(0.1), call(0.1)])

    @patch("orchestune.dispatch_rebase.os.kill")
    @patch("orchestune.dispatch_rebase.time.sleep")
    def test_wait_timeout(self, mock_sleep, mock_kill):
        # ずっとプロセスが存在している場合、タイムアウト時間経過で抜ける
        mock_kill.return_value = None

        from orchestune.dispatch_rebase import _wait_for_process_terminate

        with patch("orchestune.dispatch_rebase.time.time") as mock_time:
            # startの取得時で 0.0、その後のループ条件評価で 0.0, 0.05, 0.11
            mock_time.side_effect = [0.0, 0.0, 0.05, 0.11]
            _wait_for_process_terminate(12345, timeout=0.1)

        assert mock_kill.call_count >= 1

    @patch("orchestune.dispatch_rebase.os.kill")
    @patch("orchestune.dispatch_rebase.time.sleep")
    def test_wait_permission_error_then_exit(self, mock_sleep, mock_kill):
        # 1回目は PermissionError (プロセスは存在している)
        # 2回目に ProcessLookupError で終了
        mock_kill.side_effect = [PermissionError(), ProcessLookupError()]

        from orchestune.dispatch_rebase import _wait_for_process_terminate

        _wait_for_process_terminate(12345, timeout=1.0)

        assert mock_kill.call_count == 2
        mock_sleep.assert_called_once_with(0.1)

    @patch("orchestune.dispatch_rebase.os.kill")
    @patch("orchestune.dispatch_rebase.time.sleep")
    def test_wait_os_error(self, mock_sleep, mock_kill):
        # 1回目に一般的な OSError が発生した場合は即座に終了
        mock_kill.side_effect = OSError()

        from orchestune.dispatch_rebase import _wait_for_process_terminate

        _wait_for_process_terminate(12345, timeout=1.0)

        mock_kill.assert_called_once_with(12345, 0)
        mock_sleep.assert_not_called()


class TestDecideFootprintDeviationOutcome:
    """decide層: DAG再計算自体は純粋計算で、githubへの通知やactive/run_stateの
    変更は行わない。"""

    def test_already_forced_serial_is_noop(self):
        active = _active(forced_serial=True)
        decision = _decide_footprint_deviation_outcome(
            active, ["src/foo.py"], {}, DispatcherConfig()
        )
        assert decision.action == "already_forced_serial"

    def test_unknown_subtask_is_skipped(self):
        active = _active()
        decision = _decide_footprint_deviation_outcome(
            active, ["src/foo.py"], {}, DispatcherConfig()
        )
        assert decision.action == "skipped_unknown_subtask"

    def test_retry_limit_exceeded_forces_serial(self):
        active = _active(recompute_count=2)
        task = _task()
        config = DispatcherConfig(max_recompute_retries=2)
        decision = _decide_footprint_deviation_outcome(
            active, ["src/foo.py"], {1: task}, config
        )
        assert decision.action == "forced_serial"
        assert decision.recompute_count == 2
        # decide層はactive.forced_serialを書き換えない
        assert active.forced_serial is False

    def test_under_retry_limit_recomputes(self):
        active = _active(recompute_count=0)
        task = _task()
        config = DispatcherConfig(max_recompute_retries=2)
        decision = _decide_footprint_deviation_outcome(
            active, ["src/bar.py"], {1: task}, config
        )
        assert decision.action == "recomputed"
        assert decision.subtask_id == "task-a"
        # decide層はactive.recompute_countを書き換えない
        assert active.recompute_count == 0


class TestDecideRebaseTarget:
    def test_no_depends_on_returns_none(self):
        assert _decide_rebase_target(_task(depends_on=()), set(), set(), {}) is None

    def test_returns_branch_when_exactly_one_ci_passed_dependency_exists(self):
        task = _task(depends_on=("task-x", "task-y"))
        branch = _decide_rebase_target(
            task,
            {"task-x"},
            {"task-y"},
            {"task-y": "claude/issue-2-task-y"},
        )
        assert branch == "claude/issue-2-task-y"

    def test_no_ci_passed_dependency_returns_none(self):
        task = _task(depends_on=("task-x",))
        assert _decide_rebase_target(task, set(), set(), {}) is None

    def test_multiple_ci_passed_dependencies_return_none(self):
        task = _task(depends_on=("task-x", "task-y"))
        assert (
            _decide_rebase_target(
                task,
                set(),
                {"task-x", "task-y"},
                {
                    "task-x": "claude/issue-2-task-x",
                    "task-y": "claude/issue-3-task-y",
                },
            )
            is None
        )

    def test_unresolved_dependency_blocks_auto_rebase(self):
        task = _task(depends_on=("task-x", "task-y"))
        assert (
            _decide_rebase_target(
                task,
                set(),
                {"task-y"},
                {"task-y": "claude/issue-3-task-y"},
            )
            is None
        )

    def test_done_dependencies_are_ignored_when_exactly_one_ci_passed(self):
        task = _task(depends_on=("task-x", "task-y"))
        branch = _decide_rebase_target(
            task,
            {"task-x"},
            {"task-y"},
            {"task-y": "claude/issue-3-task-y"},
        )
        assert branch == "claude/issue-3-task-y"


class TestDecideRebaseNeeded:
    def test_ancestor_means_no_rebase_needed(self):
        with (
            patch("orchestune.dispatch_rebase.subprocess.run") as mock_run,
            patch(
                "orchestune.dispatch_rebase.resolve_local_or_remote_branch",
                return_value="main",
            ),
        ):
            mock_run.return_value.returncode = 0
            assert _decide_rebase_needed("main", "feature", "worktrees/w1") is False

    def test_not_ancestor_means_rebase_needed(self):
        with (
            patch("orchestune.dispatch_rebase.subprocess.run") as mock_run,
            patch(
                "orchestune.dispatch_rebase.resolve_local_or_remote_branch",
                return_value="main",
            ),
        ):
            mock_run.return_value.returncode = 1
            assert _decide_rebase_needed("main", "feature", "worktrees/w1") is True

    def test_os_error_defaults_to_no_rebase(self):
        with (
            patch(
                "orchestune.dispatch_rebase.subprocess.run",
                side_effect=OSError("boom"),
            ),
            patch(
                "orchestune.dispatch_rebase.resolve_local_or_remote_branch",
                return_value="main",
            ),
        ):
            assert _decide_rebase_needed("main", "feature", "worktrees/w1") is False


class TestTryAutoRebase:
    def test_rebase_not_needed_returns_false(self):
        active = _active(branch="feature")
        task = _task(depends_on=("task-parent",))

        done_subtask_ids = set()
        ci_passed_pr_subtask_ids = {"task-parent"}
        subtask_branch_map = {"task-parent": "parent-branch"}

        run_state = RunState(active_worktrees={})
        config = DispatcherConfig(
            run_state_path="dummy.json", worktree_root="worktrees"
        )

        with (
            patch(
                "orchestune.dispatch_rebase._decide_rebase_needed",
                return_value=False,
            ),
            patch("orchestune.dispatch_rebase._apply_auto_rebase") as mock_apply,
        ):
            result = _try_auto_rebase(
                active=active,
                active_task=task,
                key="1",
                run_state=run_state,
                done_subtask_ids=done_subtask_ids,
                ci_passed_pr_subtask_ids=ci_passed_pr_subtask_ids,
                subtask_branch_map=subtask_branch_map,
                config=config,
            )

        assert result is False
        mock_apply.assert_not_called()

    def test_rebase_needed_returns_true(self):
        active = _active(branch="feature")
        task = _task(depends_on=("task-parent",))

        done_subtask_ids = set()
        ci_passed_pr_subtask_ids = {"task-parent"}
        subtask_branch_map = {"task-parent": "parent-branch"}

        run_state = RunState(active_worktrees={})
        config = DispatcherConfig(
            run_state_path="dummy.json", worktree_root="worktrees"
        )

        with (
            patch(
                "orchestune.dispatch_rebase._decide_rebase_needed",
                return_value=True,
            ),
            patch("orchestune.dispatch_rebase._apply_auto_rebase") as mock_apply,
        ):
            result = _try_auto_rebase(
                active=active,
                active_task=task,
                key="1",
                run_state=run_state,
                done_subtask_ids=done_subtask_ids,
                ci_passed_pr_subtask_ids=ci_passed_pr_subtask_ids,
                subtask_branch_map=subtask_branch_map,
                config=config,
            )

        assert result is True
        mock_apply.assert_called_once_with(
            active, task, "1", run_state, "parent-branch", config
        )


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
            run_state_path="dummy.json",
            worktree_root="worktrees",
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
            run_state_path="dummy.json",
            worktree_root="worktrees",
            dispatch_target=mock_target,
            apply=True,
        )

        with (
            patch("orchestune.dispatch_rebase.github.remove_label"),
            patch("orchestune.dispatch_rebase.github.add_label"),
            patch("orchestune.dispatch_rebase.github.add_comment"),
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
            run_state_path="dummy.json",
            worktree_root="worktrees",
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
            run_state_path="dummy.json",
            worktree_root="worktrees",
            dispatch_target=mock_target,
            apply=True,
        )

        with (
            patch("orchestune.dispatch_rebase.github.remove_label") as mock_remove,
            patch("orchestune.dispatch_rebase.github.add_label") as mock_add_label,
            patch("orchestune.dispatch_rebase.github.add_comment") as mock_comment,
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
