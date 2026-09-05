import tempfile
from pathlib import Path

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.dependency_resolution import (
    TaskDependencies,
    resolve_all_dependencies,
)
from orchestune.dispatch.escalation import (
    _decide_changes_requested_escalation,
    _rule_changes_requested,
    apply_human_review_escalation,
)
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import ActiveWorktree, RunState

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-state-"))


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
        dependency_resolution={},
        done_issue_numbers=set(),
        ci_passed_pr_issue_numbers=set(),
        changes_requested_issue_numbers=set(),
        branch_by_issue_number={},
        prs=[],
        pr_by_branch={},
        config=DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        ),
    )
    defaults.update(overrides)
    if "dependency_resolution" not in overrides and "tasks_by_issue" in overrides:
        defaults["dependency_resolution"] = resolve_all_dependencies(
            overrides["tasks_by_issue"]
        )
    return CycleContext(**defaults)


class TestApplyHumanReviewEscalation:
    """空コミット完了・重複起動検知・CHANGES_REQUESTEDエスカレーションの
    3箇所で共有される、status:blocked-human-reviewへの遷移処理。"""

    def test_removes_in_progress_and_adds_human_review_label(self, fake_forge):
        apply_human_review_escalation(
            1, ("status:in-progress",), "理由", forge=fake_forge
        )

        fake_forge.remove_label.assert_called_once_with(1, "status:in-progress")
        fake_forge.add_label.assert_called_once_with(1, "status:blocked-human-review")
        fake_forge.add_comment.assert_called_once_with(1, "理由")

    def test_adds_human_review_label_before_removing_old_labels(self, fake_forge):
        # #381: 途中でクラッシュしてもIssueが必ずいずれかのstatus:*ラベルを
        # 持ち続けるよう、addがremoveより先に呼ばれなければならない。
        call_order: list[tuple[str, str]] = []
        fake_forge.remove_label.side_effect = lambda issue, label: call_order.append(
            ("remove", label)
        )
        fake_forge.add_label.side_effect = lambda issue, label: call_order.append(
            ("add", label)
        )
        apply_human_review_escalation(
            1, ("status:in-progress",), "理由", forge=fake_forge
        )

        assert call_order == [
            ("add", "status:blocked-human-review"),
            ("remove", "status:in-progress"),
        ]

    def test_removes_both_queued_and_blocked_when_both_present(self, fake_forge):
        apply_human_review_escalation(
            2, ("status:queued", "status:blocked"), "理由", forge=fake_forge
        )

        fake_forge.remove_label.assert_any_call(2, "status:queued")
        fake_forge.remove_label.assert_any_call(2, "status:blocked")
        assert fake_forge.remove_label.call_count == 2

    def test_ignores_unrelated_labels(self, fake_forge):
        apply_human_review_escalation(
            3,
            ("status:in-progress", "priority:high"),
            "理由",
            forge=fake_forge,
        )

        fake_forge.remove_label.assert_called_once_with(3, "status:in-progress")

    def test_no_removable_labels_still_adds_human_review_and_comment(self, fake_forge):
        apply_human_review_escalation(4, (), "理由", forge=fake_forge)

        fake_forge.remove_label.assert_not_called()
        fake_forge.add_label.assert_called_once_with(4, "status:blocked-human-review")
        fake_forge.add_comment.assert_called_once_with(4, "理由")


class TestDecideChangesRequestedEscalation:
    def test_false_when_no_depends_on(self):
        assert (
            _decide_changes_requested_escalation(_task(depends_on=()), set(), {})
            is False
        )

    def test_false_when_dependency_not_changes_requested(self):
        task = _task(depends_on=("task-x",))
        deps = {1: TaskDependencies(resolved=(2,))}
        assert _decide_changes_requested_escalation(task, set(), deps) is False

    def test_true_when_dependency_changes_requested(self):
        task = _task(depends_on=("task-x",))
        deps = {1: TaskDependencies(resolved=(2,))}
        assert _decide_changes_requested_escalation(task, {2}, deps) is True


class TestRuleChangesRequested:
    def test_none_when_no_dependency_changes_requested(self):
        task = _task(depends_on=("task-x",), parent_number=100)
        dep = _task(issue_number=2, subtask_id="task-x", parent_number=100)
        ctx = _ctx(tasks_by_issue={1: task, 2: dep})
        outcome = _rule_changes_requested(ctx, "1", _active(), task)
        assert outcome is None

    def test_terminal_event_when_dependency_changes_requested(self):
        task = _task(depends_on=("task-x",), parent_number=100)
        dep = _task(issue_number=2, subtask_id="task-x", parent_number=100)
        ctx = _ctx(
            tasks_by_issue={1: task, 2: dep},
            changes_requested_issue_numbers={2},
        )
        outcome = _rule_changes_requested(ctx, "1", _active(), task)
        assert outcome is not None
        assert outcome.terminal is True
        assert (
            outcome.completion_event["action"] == "escalated_due_to_changes_requested"
        )
