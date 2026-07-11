from pathlib import Path
from unittest.mock import patch

from orchestune.dispatch_cycle import (
    ActiveWorktreeRuleOutcome,
    CycleContext,
    _ActiveWorktreeAggregates,
    _decide_blocked_promotions,
    _decide_changes_requested_escalation,
    _decide_external_lock_sync,
    _decide_stale_active_entry,
    _process_active_worktrees,
    _rule_changes_requested,
    _rule_completed,
    _run_active_worktree_rules,
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


class TestDecideStaleActiveEntry:
    """decide層: githubラベルの読み取りのみでstale判定を行い、run_stateは変更しない。"""

    def test_none_when_still_in_progress(self):
        task = _task(status_labels=("status:in-progress",))
        assert _decide_stale_active_entry(_active(), task) is None

    def test_none_when_no_matching_task(self):
        assert _decide_stale_active_entry(_active(), None) is None

    def test_stale_when_label_no_longer_in_progress(self):
        task = _task(status_labels=("status:blocked",))
        event = _decide_stale_active_entry(_active(), task)
        assert event is not None
        assert event["action"] == "stale_active_entry_discarded"


class TestDecideChangesRequestedEscalation:
    def test_false_when_no_depends_on(self):
        assert (
            _decide_changes_requested_escalation(_task(depends_on=()), set()) is False
        )

    def test_false_when_dependency_not_changes_requested(self):
        task = _task(depends_on=("task-x",))
        assert _decide_changes_requested_escalation(task, set()) is False

    def test_true_when_dependency_changes_requested(self):
        task = _task(depends_on=("task-x",))
        assert _decide_changes_requested_escalation(task, {"task-x"}) is True


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


class TestRunActiveWorktreeRules:
    """ルールチェーン実行エンジン自体の振る舞い（#86）。

    新しい判断パターンは`_rule_*`関数を1つ書いてリストに追加するだけで済む
    ことを裏付けるため、ここではルールの中身に依存しない汎用的な挙動
    （None/terminal/non-terminalの扱い）のみを検証する。
    """

    def test_none_result_tries_next_rule(self):
        calls = []

        def _rule_a(ctx, key, active, active_task):
            calls.append("a")
            return None

        def _rule_b(ctx, key, active, active_task):
            calls.append("b")
            return ActiveWorktreeRuleOutcome(terminal=True)

        aggregates = _ActiveWorktreeAggregates()
        handled = _run_active_worktree_rules(
            [_rule_a, _rule_b], _ctx(), "1", _active(), None, aggregates
        )
        assert handled is True
        assert calls == ["a", "b"]

    def test_terminal_result_stops_the_chain(self):
        calls = []

        def _rule_a(ctx, key, active, active_task):
            calls.append("a")
            return ActiveWorktreeRuleOutcome(terminal=True)

        def _rule_b(ctx, key, active, active_task):
            calls.append("b")
            return ActiveWorktreeRuleOutcome(terminal=True)

        aggregates = _ActiveWorktreeAggregates()
        handled = _run_active_worktree_rules(
            [_rule_a, _rule_b], _ctx(), "1", _active(), None, aggregates
        )
        assert handled is True
        assert calls == ["a"]

    def test_non_terminal_result_falls_through_to_next_rule(self):
        """「記録はするが処理は継続する」ケースを
        汎用的なNone/terminal/non-terminalの組み合わせで再現する。"""
        calls = []

        def _rule_a(ctx, key, active, active_task):
            calls.append("a")
            return ActiveWorktreeRuleOutcome(
                completion_event={"action": "skip"}, terminal=False
            )

        def _rule_b(ctx, key, active, active_task):
            calls.append("b")
            return ActiveWorktreeRuleOutcome(terminal=True)

        aggregates = _ActiveWorktreeAggregates()
        handled = _run_active_worktree_rules(
            [_rule_a, _rule_b], _ctx(), "1", _active(), None, aggregates
        )
        assert handled is True
        assert calls == ["a", "b"]
        # non-terminalなruleが記録したイベントもaggregatesへmergeされていること
        assert aggregates.completion_events == [{"action": "skip"}]

    def test_no_rule_matches_returns_false(self):
        def _rule_a(ctx, key, active, active_task):
            return None

        aggregates = _ActiveWorktreeAggregates()
        handled = _run_active_worktree_rules(
            [_rule_a], _ctx(), "1", _active(), None, aggregates
        )
        assert handled is False
        assert aggregates.completion_events == []

    def test_merges_all_outcome_fields(self):
        def _rule(ctx, key, active, active_task):
            return ActiveWorktreeRuleOutcome(
                completion_event={"action": "done"},
                deviation_event={"action": "recomputed"},
                completed_subtask_id="task-a",
                forced_serial=True,
                terminal=True,
            )

        aggregates = _ActiveWorktreeAggregates()
        _run_active_worktree_rules([_rule], _ctx(), "1", _active(), None, aggregates)
        assert aggregates.completion_events == [{"action": "done"}]
        assert aggregates.deviation_events == [{"action": "recomputed"}]
        assert aggregates.completed_subtask_ids == {"task-a"}
        assert aggregates.any_forced_serial is True


class TestRuleChangesRequested:
    def test_none_when_no_dependency_changes_requested(self):
        task = _task(depends_on=("task-x",))
        outcome = _rule_changes_requested(_ctx(), "1", _active(), task)
        assert outcome is None

    def test_terminal_event_when_dependency_changes_requested(self):
        task = _task(depends_on=("task-x",))
        ctx = _ctx(changes_requested_subtask_ids={"task-x"})
        outcome = _rule_changes_requested(ctx, "1", _active(), task)
        assert outcome is not None
        assert outcome.terminal is True
        assert (
            outcome.completion_event["action"] == "escalated_due_to_changes_requested"
        )


class TestRuleCompleted:
    def test_dirty_worktree_is_terminal(self):
        active = _active()
        task = _task()
        ctx = _ctx()
        with (
            patch(
                "orchestune.dispatch_cycle._is_worktree_complete",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_cycle._finalize_completed_worktree",
                return_value={"action": "completion_skipped_dirty_worktree"},
            ),
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is not None
        assert outcome.terminal is True
        assert outcome.completion_event["action"] == "completion_skipped_dirty_worktree"


class TestProcessActiveWorktrees:
    """_process_active_worktreesの結合テストケース。"""

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
                "orchestune.dispatch_cycle._is_worktree_complete",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_cycle.is_process_alive",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_rebase._decide_rebase_needed",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_cycle.check_footprint_deviation",
                return_value=["b.py"],
            ),
            patch(
                "orchestune.dispatch_cycle._handle_footprint_deviation",
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
                "orchestune.dispatch_cycle._is_worktree_complete",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_cycle._finalize_completed_worktree",
                return_value={"action": "completion_skipped_dirty_worktree"},
            ),
            patch(
                "orchestune.dispatch_cycle._try_auto_rebase",
                side_effect=AssertionError("Should not call auto rebase"),
            ),
            patch(
                "orchestune.dispatch_cycle.check_footprint_deviation",
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
                "orchestune.dispatch_cycle._finalize_not_needed_worktree",
                return_value={"action": "not_needed"},
            ),
            patch(
                "orchestune.dispatch_cycle._decide_stale_active_entry",
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
                "orchestune.dispatch_cycle._is_worktree_complete",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_cycle.is_process_alive",
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
                "orchestune.dispatch_cycle._finalize_not_needed_worktree",
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
                "orchestune.dispatch_cycle._finalize_not_needed_worktree",
                return_value={"action": "not_needed"},
            ),
            patch(
                "orchestune.dispatch_cycle._is_worktree_complete",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_cycle.is_process_alive",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_rebase._decide_rebase_needed",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_cycle.check_footprint_deviation",
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
        assert "2" in ctx_two.run_state.active_worktrees
