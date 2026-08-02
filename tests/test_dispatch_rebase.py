import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import ANY, MagicMock, call, patch

from orchestune.dag import FootprintConflict
from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_cycle import run_dispatch_cycle
from orchestune.dispatch_rebase import (
    _decide_footprint_deviation_outcome,
    _decide_rebase_needed,
    _decide_rebase_target,
    _try_auto_rebase,
    notify_force_serial,
    notify_recompute,
)
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import (
    ActiveWorktree,
    RunState,
    load_run_state,
    save_run_state,
)
from orchestune.models import PrRecord
from tests.conftest import make_issue


@contextmanager
def _patch_gc_process_alive(*, return_value: bool):
    """Patch every consumer split from the former dispatch_gc dependency."""
    with ExitStack() as stack:
        for target in (
            "orchestune.dispatch_gc.is_process_alive",
            "orchestune.dispatch_gc_completion.is_process_alive",
            "orchestune.dispatch_gc_zombies.is_process_alive",
        ):
            stack.enter_context(patch(target, return_value=return_value))
        yield


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


def _issue(
    number,
    labels=("status:queued",),
    footprint=("src/foo.py",),
    symbols=("foo.Foo",),
    subtask_id="task-a",
    depends_on=(),
    created_at="2026-01-01T00:00:00+00:00",
    parent_number=181,
):
    """`tests/conftest.py`の`make_issue`に、このファイルの旧テスト群が前提と
    する`parent_number`（既定181）とtitleを合わせた薄いラッパー。"""
    parent = {"number": parent_number} if parent_number is not None else None
    return make_issue(
        number,
        title="t",
        labels=labels,
        footprint=footprint,
        symbols=symbols,
        subtask_id=subtask_id,
        depends_on=depends_on,
        created_at=created_at,
        parent=parent,
    )


