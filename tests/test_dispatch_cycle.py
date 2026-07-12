from pathlib import Path
from unittest.mock import patch

from orchestune.dispatch_cycle import (
    CycleContext,
    _decide_blocked_promotions,
    _decide_external_lock_sync,
    _process_active_worktrees,
)
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree, RunState
from orchestune.dispatcher import DispatcherConfig
from orchestune.github import IssueRecord


def _task(**overrides):
    defaults = dict(
        issue_number=1,
        subtask_id="task-a",
        footprint=(),
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
        declared_footprint=(),
    )
    defaults.update(overrides)
    return ActiveWorktree(**defaults)


def _ctx(**overrides):
    defaults = dict(
        run_state=RunState(active_worktrees={}),
        tasks_by_issue={},
        issue_number_by_subtask_id={},
        done_subtask_ids=set(),
        ci_passed_pr_subtask_ids=set(),
        changes_requested_subtask_ids=set(),
        subtask_branch_map={},
        prs=[],
        pr_by_branch={},
        config=DispatcherConfig(run_state_path="dummy.json", worktree_root="worktrees"),
    )
    defaults.update(overrides)
    return CycleContext(**defaults)


class TestDecideBlockedPromotions:
    """decide層: 依存解決済みタスクの判定のみを行い、githubラベルは変更しない。"""

    def test_no_depends_on_is_not_promotable(self):
        task = _task(depends_on=())
        promotable = _decide_blocked_promotions([], [], set(), {1: task})
        assert promotable == []

    def test_unresolved_dependency_is_not_promotable(self):
        task = _task(depends_on=("task-x",))
        issue = IssueRecord(
            number=1, title="t", body="", labels=(), created_at="2026-01-01T00:00:00Z"
        )
        promotable = _decide_blocked_promotions([issue], [], set(), {1: task})
        assert promotable == []

    def test_resolved_via_completed_subtask_ids_is_promotable(self):
        task = _task(depends_on=("task-x",))
        issue = IssueRecord(
            number=1, title="t", body="", labels=(), created_at="2026-01-01T00:00:00Z"
        )
        promotable = _decide_blocked_promotions([issue], [], {"task-x"}, {1: task})
        assert promotable == [task]


class TestDecideExternalLockSync:
    """decide層: githubからの読み取りとscan_external_locksの純粋計算のみを行い、
    ラベルの書き込みは行わない。"""

    def test_no_bare_branches_means_no_locks(self):
        run_state = RunState(active_worktrees={})
        with (
            patch(
                "orchestune.dispatch_cycle.github.list_remote_branches",
                return_value=[],
            ),
        ):
            result = _decide_external_lock_sync({}, [], run_state)
        assert result.to_lock == []
        assert result.to_unlock == []


