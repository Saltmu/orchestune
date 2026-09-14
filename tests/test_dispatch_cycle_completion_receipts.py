"""#882: CompletionReceiptとGC完了の保存後Context反映。

`_record_completed_worktree`は、Forge成功後の履歴追加・active削除という
既存の帳簿操作を行った"後"に、既存`save_run_state`を成功境界として置き、
保存が実際に成功した場合にのみ`CompletionReceipt`を消費して
`ctx.record_completion`を呼ぶ。保存前・保存例外時にはContextへ一切反映せず、
`"completed"`/検証済み`"already_merged"`以外のaction（token-limit escalation
を含む）はreceiptを生成しない——Action文字列の`"completed"`接頭辞や履歴差分
から推測しない。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestune.dispatch.cycle_context_state import RecordStatus
from orchestune.dispatch.cycle_records import CompletionReceipt
from orchestune.dispatch.dependency_assessment import DependencyState
from orchestune.dispatch.dependency_resolution import TaskDependencies
from orchestune.dispatch.gc import _record_completed_worktree
from tests.dispatch_gc_test_support import _active, _task
from tests.dispatch_gc_test_support import _rule_ctx as _ctx


class TestCompletionReceipt:
    def test_is_frozen_and_holds_the_issue_number(self):
        receipt = CompletionReceipt(issue_number=280)

        assert receipt.issue_number == 280
        with pytest.raises(AttributeError):
            receipt.issue_number = 281  # type: ignore[misc]


class TestRecordCompletedWorktreeSuccessBoundary:
    def _ctx_with_task(self, **task_overrides):
        task_overrides.setdefault("status_labels", ("status:in-progress",))
        task = _task(**task_overrides)
        ctx = _ctx(tasks_by_issue={task.issue_number: task})
        ctx.config.apply = True
        return ctx, task

    def test_completed_action_saves_before_recording_completion(self):
        ctx, task = self._ctx_with_task()
        active = _active()
        ctx.run_state.active_worktrees["1"] = active
        calls: list[str] = []

        def _fake_save(*_args, **_kwargs):
            calls.append("save")
            assert not ctx.is_completion_confirmed(
                active.issue_number
            ), "record_completion must not fire before save_run_state succeeds"

        with patch(
            "orchestune.dispatch.gc.save_run_state", side_effect=_fake_save
        ) as mock_save:
            _record_completed_worktree(ctx, "1", active, task, {"action": "completed"})

        mock_save.assert_called_once_with(
            ctx.run_state,
            ctx.config.run_state_path,
            launch_window_seconds=ctx.config.window_seconds,
            open_prs=ctx.prs,
        )
        assert calls == ["save"]
        assert ctx.is_completion_confirmed(active.issue_number) is True
        assert "1" not in ctx.run_state.active_worktrees
        assert len(ctx.run_state.completed_worktrees) == 1

    def test_verified_already_merged_records_completion_even_without_subtask_id(self):
        ctx, task = self._ctx_with_task(subtask_id="")
        active = _active()
        ctx.run_state.active_worktrees["1"] = active

        with patch("orchestune.dispatch.gc.save_run_state", autospec=True):
            _record_completed_worktree(
                ctx, "1", active, task, {"action": "already_merged"}
            )

        assert ctx.is_completion_confirmed(active.issue_number) is True

    def test_escalated_token_limit_exceeded_does_not_record_completion(self):
        ctx, task = self._ctx_with_task()
        active = _active()
        ctx.run_state.active_worktrees["1"] = active

        with patch("orchestune.dispatch.gc.save_run_state", autospec=True):
            _record_completed_worktree(
                ctx,
                "1",
                active,
                task,
                {"action": "escalated_token_limit_exceeded"},
            )

        assert ctx.is_completion_confirmed(active.issue_number) is False
        # History bookkeeping is unaffected by the receipt decision.
        assert len(ctx.run_state.completed_worktrees) == 1

    def test_save_exception_suppresses_the_completion_record(self):
        ctx, task = self._ctx_with_task()
        active = _active()
        ctx.run_state.active_worktrees["1"] = active

        with patch(
            "orchestune.dispatch.gc.save_run_state",
            side_effect=RuntimeError("disk full"),
        ):
            _record_completed_worktree(ctx, "1", active, task, {"action": "completed"})

        assert ctx.is_completion_confirmed(active.issue_number) is False
        # The in-memory ledger mutation (Forge success already confirmed it)
        # is not rolled back by a persistence failure.
        assert len(ctx.run_state.completed_worktrees) == 1
        assert "1" not in ctx.run_state.active_worktrees

    def test_dry_run_never_saves_or_records_completion(self):
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx(tasks_by_issue={task.issue_number: task})
        ctx.config.apply = False
        active = _active()

        with patch("orchestune.dispatch.gc.save_run_state", autospec=True) as mock_save:
            outcome = _record_completed_worktree(
                ctx, "1", active, task, {"action": "completed"}
            )

        mock_save.assert_not_called()
        assert ctx.is_completion_confirmed(active.issue_number) is False
        assert outcome.completion_event["action"] == "completed"

    def test_confirmed_completion_reflects_immediately_in_dependent_assessment(self):
        upstream = _task(
            issue_number=280,
            subtask_id="upstream",
            status_labels=("status:in-progress",),
        )
        downstream = _task(
            issue_number=281,
            subtask_id="downstream",
            status_labels=("status:queued",),
        )
        ctx = _ctx(
            tasks_by_issue={280: upstream, 281: downstream},
            dependency_resolution={281: TaskDependencies(resolved=(280,))},
        )
        ctx.config.apply = True
        active = _active(issue_number=280)
        ctx.run_state.active_worktrees["1"] = active

        before = ctx.assess_dependencies(281)
        assert before is not None
        assert before.resolved[0].state is DependencyState.WAITING

        with patch("orchestune.dispatch.gc.save_run_state", autospec=True):
            _record_completed_worktree(
                ctx, "1", active, upstream, {"action": "completed"}
            )

        after = ctx.assess_dependencies(281)
        assert after is not None
        assert after.resolved[0].state is DependencyState.COMPLETED


class TestRecordCompletionNoopDoesNotGrowHistory:
    def test_second_record_completion_call_is_a_noop_and_leaves_history_untouched(self):
        task = _task(issue_number=280, status_labels=("status:in-progress",))
        ctx = _ctx(tasks_by_issue={280: task})

        first = ctx.record_completion(280)
        second = ctx.record_completion(280)

        assert first.status is RecordStatus.APPLIED
        assert second.status is RecordStatus.NOOP
        assert ctx.run_state.completed_worktrees == []
