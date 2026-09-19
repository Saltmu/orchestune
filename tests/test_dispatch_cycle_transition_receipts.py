"""#883: `apply_verified_transition` と `_authoritative_execution_active` の単体契約。

#922: status executor と blocked-recompute recovery の「接続確認」はここから外した。
前者は port 境界（`test_dispatch_cycle_consistency_port.py::TestExecuteRepair`）が、
後者は `_handle_blocked_recompute_recovery` の責務を所有する
`test_dispatch_reconciliation_promotions.py::TestHandleBlockedRecomputeRecovery` が
同じ保証を持つ。


`apply_verified_transition(ctx, receipt, *, execution_active)` is a thin
bridge over #868's already-tested `CycleContext.record_transition`:
`execution_active=None` (the caller's execution observation is unknown this
cycle) holds -- `record_transition` is never called, so an uncertain
execution claim can never retire or assert a launch fact. Every other
CONFLICT/NOOP/APPLIED outcome comes straight from `record_transition`.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import patch

from orchestune.dispatch.cycle_context_state import (
    REASON_STALE_OBSERVATION,
    REASON_TERMINAL_STATE,
    RecordStatus,
)
from orchestune.dispatch.cycle_records import (
    _authoritative_execution_active,
    apply_verified_transition,
)
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.dispatch.status_repair import (
    VerifiedStatusTransition,
    execute_status_repair_command,
)
from tests.dispatch_test_support import make_test_cycle_context, make_test_task
from tests.test_consistency_status_repair import _plan


def _ctx(tmp_path, **overrides):
    """テストごとの`tmp_path`に状態ファイルを置くCycleContext。"""
    return make_test_cycle_context(state_root=tmp_path, **overrides)


def _task(**overrides):
    """`apply_verified_transition`の入口となる`status:blocked`なTask。"""
    defaults = {"status_labels": ("status:blocked",)}
    defaults.update(overrides)
    return make_test_task(defaults.pop("issue_number", 280), **defaults)


class TestApplyVerifiedTransition:
    def test_stale_labels_conflict(self, tmp_path):
        task = _task(status_labels=("status:blocked",))
        ctx = _ctx(tmp_path, tasks_by_issue={280: task})
        receipt = VerifiedStatusTransition(
            issue_number=280,
            before_labels=("status:queued",),  # stale: ctx actually says blocked
            verified_labels=("status:queued",),
            intent_id="intent-1",
        )

        result = apply_verified_transition(ctx, receipt, execution_active=False)

        assert result is not None
        assert result.status is RecordStatus.CONFLICT
        assert result.reason == REASON_STALE_OBSERVATION
        assert ctx.task(280).status_labels == ("status:blocked",)

    def test_terminal_rollback_conflict(self, tmp_path):
        task = _task(status_labels=("status:done",))
        ctx = _ctx(tmp_path, tasks_by_issue={280: task})
        receipt = VerifiedStatusTransition(
            issue_number=280,
            before_labels=("status:done",),
            verified_labels=("status:blocked",),
            intent_id="intent-1",
        )

        result = apply_verified_transition(ctx, receipt, execution_active=False)

        assert result is not None
        assert result.status is RecordStatus.CONFLICT
        assert result.reason == REASON_TERMINAL_STATE
        assert ctx.task(280).status_labels == ("status:done",)

    def test_unknown_execution_holds_without_recording(self, tmp_path):
        task = _task(status_labels=("status:blocked",))
        ctx = _ctx(tmp_path, tasks_by_issue={280: task})
        receipt = VerifiedStatusTransition(
            issue_number=280,
            before_labels=("status:blocked",),
            verified_labels=("status:in-progress",),
            intent_id="intent-1",
        )

        with patch.object(ctx, "record_transition", wraps=ctx.record_transition) as spy:
            result = apply_verified_transition(ctx, receipt, execution_active=None)

        spy.assert_not_called()
        assert result is None
        assert ctx.task(280).status_labels == ("status:blocked",)

    def test_same_retry_is_a_noop(self, tmp_path):
        task = _task(status_labels=("status:blocked",))
        ctx = _ctx(tmp_path, tasks_by_issue={280: task})
        receipt = VerifiedStatusTransition(
            issue_number=280,
            before_labels=("status:blocked",),
            verified_labels=("status:queued",),
            intent_id="intent-1",
        )

        first = apply_verified_transition(ctx, receipt, execution_active=False)
        second = apply_verified_transition(ctx, receipt, execution_active=False)

        assert first is not None and first.status is RecordStatus.APPLIED
        assert second is not None and second.status is RecordStatus.NOOP
        assert ctx.task(280).status_labels == ("status:queued",)

    def test_auxiliary_label_removal_with_unchanged_primary_applies(self, tmp_path):
        before = ("status:blocked", "priority:high", "ci:base-branch-red")
        task = _task(status_labels=before)
        ctx = _ctx(tmp_path, tasks_by_issue={280: task})
        receipt = VerifiedStatusTransition(
            issue_number=280,
            before_labels=before,
            verified_labels=("status:blocked", "priority:high"),
            intent_id="intent-1",
        )

        result = apply_verified_transition(ctx, receipt, execution_active=False)

        assert result is not None
        assert result.status is RecordStatus.APPLIED
        # `record_transition` stores the normalized (sorted) label set.
        assert ctx.task(280).status_labels == ("priority:high", "status:blocked")


def _ctx_with_active_launch(tmp_path, **overrides):
    active = ActiveWorktree(
        issue_number=280,
        branch="claude/issue-280-task-a",
        worktree_path="worktrees/w1",
        pid=111,
        started_at=1_699_999_000.0,
        declared_footprint=(),
    )
    overrides.setdefault("run_state", RunState(active_worktrees={"1": active}))
    return _ctx(tmp_path, **overrides)


class TestAuthoritativeExecutionActive:
    """Codex #899 review: a live launch must never be silently reclaimed by
    an unrelated status repair whose verified target isn't a launch label.
    """

    def test_no_launch_and_non_execution_target_is_false(self, tmp_path):
        ctx = _ctx(tmp_path, tasks_by_issue={280: _task()})
        receipt = VerifiedStatusTransition(
            280, ("status:blocked",), ("status:queued",), "i"
        )

        assert _authoritative_execution_active(ctx, receipt) is False

    def test_indeterminate_active_entry_and_non_execution_target_holds(self, tmp_path):
        """Two ActiveWorktree entries for the same Issue make `launch_fact`
        report `None` (ambiguous), but the bookkeeping entries are still
        present -- this must hold, not fall through to `False`.
        """
        duplicate = ActiveWorktree(
            issue_number=280,
            branch="claude/issue-280-task-a",
            worktree_path="worktrees/w1",
            pid=111,
            started_at=1_699_999_000.0,
            declared_footprint=(),
        )
        ctx = _ctx(
            tmp_path,
            tasks_by_issue={280: _task()},
            run_state=RunState(
                active_worktrees={
                    "1": duplicate,
                    "2": dataclasses.replace(duplicate, worktree_path="worktrees/w2"),
                }
            ),
        )
        assert ctx.launch_fact(280) is None
        receipt = VerifiedStatusTransition(
            280, ("status:blocked",), ("status:queued",), "i"
        )

        assert (
            _authoritative_execution_active(
                ctx, receipt, has_active_entry=lambda issue_number: issue_number == 280
            )
            is None
        )

    def test_active_launch_and_non_execution_target_holds(self, tmp_path):
        ctx = _ctx_with_active_launch(tmp_path, tasks_by_issue={280: _task()})
        receipt = VerifiedStatusTransition(
            280, ("status:blocked",), ("status:queued",), "i"
        )

        assert _authoritative_execution_active(ctx, receipt) is None

    def test_active_launch_and_execution_target_is_true(self, tmp_path):
        ctx = _ctx_with_active_launch(tmp_path, tasks_by_issue={280: _task()})
        receipt = VerifiedStatusTransition(
            280, ("status:queued",), ("status:in-progress",), "i"
        )

        assert _authoritative_execution_active(ctx, receipt) is True

    def test_no_launch_and_execution_target_holds(self, tmp_path):
        ctx = _ctx(tmp_path, tasks_by_issue={280: _task()})
        receipt = VerifiedStatusTransition(
            280, ("status:queued",), ("status:in-progress",), "i"
        )

        assert _authoritative_execution_active(ctx, receipt) is None

    def test_unrelated_repair_does_not_reclaim_a_live_launch(self, tmp_path):
        """An unrelated PRIMARY_STATUS_CONFLICT-style repair down to
        `status:queued` must leave a genuinely active launch's fact intact,
        not retire it -- otherwise the Issue becomes schedulable again while
        the original run is still active.
        """
        ctx = _ctx_with_active_launch(
            tmp_path, tasks_by_issue={280: _task(status_labels=("status:queued",))}
        )
        assert ctx.launch_fact(280) is not None
        receipt = VerifiedStatusTransition(
            issue_number=280,
            before_labels=("status:queued",),
            verified_labels=("status:queued",),
            intent_id="i",
        )

        result = apply_verified_transition(
            ctx, receipt, execution_active=_authoritative_execution_active(ctx, receipt)
        )

        assert result is None
        assert ctx.launch_fact(280) is not None


def _remove_done_command(task):
    return _plan({task.issue_number: task})[1][0]


def _execute(command, tasks, config, on_verified=None):
    return execute_status_repair_command(
        command,
        tasks,
        completion_evidence=_evidence(),
        config=config,
        on_verified=on_verified,
    )


def _evidence():
    class _CompletionEvidence:
        def is_completion_confirmed(self, issue_number: int) -> bool:
            return False

    return _CompletionEvidence()
