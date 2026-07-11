from unittest.mock import patch

from orchestune.dispatch_cycle import (
    _decide_blocked_promotions,
    _decide_changes_requested_escalation,
    _decide_external_lock_sync,
    _decide_stale_active_entry,
)
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree, RunState
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
        assert _decide_changes_requested_escalation(_task(depends_on=()), set()) is False

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
