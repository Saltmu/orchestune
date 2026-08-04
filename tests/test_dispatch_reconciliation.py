"""dispatch_reconciliation.py の復元・整合性修復に関する境界値テスト (#337)。

`_collect_active_conflict_subtask_ids` / `_decide_blocked_promotions` /
`_handle_blocked_recompute_recovery` は既存の `tests/test_dispatch_cycle.py`
では実質未検証だったため、本ファイルで単体テストとして完結させる。
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestune.dag import FootprintConflict
from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_reconciliation import (
    _apply_blocked_promotions,
    _collect_active_conflict_subtask_ids,
    _decide_blocked_promotions,
    _handle_blocked_recompute_recovery,
    _promote_blocked_tasks,
    _reconcile_dual_status_tasks,
    _self_heal_run_state,
)
from orchestune.dispatch_rules import CycleContext
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree, RunState
from orchestune.models import IssueRecord

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-reconciliation-"))


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
        config=DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        ),
    )
    defaults.update(overrides)
    return CycleContext(**defaults)


def _issue(number, labels=(), state="OPEN"):
    return IssueRecord(
        number=number,
        title=f"Issue {number}",
        body="",
        labels=labels,
        created_at="2026-01-01T00:00:00+00:00",
        state=state,
    )


class _IssuesStub:
    """`_handle_blocked_recompute_recovery`が要求する`.all()`のみを持つ最小スタブ。"""

    def __init__(self, issues):
        self._issues = list(issues)

    def all(self):
        return list(self._issues)


class TestCollectActiveConflictSubtaskIds:
    def test_skips_active_worktree_without_matching_task(self):
        active = _active(issue_number=99)
        run_state = RunState(active_worktrees={"w1": active})
        ctx = _ctx(tasks_by_issue={})
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch(
            "orchestune.dispatch_reconciliation.check_footprint_deviation",
            side_effect=AssertionError("タスク未対応のactiveは検査すべきではない"),
        ):
            result = _collect_active_conflict_subtask_ids(run_state, ctx, {}, config)

        assert result == set()

    def test_skips_active_worktree_when_task_has_no_subtask_id(self):
        task = _task(issue_number=1, subtask_id="")
        active = _active(issue_number=1)
        run_state = RunState(active_worktrees={"w1": active})
        ctx = _ctx(tasks_by_issue={1: task})
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch(
            "orchestune.dispatch_reconciliation.check_footprint_deviation",
            side_effect=AssertionError("subtask_id未設定のactiveは検査すべきではない"),
        ):
            result = _collect_active_conflict_subtask_ids(run_state, ctx, {}, config)

        assert result == set()

    def test_fail_closed_when_deviation_undetectable(self):
        """deviatedがNone（検出不能エラー）の場合は全サブタスクを競合中として扱う。"""
        task = _task(issue_number=1, subtask_id="task-a")
        active = _active(issue_number=1)
        run_state = RunState(active_worktrees={"w1": active})
        ctx = _ctx(tasks_by_issue={1: task})
        subtasks_for_recompute = {"task-a": object(), "task-b": object()}
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch(
            "orchestune.dispatch_reconciliation.check_footprint_deviation",
            return_value=None,
        ):
            result = _collect_active_conflict_subtask_ids(
                run_state, ctx, subtasks_for_recompute, config
            )

        assert result == {"task-a", "task-b"}

    def test_adds_blocked_subtask_ids_from_recomputed_conflicts(self):
        task = _task(issue_number=1, subtask_id="task-a", footprint=("a.py",))
        active = _active(
            issue_number=1,
            declared_footprint=("a.py",),
            worktree_path="worktrees/w1",
        )
        run_state = RunState(active_worktrees={"w1": active})
        ctx = _ctx(tasks_by_issue={1: task})
        subtasks_for_recompute = {"task-a": object(), "task-b": object()}
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        conflicts = [
            FootprintConflict(
                subtask_id="task-a",
                other_subtask_id="task-b",
                similarity=0.9,
                blocked_subtask_id="task-b",
            )
        ]

        with (
            patch(
                "orchestune.dispatch_reconciliation.check_footprint_deviation",
                return_value=["b.py"],
            ),
            patch(
                "orchestune.dispatch_reconciliation.recompute_dag_for_footprint_change",
                return_value=(MagicMock(), conflicts),
            ) as mock_recompute,
        ):
            result = _collect_active_conflict_subtask_ids(
                run_state, ctx, subtasks_for_recompute, config
            )

        assert result == {"task-b"}
        mock_recompute.assert_called_once_with(
            subtasks_for_recompute,
            "task-a",
            updated_footprint=("a.py", "b.py"),
        )

    def test_conflict_without_blocked_subtask_id_is_ignored(self):
        task = _task(issue_number=1, subtask_id="task-a")
        active = _active(issue_number=1)
        run_state = RunState(active_worktrees={"w1": active})
        ctx = _ctx(tasks_by_issue={1: task})
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        conflicts = [
            FootprintConflict(
                subtask_id="task-a",
                other_subtask_id="task-c",
                similarity=0.9,
                blocked_subtask_id="",
            )
        ]

        with (
            patch(
                "orchestune.dispatch_reconciliation.check_footprint_deviation",
                return_value=["c.py"],
            ),
            patch(
                "orchestune.dispatch_reconciliation.recompute_dag_for_footprint_change",
                return_value=(MagicMock(), conflicts),
            ),
        ):
            result = _collect_active_conflict_subtask_ids(run_state, ctx, {}, config)

        assert result == set()

    def test_fail_closed_when_recompute_raises(self):
        task = _task(issue_number=1, subtask_id="task-a")
        active = _active(issue_number=1)
        run_state = RunState(active_worktrees={"w1": active})
        ctx = _ctx(tasks_by_issue={1: task})
        subtasks_for_recompute = {"task-a": object(), "task-b": object()}
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with (
            patch(
                "orchestune.dispatch_reconciliation.check_footprint_deviation",
                return_value=["b.py"],
            ),
            patch(
                "orchestune.dispatch_reconciliation.recompute_dag_for_footprint_change",
                side_effect=RuntimeError("DAG再計算エラー"),
            ),
        ):
            result = _collect_active_conflict_subtask_ids(
                run_state, ctx, subtasks_for_recompute, config
            )

        assert result == {"task-a", "task-b"}


class TestDecideBlockedPromotions:
    def test_issue_without_matching_task_is_skipped(self):
        promotable = _decide_blocked_promotions([_issue(1)], [], set(), {})
        assert promotable == []

    def test_issue_with_task_missing_depends_on_is_skipped(self):
        task = _task(issue_number=1, depends_on=())
        promotable = _decide_blocked_promotions([_issue(1)], [], set(), {1: task})
        assert promotable == []


class TestApplyBlockedPromotions:
    def test_dry_run_returns_events_without_calling_github(self):
        task = _task(issue_number=5, subtask_id="task-e")
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=False,
        )

        with (
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add,
        ):
            events = _apply_blocked_promotions([task], config)

        mock_remove.assert_not_called()
        mock_add.assert_not_called()
        assert events == [{"issue_number": 5, "subtask_id": "task-e"}]

    def test_apply_swaps_blocked_label_for_queued(self):
        task = _task(issue_number=5, subtask_id="task-e")
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )

        with (
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add,
        ):
            events = _apply_blocked_promotions([task], config)

        mock_remove.assert_called_once_with(5, "status:blocked")
        mock_add.assert_called_once_with(5, "status:queued")
        assert events == [{"issue_number": 5, "subtask_id": "task-e"}]


class TestPromoteBlockedTasks:
    def test_decide_and_apply_are_wired_together(self):
        task = _task(issue_number=1, subtask_id="task-a", depends_on=("task-x",))
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )

        with (
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add,
        ):
            events = _promote_blocked_tasks(
                [_issue(1)], [], {"task-x"}, {1: task}, config
            )

        mock_remove.assert_called_once_with(1, "status:blocked")
        mock_add.assert_called_once_with(1, "status:queued")
        assert events == [{"issue_number": 1, "subtask_id": "task-a"}]


class TestSelfHealRunState:
    def test_persists_recovered_state_when_run_state_missing(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        config = DispatcherConfig(
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )
        run_state = RunState(active_worktrees={})

        with (
            patch(
                "orchestune.forge.GitHubForge.list_issues_by_label",
                return_value=[],
            ),
            patch(
                "orchestune.dispatch_reconciliation.recover_run_state",
                return_value=True,
            ),
            patch("orchestune.dispatch_reconciliation.save_run_state") as mock_save,
        ):
            _self_heal_run_state(run_state, config)

        mock_save.assert_called_once_with(
            run_state,
            config.run_state_path,
            launch_window_seconds=config.window_seconds,
        )

    def test_does_not_persist_when_recovery_reports_no_change(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )
        run_state = RunState(active_worktrees={})

        with (
            patch(
                "orchestune.forge.GitHubForge.list_issues_by_label",
                return_value=[],
            ),
            patch(
                "orchestune.dispatch_reconciliation.recover_run_state",
                return_value=False,
            ),
            patch("orchestune.dispatch_reconciliation.save_run_state") as mock_save,
        ):
            _self_heal_run_state(run_state, config)

        mock_save.assert_not_called()


class TestDualStatusReconciliationMultipleTasks:
    def test_only_dual_status_tasks_are_reconciled_among_several(self):
        """複数タスクが混在する中で、status:done/status:queuedの重複を持つ
        タスクのみが個別に整合修復されることを確認する（境界値: 複数ラベル重複）。"""
        dual_a = _task(
            issue_number=1,
            subtask_id="task-a",
            status_labels=("status:done", "status:queued"),
        )
        dual_b = _task(
            issue_number=2,
            subtask_id="task-b",
            status_labels=("status:done", "status:queued"),
        )
        queued_only = _task(
            issue_number=3,
            subtask_id="task-c",
            status_labels=("status:queued",),
        )
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )

        with patch("orchestune.forge.GitHubForge.remove_label") as mock_remove:
            events = _reconcile_dual_status_tasks(
                {1: dual_a, 2: dual_b, 3: queued_only}, config
            )

        assert mock_remove.call_args_list == [
            ((1, "status:done"),),
            ((2, "status:done"),),
        ]
        assert events == [
            {"issue_number": 1, "subtask_id": "task-a"},
            {"issue_number": 2, "subtask_id": "task-b"},
        ]


class TestHandleBlockedRecomputeRecovery:
    def test_returns_empty_when_no_blocked_recompute_issues(self):
        run_state = RunState(active_worktrees={})
        ctx = _ctx()
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        result = _handle_blocked_recompute_recovery(
            _IssuesStub([_issue(1, labels=("status:queued",))]),
            run_state,
            ctx,
            set(),
            config,
        )

        assert result == []

    def test_issue_without_matching_task_is_skipped(self):
        run_state = RunState(active_worktrees={})
        ctx = _ctx(tasks_by_issue={})
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )

        with (
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add,
        ):
            result = _handle_blocked_recompute_recovery(
                _IssuesStub(
                    [_issue(1, labels=("status:blocked-recompute", "status:blocked"))]
                ),
                run_state,
                ctx,
                set(),
                config,
            )

        mock_remove.assert_not_called()
        mock_add.assert_not_called()
        assert result == []

    def test_dry_run_returns_event_without_calling_github(self):
        task = _task(
            issue_number=1, subtask_id="task-a", depends_on=(), status_labels=()
        )
        run_state = RunState(active_worktrees={})
        ctx = _ctx(tasks_by_issue={1: task})
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=False,
        )

        with (
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add,
        ):
            result = _handle_blocked_recompute_recovery(
                _IssuesStub(
                    [_issue(1, labels=("status:blocked-recompute", "status:blocked"))]
                ),
                run_state,
                ctx,
                set(),
                config,
            )

        mock_remove.assert_not_called()
        mock_add.assert_not_called()
        assert result == [{"issue_number": 1, "subtask_id": "task-a"}]

    def test_apply_promotes_when_dependencies_are_resolved(self):
        task = _task(
            issue_number=1,
            subtask_id="task-a",
            depends_on=("task-x",),
            status_labels=(),
        )
        run_state = RunState(active_worktrees={})
        ctx = _ctx(tasks_by_issue={1: task}, done_subtask_ids={"task-x"})
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )

        with (
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add,
        ):
            result = _handle_blocked_recompute_recovery(
                _IssuesStub(
                    [_issue(1, labels=("status:blocked-recompute", "status:blocked"))]
                ),
                run_state,
                ctx,
                set(),
                config,
            )

        assert mock_remove.call_args_list == [
            ((1, "status:blocked-recompute"),),
            ((1, "status:blocked"),),
        ]
        mock_add.assert_called_once_with(1, "status:queued")
        assert result == [{"issue_number": 1, "subtask_id": "task-a"}]

    def test_stays_blocked_when_dependency_still_pending(self):
        task = _task(
            issue_number=1,
            subtask_id="task-a",
            depends_on=("task-x",),
            status_labels=(),
        )
        run_state = RunState(active_worktrees={})
        ctx = _ctx(tasks_by_issue={1: task}, done_subtask_ids=set())
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )

        with (
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add,
        ):
            result = _handle_blocked_recompute_recovery(
                _IssuesStub(
                    [_issue(1, labels=("status:blocked-recompute", "status:blocked"))]
                ),
                run_state,
                ctx,
                set(),
                config,
            )

        mock_remove.assert_called_once_with(1, "status:blocked-recompute")
        mock_add.assert_not_called()
        assert result == []

    def test_dependency_resolved_via_completed_subtask_ids(self):
        """`completed_subtask_ids`（status:not-neededを含む解決経路）でも
        依存解決とみなされることを確認する。"""
        task = _task(
            issue_number=1,
            subtask_id="task-a",
            depends_on=("task-x",),
            status_labels=(),
        )
        run_state = RunState(active_worktrees={})
        ctx = _ctx(tasks_by_issue={1: task}, done_subtask_ids=set())
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )

        with (
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add,
        ):
            result = _handle_blocked_recompute_recovery(
                _IssuesStub(
                    [_issue(1, labels=("status:blocked-recompute", "status:blocked"))]
                ),
                run_state,
                ctx,
                {"task-x"},
                config,
            )

        assert mock_remove.call_args_list == [
            ((1, "status:blocked-recompute"),),
            ((1, "status:blocked"),),
        ]
        mock_add.assert_called_once_with(1, "status:queued")
        assert result == [{"issue_number": 1, "subtask_id": "task-a"}]
