from unittest.mock import patch

from orchestune.dispatch_escalation import (
    _decide_changes_requested_escalation,
    _rule_changes_requested,
    apply_human_review_escalation,
)
from orchestune.dispatch_rules import CycleContext
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree, RunState
from orchestune.dispatcher import DispatcherConfig


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


class TestApplyHumanReviewEscalation:
    """空コミット完了・重複起動検知・CHANGES_REQUESTEDエスカレーションの
    3箇所で共有される、status:blocked-human-reviewへの遷移処理。"""

    def test_removes_in_progress_and_adds_human_review_label(self):
        with (
            patch("orchestune.dispatch_escalation.github.remove_label") as mock_remove,
            patch("orchestune.dispatch_escalation.github.add_label") as mock_add,
            patch("orchestune.dispatch_escalation.github.add_comment") as mock_comment,
        ):
            apply_human_review_escalation(1, ("status:in-progress",), "理由")

        mock_remove.assert_called_once_with(1, "status:in-progress")
        mock_add.assert_called_once_with(1, "status:blocked-human-review")
        mock_comment.assert_called_once_with(1, "理由")

    def test_removes_both_queued_and_blocked_when_both_present(self):
        with (
            patch("orchestune.dispatch_escalation.github.remove_label") as mock_remove,
            patch("orchestune.dispatch_escalation.github.add_label"),
            patch("orchestune.dispatch_escalation.github.add_comment"),
        ):
            apply_human_review_escalation(
                2, ("status:queued", "status:blocked"), "理由"
            )

        mock_remove.assert_any_call(2, "status:queued")
        mock_remove.assert_any_call(2, "status:blocked")
        assert mock_remove.call_count == 2

    def test_ignores_unrelated_labels(self):
        with (
            patch("orchestune.dispatch_escalation.github.remove_label") as mock_remove,
            patch("orchestune.dispatch_escalation.github.add_label"),
            patch("orchestune.dispatch_escalation.github.add_comment"),
        ):
            apply_human_review_escalation(
                3, ("status:in-progress", "priority:high"), "理由"
            )

        mock_remove.assert_called_once_with(3, "status:in-progress")

    def test_no_removable_labels_still_adds_human_review_and_comment(self):
        with (
            patch("orchestune.dispatch_escalation.github.remove_label") as mock_remove,
            patch("orchestune.dispatch_escalation.github.add_label") as mock_add,
            patch("orchestune.dispatch_escalation.github.add_comment") as mock_comment,
        ):
            apply_human_review_escalation(4, (), "理由")

        mock_remove.assert_not_called()
        mock_add.assert_called_once_with(4, "status:blocked-human-review")
        mock_comment.assert_called_once_with(4, "理由")


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
