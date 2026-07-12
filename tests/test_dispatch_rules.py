from orchestune.dispatch_rules import (
    ActiveWorktreeRuleOutcome,
    CycleContext,
    RuleChain,
    _ActiveWorktreeAggregates,
)
from orchestune.dispatch_state import ActiveWorktree, RunState
from orchestune.dispatcher import DispatcherConfig


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


class TestRuleChainRun:
    """RuleChain(Composite)の実行エンジン自体の振る舞い（#86）。

    新しい判断パターンは`_rule_*`関数を1つ書いて`RuleChain.rules`に追加する
    だけで済むことを裏付けるため、ここではルールの中身に依存しない汎用的な
    挙動（None/terminal/non-terminalの扱い）のみを検証する。
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
        handled = RuleChain(rules=[_rule_a, _rule_b]).run(
            _ctx(), "1", _active(), None, aggregates
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
        handled = RuleChain(rules=[_rule_a, _rule_b]).run(
            _ctx(), "1", _active(), None, aggregates
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
        handled = RuleChain(rules=[_rule_a, _rule_b]).run(
            _ctx(), "1", _active(), None, aggregates
        )
        assert handled is True
        assert calls == ["a", "b"]
        # non-terminalなruleが記録したイベントもaggregatesへmergeされていること
        assert aggregates.completion_events == [{"action": "skip"}]

    def test_no_rule_matches_returns_false(self):
        def _rule_a(ctx, key, active, active_task):
            return None

        aggregates = _ActiveWorktreeAggregates()
        handled = RuleChain(rules=[_rule_a]).run(
            _ctx(), "1", _active(), None, aggregates
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
        RuleChain(rules=[_rule]).run(_ctx(), "1", _active(), None, aggregates)
        assert aggregates.completion_events == [{"action": "done"}]
        assert aggregates.deviation_events == [{"action": "recomputed"}]
        assert aggregates.completed_subtask_ids == {"task-a"}
        assert aggregates.any_forced_serial is True
