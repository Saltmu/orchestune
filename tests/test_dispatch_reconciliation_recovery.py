"""dispatch_reconciliation.py の整合性修復に関する境界値テスト (#337)。

base branch redの復元境界は既存の
`tests/test_dispatch_cycle.py`では実質未検証だったため、本ファイルで
単体テストとして完結させる。
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.reconciliation import (
    BaseBranchRedRecoveryDecision,
    _apply_base_branch_red_recovery,
    _decide_base_branch_red_recovery,
    _handle_base_branch_red_recovery,
    _resolve_base_branch_for_task,
)
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.models import IssueRecord
from orchestune.outcome_record import OutcomeRecord

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
            events_log_path=tmp_path / "events.jsonl",
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


class TestBaseBranchRedRecovery:
    def test_decide_requeue_when_base_sha_advances_and_no_pending_deps(self):
        issue = _issue(1, labels=("status:blocked", "ci:base-branch-red"))
        task = _task(issue_number=1, subtask_id="task-a", depends_on=())
        outcome = OutcomeRecord(
            result="blocked",
            issue=1,
            reason="base-branch-red",
            base_sha="1111111111111111111111111111111111111111",
            attempt=1,
        )
        decisions = _decide_base_branch_red_recovery(
            base_branch_red_issues=[issue],
            tasks_by_issue={1: task},
            done_subtask_ids=set(),
            current_base_shas={1: "2222222222222222222222222222222222222222"},
            outcomes_by_issue={1: outcome},
        )
        assert len(decisions) == 1
        assert decisions[0].action == "requeue"
        assert decisions[0].issue_number == 1
        assert decisions[0].subtask_id == "task-a"

    def test_decide_unmark_only_when_base_sha_advances_but_has_pending_deps(self):
        issue = _issue(1, labels=("status:blocked", "ci:base-branch-red"))
        task = _task(issue_number=1, subtask_id="task-a", depends_on=("task-dep",))
        outcome = OutcomeRecord(
            result="blocked",
            issue=1,
            reason="base-branch-red",
            base_sha="1111111111111111111111111111111111111111",
            attempt=1,
        )
        decisions = _decide_base_branch_red_recovery(
            base_branch_red_issues=[issue],
            tasks_by_issue={1: task},
            done_subtask_ids=set(),  # task-dep is not done
            current_base_shas={1: "2222222222222222222222222222222222222222"},
            outcomes_by_issue={1: outcome},
        )
        assert len(decisions) == 1
        assert decisions[0].action == "unmark_only"

    def test_decide_no_recovery_when_base_sha_has_not_advanced(self):
        issue = _issue(1, labels=("status:blocked", "ci:base-branch-red"))
        task = _task(issue_number=1, subtask_id="task-a", depends_on=())
        outcome = OutcomeRecord(
            result="blocked",
            issue=1,
            reason="base-branch-red",
            base_sha="1111111111111111111111111111111111111111",
            attempt=1,
        )
        decisions = _decide_base_branch_red_recovery(
            base_branch_red_issues=[issue],
            tasks_by_issue={1: task},
            done_subtask_ids=set(),
            current_base_shas={1: "1111111111111111111111111111111111111111"},
            outcomes_by_issue={1: outcome},
        )
        assert decisions == []

    def test_decide_escalate_when_attempt_3(self):
        issue = _issue(1, labels=("status:blocked", "ci:base-branch-red"))
        task = _task(issue_number=1, subtask_id="task-a", depends_on=())
        outcome = OutcomeRecord(
            result="blocked",
            issue=1,
            reason="base-branch-red",
            base_sha="1111111111111111111111111111111111111111",
            attempt=3,
        )
        decisions = _decide_base_branch_red_recovery(
            base_branch_red_issues=[issue],
            tasks_by_issue={1: task},
            done_subtask_ids=set(),
            current_base_shas={1: "2222222222222222222222222222222222222222"},
            outcomes_by_issue={1: outcome},
        )
        assert len(decisions) == 1
        assert decisions[0].action == "escalate"
        assert decisions[0].attempt == 3

    def test_apply_requeue_removes_marker_and_transitions_to_queued(self, tmp_path):
        decision = BaseBranchRedRecoveryDecision(
            issue_number=1,
            subtask_id="task-a",
            action="requeue",
            recorded_base_sha="1111111",
            current_base_sha="2222222",
            attempt=1,
        )
        fake_forge = MagicMock()
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            forge=fake_forge,
        )
        events = _apply_base_branch_red_recovery([decision], config)
        assert events == [{"issue_number": 1, "subtask_id": "task-a"}]
        fake_forge.remove_label.assert_any_call(1, "ci:base-branch-red")
        fake_forge.add_label.assert_called_once_with(1, "status:queued")
        fake_forge.add_comment.assert_called_once()

    def test_apply_escalate_transitions_to_blocked_human_review(self, tmp_path):
        decision = BaseBranchRedRecoveryDecision(
            issue_number=1,
            subtask_id="task-a",
            action="escalate",
            attempt=3,
        )
        fake_forge = MagicMock()
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            forge=fake_forge,
        )
        events = _apply_base_branch_red_recovery([decision], config)
        assert events == []
        fake_forge.add_label.assert_called_once_with(1, "status:blocked-human-review")
        fake_forge.remove_label.assert_any_call(1, "ci:base-branch-red")

    def test_handle_base_branch_red_recovery_empty_when_no_matching_issues(
        self, tmp_path
    ):
        issues_mock = MagicMock()
        issues_mock.all.return_value = [_issue(1, labels=("status:blocked",))]
        ctx = MagicMock()
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
        )
        events = _handle_base_branch_red_recovery(issues_mock, ctx, set(), config)
        assert events == []

    def test_handle_base_branch_red_recovery_success(self, tmp_path):
        issue = _issue(1, labels=("status:blocked", "ci:base-branch-red"))
        issues_mock = MagicMock()
        issues_mock.all.return_value = [issue]
        task = _task(issue_number=1, subtask_id="task-a", depends_on=())
        outcome = OutcomeRecord(
            result="blocked",
            issue=1,
            reason="base-branch-red",
            base_sha="1111111111111111111111111111111111111111",
            attempt=1,
        )
        fake_forge = MagicMock()
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:10Z"}
        ]
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            forge=fake_forge,
        )
        ctx = MagicMock()
        ctx.tasks_by_issue = {1: task}
        ctx.done_subtask_ids = set()
        ctx.subtask_branch_map = {}

        with patch(
            "orchestune.dispatch.reconciliation._get_branch_commit_sha",
            return_value="2222222222222222222222222222222222222222",
        ):
            events = _handle_base_branch_red_recovery(issues_mock, ctx, set(), config)

        assert events == [{"issue_number": 1, "subtask_id": "task-a"}]
        fake_forge.remove_label.assert_any_call(1, "ci:base-branch-red")
        fake_forge.add_label.assert_called_once_with(1, "status:queued")


class TestResolveBaseBranchForTask:
    def test_when_sole_dependency_is_done_returns_origin_main(self, tmp_path):
        task = _task(issue_number=2, subtask_id="task-b", depends_on=("task-a",))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            parent_issue_number=None,
        )
        subtask_branch_map = {"task-a": "claude/issue-1-task-a"}
        done_subtask_ids = {"task-a"}

        base_branch = _resolve_base_branch_for_task(
            task, config, subtask_branch_map, done_subtask_ids
        )
        assert base_branch == "origin/main"

    def test_when_sole_dependency_is_done_with_parent_returns_parent_branch(
        self, tmp_path
    ):
        task = _task(issue_number=2, subtask_id="task-b", depends_on=("task-a",))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            parent_issue_number=100,
        )
        subtask_branch_map = {"task-a": "claude/issue-1-task-a"}
        done_subtask_ids = {"task-a"}

        base_branch = _resolve_base_branch_for_task(
            task, config, subtask_branch_map, done_subtask_ids
        )
        assert base_branch == "parent/issue-100"

    def test_when_single_dependency_unresolved_returns_dep_branch(self, tmp_path):
        task = _task(issue_number=2, subtask_id="task-b", depends_on=("task-a",))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            parent_issue_number=100,
        )
        subtask_branch_map = {"task-a": "claude/issue-1-task-a"}
        done_subtask_ids = set()

        base_branch = _resolve_base_branch_for_task(
            task, config, subtask_branch_map, done_subtask_ids
        )
        assert base_branch == "claude/issue-1-task-a"

    def test_when_multiple_dependencies_unresolved_returns_parent_or_main(
        self, tmp_path
    ):
        task = _task(
            issue_number=3,
            subtask_id="task-c",
            depends_on=("task-a", "task-b"),
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            parent_issue_number=100,
        )
        subtask_branch_map = {
            "task-a": "claude/issue-1-task-a",
            "task-b": "claude/issue-2-task-b",
        }
        done_subtask_ids = set()

        base_branch = _resolve_base_branch_for_task(
            task, config, subtask_branch_map, done_subtask_ids
        )
        assert base_branch == "parent/issue-100"