class TestNotifyRecompute:
    def test_dry_run_reports_without_calling_github(self):
        conflict = FootprintConflict(
            subtask_id="task-a",
            other_subtask_id="task-b",
            similarity=0.5,
            blocked_subtask_id="task-b",
        )
        with (
            patch("orchestune.forge.GitHubForge.add_comment") as mock_comment,
            patch("orchestune.forge.GitHubForge.add_label") as mock_label,
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
            patch("orchestune.forge.GitHubForge.add_comment") as mock_comment,
            patch("orchestune.forge.GitHubForge.add_label") as mock_label,
            patch("orchestune.forge.GitHubForge.remove_label"),
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
            patch("orchestune.forge.GitHubForge.add_comment"),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
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
        with patch("orchestune.forge.GitHubForge.add_comment") as mock_comment:
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
        with patch("orchestune.forge.GitHubForge.add_comment") as mock_comment:
            notify_force_serial(
                "task-a",
                issue_number=1,
                parent_issue_number=181,
                retry_count=2,
                apply=True,
            )
        mock_comment.assert_called_once_with(181, ANY)

    def test_apply_without_parent_issue_skips_comment(self):
        with patch("orchestune.forge.GitHubForge.add_comment") as mock_comment:
            notify_force_serial(
                "task-a",
                issue_number=1,
                parent_issue_number=None,
                retry_count=2,
                apply=True,
            )
        mock_comment.assert_not_called()


class TestNotifyForceSerialWithFakeForge:
    """#293: `mock.patch`によるグローバルなクラスメソッド差し替えではなく、
    `forge`引数への注入だけでテストが書けることを示す。"""

    def test_uses_injected_fake_forge_instead_of_patching(self):
        fake_forge = MagicMock()

        notify_force_serial(
            "task-a",
            issue_number=1,
            parent_issue_number=181,
            retry_count=2,
            apply=True,
            forge=fake_forge,
        )

        fake_forge.add_comment.assert_called_once()
        assert fake_forge.add_comment.call_args.args[0] == 181


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

    def test_returncode_128_logs_warning_and_returns_false(self):
        with (
            patch("orchestune.dispatch_rebase.subprocess.run") as mock_run,
            patch(
                "orchestune.dispatch_rebase.resolve_local_or_remote_branch",
                return_value="nonexistent-branch",
            ),
            patch("orchestune.dispatch_rebase.logger.warning") as mock_warn,
        ):
            mock_run.return_value.returncode = 128
            mock_run.return_value.stderr = (
                "fatal: Not a valid object name nonexistent-branch"
            )
            assert (
                _decide_rebase_needed("nonexistent-branch", "feature", "worktrees/w1")
                is False
            )
            mock_warn.assert_called_once()
            assert "128" in mock_warn.call_args[0][0]

    def test_missing_ref_resolution_failure_logs_warning_and_returns_false(self):
        with (
            patch(
                "orchestune.dispatch_rebase.resolve_local_or_remote_branch",
                side_effect=ValueError("Invalid ref name"),
            ),
            patch("orchestune.dispatch_rebase.logger.warning") as mock_warn,
        ):
            assert (
                _decide_rebase_needed("invalid..ref", "feature", "worktrees/w1")
                is False
            )
            mock_warn.assert_called_once()
            assert "Failed to resolve branch" in mock_warn.call_args[0][0]

    def test_os_error_logs_warning_and_returns_false(self):
        with (
            patch(
                "orchestune.dispatch_rebase.subprocess.run",
                side_effect=OSError("command not found"),
            ),
            patch(
                "orchestune.dispatch_rebase.resolve_local_or_remote_branch",
                return_value="main",
            ),
            patch("orchestune.dispatch_rebase.logger.warning") as mock_warn,
        ):
            assert _decide_rebase_needed("main", "feature", "worktrees/w1") is False
            mock_warn.assert_called_once()
            assert "OSError" in mock_warn.call_args[0][0]


class TestTryAutoRebase:
    def test_rebase_not_needed_returns_false(self):
        active = _active(branch="feature")
        task = _task(depends_on=("task-parent",))

        done_subtask_ids = set()
        ci_passed_pr_subtask_ids = {"task-parent"}
        subtask_branch_map = {"task-parent": "parent-branch"}

        run_state = RunState(active_worktrees={})
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
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
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
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


class TestBranchStacking:
    def test_stacking_blocked_task_when_dependency_pr_ci_passes(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
        )
        blocked_issue = _issue(
            2, labels=("status:blocked",), subtask_id="task-2", depends_on=("task-1",)
        )
        parent_issue = _issue(1, labels=("status:in-progress",), subtask_id="task-1")

        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatch_cycle.list_remote_branches",
                return_value=["origin/claude/issue-1-task-1"],
            ),
            patch(
                "orchestune.forge.GitHubForge.list_open_prs",
                return_value=[
                    PrRecord(
                        number=10,
                        head_ref="claude/issue-1-task-1",
                        changed_files=("src/a.py",),
                        review_decision="APPROVED",
                        is_ci_passing=True,
                    )
                ],
            ),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_launch.create_worktree_and_launch"
            ) as mock_launch,
        ):
            mock_list.side_effect = lambda label, **_: (
                [blocked_issue]
                if label == "status:blocked"
                else [parent_issue]
                if label == "status:in-progress"
                else []
            )
            mock_launch.return_value = MagicMock(
                launched=True,
                pid=123,
                branch="claude/issue-2-task-2",
                worktree_path="worktrees/claude-issue-2-task-2",
                error_message=None,
                external_id=None,
                external_url=None,
                dispatch_started_at=1_700_000_000.0,
            )

            report = run_dispatch_cycle(config)

        mock_launch.assert_called_once_with(
            ANY,
            "claude/issue-2-task-2",
            ANY,
            ANY,
            apply=True,
            base_branch="claude/issue-1-task-1",
        )
        mock_remove_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:in-progress")
        assert len(report.selected) == 1

    def test_stacking_depth_limit_of_one(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=3,
            max_launches_per_window=3,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
        )
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")
        issue_b = _issue(
            2, labels=("status:blocked",), subtask_id="task-2", depends_on=("task-1",)
        )
        issue_c = _issue(
            3, labels=("status:blocked",), subtask_id="task-3", depends_on=("task-2",)
        )

        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatch_cycle.list_remote_branches",
                return_value=["origin/claude/issue-1-task-1"],
            ),
            patch(
                "orchestune.forge.GitHubForge.list_open_prs",
                return_value=[
                    PrRecord(
                        number=10,
                        head_ref="claude/issue-1-task-1",
                        changed_files=("src/a.py",),
                        review_decision="APPROVED",
                        is_ci_passing=True,
                    )
                ],
            ),
            patch("orchestune.forge.GitHubForge.add_label"),
            patch("orchestune.forge.GitHubForge.remove_label"),
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_launch.create_worktree_and_launch"
            ) as mock_launch,
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue_b, issue_c]
                if label == "status:blocked"
                else [issue_a]
                if label == "status:in-progress"
                else []
            )
            mock_launch.return_value = MagicMock(
                launched=True,
                pid=123,
                branch="claude/issue-2-task-2",
                worktree_path="worktrees/claude-issue-2-task-2",
                error_message=None,
                external_id=None,
                external_url=None,
                dispatch_started_at=1_700_000_000.0,
            )

            run_dispatch_cycle(config)

        mock_launch.assert_called_once_with(
            ANY,
            "claude/issue-2-task-2",
            ANY,
            ANY,
            apply=True,
            base_branch="claude/issue-1-task-1",
        )

    def test_auto_rebase_success(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
        )
        # BはAに依存。AはPR状態、Bは実行中（active_worktrees）
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")
        issue_b = _issue(
            2,
            labels=("status:in-progress",),
            subtask_id="task-2",
            depends_on=("task-1",),
        )

        run_state = RunState(
            active_worktrees={
                "2": ActiveWorktree(
                    issue_number=2,
                    branch="claude/issue-2-task-2",
                    worktree_path=str(tmp_path / "worktrees/claude-issue-2-task-2"),
                    pid=12345,
                    started_at=1700000000.0,
                    declared_footprint=(),
                )
            }
        )
        save_run_state(run_state, config.run_state_path)

        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatch_cycle.list_remote_branches",
                return_value=["origin/claude/issue-1-task-1"],
            ),
            patch(
                "orchestune.forge.GitHubForge.list_open_prs",
                return_value=[
                    PrRecord(
                        number=10,
                        head_ref="claude/issue-1-task-1",
                        changed_files=(),
                        review_decision="APPROVED",
                        is_ci_passing=True,
                    )
                ],
            ),
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation", return_value=[]
            ),
            patch("orchestune.forge.GitHubForge.add_label"),
            patch("orchestune.forge.GitHubForge.remove_label"),
            # os.kill と Popen のモック（リブートプロセスのため）
            patch("orchestune.dispatch_rebase.os.kill") as mock_kill,
            patch("orchestune.dispatch_worktree.subprocess.Popen") as mock_popen,
            # git コマンド実行のモック
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch(
                "orchestune.dispatch_rebase.resolve_local_or_remote_branch",
                return_value="claude/issue-1-task-1",
            ),
        ):

            def list_issues_by_label_mock(label, **_):
                if label == "status:in-progress":
                    return [issue_a, issue_b]
                return []

            mock_list.side_effect = list_issues_by_label_mock

            def kill_mock(pid, sig):
                if sig == 0:
                    raise ProcessLookupError()
                return None

            mock_kill.side_effect = kill_mock

            # subprocess.runのモック動作
            def run_mock(args, **kwargs):
                if "merge-base" in args:
                    return subprocess.CompletedProcess(
                        args=args, returncode=1, stdout="", stderr=""
                    )
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="", stderr=""
                )

            mock_run.side_effect = run_mock
            mock_popen.return_value.pid = 99999

            run_dispatch_cycle(config)

        # プロセスがkillされ、rebaseされ、再起動されたことを確認
        mock_kill.assert_any_call(12345, 9)  # SIGKILL (or SIGTERM)
        # rebase実行の引数チェック（#213: rebase前にWIP退避チェックのgit statusが
        # 挟まるため、"rebase"を含む呼び出しを探して検証する）
        rebase_call = next(c for c in mock_run.call_args_list if "rebase" in c.args[0])
        assert "claude/issue-1-task-1" in rebase_call.args[0]

        # 新しいPIDで状態が保存されていることを確認
        loaded = load_run_state(config.run_state_path)
        assert loaded.active_worktrees["2"].pid == 99999

    def test_stacking_blocked_when_multiple_dependencies_unmerged(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=3,
            max_launches_per_window=3,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
        )
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")
        issue_b = _issue(2, labels=("status:in-progress",), subtask_id="task-2")
        issue_c = _issue(
            3,
            labels=("status:blocked",),
            subtask_id="task-3",
            depends_on=("task-1", "task-2"),
        )

        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatch_cycle.list_remote_branches",
                return_value=[
                    "origin/claude/issue-1-task-1",
                    "origin/claude/issue-2-task-2",
                ],
            ),
            patch(
                "orchestune.forge.GitHubForge.list_open_prs",
                return_value=[
                    PrRecord(
                        number=10,
                        head_ref="claude/issue-1-task-1",
                        changed_files=("src/a.py",),
                        review_decision="APPROVED",
                        is_ci_passing=True,
                    ),
                    PrRecord(
                        number=11,
                        head_ref="claude/issue-2-task-2",
                        changed_files=("src/b.py",),
                        review_decision="APPROVED",
                        is_ci_passing=True,
                    ),
                ],
            ),
            patch("orchestune.forge.GitHubForge.add_label"),
            patch("orchestune.forge.GitHubForge.remove_label"),
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_launch.create_worktree_and_launch"
            ) as mock_launch,
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue_c]
                if label == "status:blocked"
                else [issue_a, issue_b]
                if label == "status:in-progress"
                else []
            )

            run_dispatch_cycle(config)

        mock_launch.assert_not_called()

    def test_stacking_blocked_task_when_dependency_completes_in_same_cycle(
        self, tmp_path
    ):
        config = DispatcherConfig(
            max_concurrent=3,
            max_launches_per_window=3,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
        )
        # タスクC(3) は タスクB(2) に依存、タスクB(2) は タスクA(1) に依存
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")
        issue_b = _issue(
            2, labels=("status:blocked",), subtask_id="task-2", depends_on=("task-1",)
        )
        issue_c = _issue(
            3, labels=("status:blocked",), subtask_id="task-3", depends_on=("task-2",)
        )

        # タスクA（issue 1）は active_worktrees に登録されており、このサイクルで完了する
        run_state = RunState(
            active_worktrees={
                "1": ActiveWorktree(
                    issue_number=1,
                    branch="claude/issue-1-task-1",
                    worktree_path=str(tmp_path / "worktrees/claude-issue-1-task-1"),
                    pid=123,
                    started_at=1700000000.0,
                    declared_footprint=(),
                    base_branch="origin/main",
                )
            }
        )
        save_run_state(run_state, config.run_state_path)

        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatch_cycle.list_remote_branches",
                return_value=[
                    "origin/claude/issue-1-task-1",
                    "origin/claude/issue-2-task-2",
                ],
            ),
            patch(
                "orchestune.forge.GitHubForge.list_open_prs",
                return_value=[
                    PrRecord(
                        number=11,
                        head_ref="claude/issue-2-task-2",
                        changed_files=("src/b.py",),
                        review_decision="APPROVED",
                        is_ci_passing=True,  # 依存先BのPRはCI通過済み
                    )
                ],
            ),
            # #292: このシナリオのラベル遷移はdispatch_cycleの
            # _promote_blocked_tasks（Forge注入経由）が行うため、
            # dispatch_rebase.github経由ではなくGitHubForge側をパッチする。
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch(
                "orchestune.dispatch_launch.create_worktree_and_launch"
            ) as mock_launch,
            # タスクAの完了判定とGC処理のためのモック
            patch("orchestune.dispatch_gc._is_worktree_complete", return_value=True),
            # Completion now also consults the all-state PR list to rule out
            # an abandoned (closed-unmerged) PR before finalizing.
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch(
                "orchestune.dispatch_gc._finalize_completed_worktree",
                return_value={
                    "action": "completed",
                    "issue_number": 1,
                    "subtask_id": "task-1",
                    "commit_sha": "abc1234",
                },
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue_b, issue_c]
                if label == "status:blocked"
                else [issue_a]
                if label == "status:in-progress"
                else []
            )
            mock_launch.return_value = MagicMock(
                launched=True,
                pid=456,
                branch="claude/issue-3-task-3",
                worktree_path="worktrees/claude-issue-3-task-3",
                error_message=None,
                external_id=None,
                external_url=None,
                dispatch_started_at=1_700_000_000.0,
            )

            report = run_dispatch_cycle(config)

        # 同一サイクル内で依存先Aが完了し、かつBのPRがCI通過済みのため、
        # タスクC（issue 3）がタスクBのブランチをベースにスタッキング起動される
        mock_launch.assert_called_once_with(
            ANY,
            "claude/issue-3-task-3",
            ANY,
            ANY,
            apply=True,
            base_branch="claude/issue-2-task-2",
        )
        mock_remove_label.assert_any_call(3, "status:blocked")
        mock_add_label.assert_any_call(3, "status:in-progress")
        assert len(report.selected) == 1

    def test_auto_rebase_conflict(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
        )
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")
        issue_b = _issue(
            2,
            labels=("status:in-progress",),
            subtask_id="task-2",
            depends_on=("task-1",),
        )

        run_state = RunState(
            active_worktrees={
                "2": ActiveWorktree(
                    issue_number=2,
                    branch="claude/issue-2-task-2",
                    worktree_path=str(tmp_path / "worktrees/claude-issue-2-task-2"),
                    pid=12345,
                    started_at=1700000000.0,
                    declared_footprint=(),
                )
            }
        )
        save_run_state(run_state, config.run_state_path)

        with (
            patch(
                "orchestune.forge.GitHubForge.list_issues_by_label",
                side_effect=lambda label, **_: (
                    [issue_a, issue_b] if label == "status:in-progress" else []
                ),
            ),
            patch(
                "orchestune.dispatch_cycle.list_remote_branches",
                return_value=["origin/claude/issue-1-task-1"],
            ),
            patch(
                "orchestune.forge.GitHubForge.list_open_prs",
                return_value=[
                    PrRecord(
                        number=10,
                        head_ref="claude/issue-1-task-1",
                        changed_files=(),
                        review_decision="APPROVED",
                        is_ci_passing=True,
                    )
                ],
            ),
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation", return_value=[]
            ),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
            patch("orchestune.dispatch_rebase.os.kill") as mock_kill,
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch(
                "orchestune.dispatch_rebase.resolve_local_or_remote_branch",
                return_value="claude/issue-1-task-1",
            ),
        ):
            mock_kill.side_effect = lambda pid, sig: (
                ProcessLookupError() if sig == 0 else None
            )
            # 1. git merge-base -> 戻り値 1
            # 2. git status --porcelain (#213: rebase前のWIP退避チェック) -> clean
            # 3. git rebase -> 戻り値 128 (競合発生で失敗)
            # 4. git rebase --abort -> 戻り値 0
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
                subprocess.CalledProcessError(returncode=128, cmd="git rebase"),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ]

            run_dispatch_cycle(config)

        # rebase abort が呼ばれたこと
        abort_call = mock_run.call_args_list[3]
        assert "--abort" in abort_call.args[0]

        # 安全停止（ラベル遷移）が行われたこと
        mock_remove_label.assert_any_call(2, "status:in-progress")
        mock_add_label.assert_any_call(2, "status:manual-merge-required")
        mock_add_comment.assert_called_once()

        # active_worktrees から除外されたこと（worktree削除はしない）
        loaded = load_run_state(config.run_state_path)
        assert "2" not in loaded.active_worktrees

    def test_changes_requested_escalation(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
        )
        # BはAに依存。AはPR状態(CHANGES_REQUESTED)、Bは実行中（active_worktrees）
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")
        issue_b = _issue(
            2,
            labels=("status:in-progress",),
            subtask_id="task-2",
            depends_on=("task-1",),
        )

        run_state = RunState(
            active_worktrees={
                "2": ActiveWorktree(
                    issue_number=2,
                    branch="claude/issue-2-task-2",
                    worktree_path=str(tmp_path / "worktrees/claude-issue-2-task-2"),
                    pid=12345,
                    started_at=1700000000.0,
                    declared_footprint=(),
                )
            }
        )
        save_run_state(run_state, config.run_state_path)

        with (
            patch(
                "orchestune.forge.GitHubForge.list_issues_by_label",
                side_effect=lambda label, **_: (
                    [issue_a, issue_b] if label == "status:in-progress" else []
                ),
            ),
            patch(
                "orchestune.dispatch_cycle.list_remote_branches",
                return_value=["origin/claude/issue-1-task-1"],
            ),
            patch(
                "orchestune.forge.GitHubForge.list_open_prs",
                return_value=[
                    PrRecord(
                        number=10,
                        head_ref="claude/issue-1-task-1",
                        changed_files=(),
                        review_decision="CHANGES_REQUESTED",  # ここがポイント
                        is_ci_passing=True,
                    )
                ],
            ),
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation", return_value=[]
            ),
            # #292: CHANGES_REQUESTEDエスカレーションはdispatch_escalationの
            # apply_human_review_escalationがForge注入経由で呼ぶため、
            # dispatch_rebase.github経由ではなくGitHubForge側をパッチする。
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
            patch("orchestune.dispatch_rebase.os.kill") as mock_kill,
            patch("orchestune.dispatch_worktree.subprocess.run"),
        ):
            run_dispatch_cycle(config)

        # プロセスがkillされたこと
        mock_kill.assert_called_with(12345, 9)

        # エスカレーションラベル付与
        mock_remove_label.assert_any_call(2, "status:in-progress")
        mock_add_label.assert_any_call(2, "status:blocked-human-review")
        mock_add_comment.assert_called_once()
        assert "一時停止" in mock_add_comment.call_args[0][1]

        # active_worktrees から除外されたこと
        loaded = load_run_state(config.run_state_path)
        assert "2" not in loaded.active_worktrees