class TestProcessActiveWorktrees:
    """_process_active_worktreesの結合テストケース。

    各ruleの中身(条件判定=decide/実処理=act)は対応するact側モジュール
    (dispatch_gc/dispatch_escalation/dispatch_rebase)に定義されているため、
    patch対象はそれらのモジュールを指す（#86のComposite化に伴う移設）。
    """

    def test_auto_rebase_not_needed_falls_through_to_footprint_deviation(self):
        active = _active(
            branch="feature",
            declared_footprint=("a.py",),
            worktree_path="worktrees/w1",
            recompute_count=0,
        )
        task = _task(
            issue_number=1,
            subtask_id="task-child",
            footprint=("a.py",),
            depends_on=("task-parent",),
        )
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(
            run_state=run_state,
            tasks_by_issue={1: task},
            ci_passed_pr_subtask_ids={"task-parent"},
            subtask_branch_map={"task-parent": "parent-branch"},
        )

        with (
            patch(
                "orchestune.dispatch_gc._is_worktree_complete",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc.is_process_alive",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_rebase._decide_rebase_needed",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                return_value=["b.py"],
            ),
            patch(
                "orchestune.dispatch_rebase._handle_footprint_deviation",
                return_value={
                    "action": "recomputed",
                    "issue_number": 1,
                    "deviated_files": ["b.py"],
                },
            ),
        ):
            (
                completion_events,
                deviation_events,
                any_forced_serial,
                completed_subtask_ids,
            ) = _process_active_worktrees(ctx)

        assert completion_events == []
        assert len(deviation_events) == 1
        assert deviation_events[0]["action"] == "recomputed"
        assert deviation_events[0]["deviated_files"] == ["b.py"]
        assert any_forced_serial is False
        assert completed_subtask_ids == set()

    def test_dirty_worktree_skips_completion_and_does_not_fall_through(self):
        active = _active(
            branch="feature",
            declared_footprint=("a.py",),
            worktree_path="worktrees/w1",
        )
        task = _task(
            issue_number=1,
            subtask_id="task-child",
            footprint=("a.py",),
            depends_on=("task-parent",),
        )
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(
            run_state=run_state,
            tasks_by_issue={1: task},
            ci_passed_pr_subtask_ids={"task-parent"},
            subtask_branch_map={"task-parent": "parent-branch"},
        )

        with (
            patch(
                "orchestune.dispatch_gc._is_worktree_complete",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_gc._finalize_completed_worktree",
                return_value={"action": "completion_skipped_dirty_worktree"},
            ),
            patch(
                "orchestune.dispatch_rebase._try_auto_rebase",
                side_effect=AssertionError("Should not call auto rebase"),
            ),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                side_effect=AssertionError("Should not call check footprint deviation"),
            ),
        ):
            (
                completion_events,
                deviation_events,
                any_forced_serial,
                completed_subtask_ids,
            ) = _process_active_worktrees(ctx)

        assert len(completion_events) == 1
        assert completion_events[0]["action"] == "completion_skipped_dirty_worktree"
        assert deviation_events == []
        assert any_forced_serial is False
        assert completed_subtask_ids == set()

    def test_not_needed_label_takes_precedence_over_stale_entry(self):
        active = _active(issue_number=1)
        task = _task(
            issue_number=1,
            status_labels=("status:not-needed", "status:blocked"),
        )
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(
            run_state=run_state,
            tasks_by_issue={1: task},
            config=DispatcherConfig(
                run_state_path=Path("dummy.json"),
                worktree_root=Path("worktrees"),
                apply=True,
            ),
        )

        with (
            patch(
                "orchestune.dispatch_gc._finalize_not_needed_worktree",
                return_value={"action": "not_needed"},
            ),
            patch(
                "orchestune.dispatch_gc._decide_stale_active_entry",
                side_effect=AssertionError("Should not call decide stale active entry"),
            ),
        ):
            (
                completion_events,
                deviation_events,
                any_forced_serial,
                completed_subtask_ids,
            ) = _process_active_worktrees(ctx)

        assert len(completion_events) == 1
        assert completion_events[0]["action"] == "not_needed"
        assert deviation_events == []
        assert completed_subtask_ids == {task.subtask_id}
        assert "1" not in ctx.run_state.active_worktrees

    def test_auto_rebase_failure_discards_active_entry(self):
        active = _active(
            branch="feature",
            worktree_path="worktrees/w1",
            pid=123,
        )
        task = _task(
            issue_number=1,
            subtask_id="task-child",
            depends_on=("task-parent",),
        )
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(
            run_state=run_state,
            tasks_by_issue={1: task},
            ci_passed_pr_subtask_ids={"task-parent"},
            subtask_branch_map={"task-parent": "parent-branch"},
            config=DispatcherConfig(
                run_state_path=Path("dummy.json"),
                worktree_root=Path("worktrees"),
                apply=True,
            ),
        )

        def mock_subprocess_run(args, **kwargs):
            import subprocess

            if "rebase" in args:
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0)

        with (
            patch(
                "orchestune.dispatch_gc._is_worktree_complete",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc.is_process_alive",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_rebase._decide_rebase_needed",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_rebase.subprocess.run",
                side_effect=mock_subprocess_run,
            ),
            patch("orchestune.dispatch_rebase.github.remove_label") as mock_remove,
            patch("orchestune.dispatch_rebase.github.add_label") as mock_add,
            patch("orchestune.dispatch_rebase.github.add_comment") as mock_comment,
        ):
            (
                completion_events,
                deviation_events,
                any_forced_serial,
                completed_subtask_ids,
            ) = _process_active_worktrees(ctx)

        assert completion_events == []
        assert deviation_events == []
        assert completed_subtask_ids == set()
        assert "1" not in ctx.run_state.active_worktrees
        mock_remove.assert_called_once_with(1, "status:in-progress")
        mock_add.assert_called_once_with(1, "status:manual-merge-required")
        mock_comment.assert_called_once_with(
            1,
            "自動リベース中にコンフリクトが発生しました。手動でマージを行ってください。\n対象の依存元ブランチ: parent-branch",
        )

    def test_forced_serial_persists_with_early_termination_rules(self):
        active = _active(
            issue_number=1,
            forced_serial=True,
        )
        task = _task(
            issue_number=1,
            status_labels=("status:not-needed",),
        )
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(
            run_state=run_state,
            tasks_by_issue={1: task},
            config=DispatcherConfig(
                run_state_path=Path("dummy.json"),
                worktree_root=Path("worktrees"),
                apply=True,
            ),
        )

        with (
            patch(
                "orchestune.dispatch_gc._finalize_not_needed_worktree",
                return_value={"action": "not_needed"},
            ),
        ):
            (
                completion_events,
                deviation_events,
                any_forced_serial,
                completed_subtask_ids,
            ) = _process_active_worktrees(ctx)

        assert len(completion_events) == 1
        assert completion_events[0]["action"] == "not_needed"
        assert deviation_events == []
        assert completed_subtask_ids == {task.subtask_id}
        assert any_forced_serial is False
        assert "1" not in ctx.run_state.active_worktrees

        # 追加検証：もう1つの worktree があって、そちらは早期終了せず forced_serial=True の場合
        active_early = _active(issue_number=1, forced_serial=True)
        active_keep = _active(issue_number=2, forced_serial=True)
        task_early = _task(issue_number=1, status_labels=("status:not-needed",))
        task_keep = _task(issue_number=2, status_labels=("status:in-progress",))

        run_state_two = RunState(active_worktrees={"1": active_early, "2": active_keep})
        ctx_two = _ctx(
            run_state=run_state_two,
            tasks_by_issue={1: task_early, 2: task_keep},
            config=DispatcherConfig(
                run_state_path=Path("dummy.json"),
                worktree_root=Path("worktrees"),
                apply=True,
            ),
        )

        with (
            patch(
                "orchestune.dispatch_gc._finalize_not_needed_worktree",
                return_value={"action": "not_needed"},
            ),
            patch(
                "orchestune.dispatch_gc._is_worktree_complete",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc.is_process_alive",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_rebase._decide_rebase_needed",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                return_value=[],
            ),
        ):
            (
                completion_events,
                deviation_events,
                any_forced_serial,
                completed_subtask_ids,
            ) = _process_active_worktrees(ctx_two)

        assert any_forced_serial is True
        assert "1" not in ctx_two.run_state.active_worktrees
