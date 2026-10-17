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

from typing import Any, cast
from unittest.mock import patch

import pytest

from orchestune.dependencies.assessment import DependencyState
from orchestune.dependencies.resolution import TaskDependencies
from orchestune.dispatch.cycle_context_state import RecordStatus
from orchestune.dispatch.cycle_events import (
    DirtyWorktreeEscalatedCompletion,
    EarlyDeathRequeuedCompletion,
    ForgeFailureCompletion,
    PriorMergeEvidenceCompletion,
    TaskWorktreeCompletion,
    WorktreeCompletion,
    WorktreeCompletionHold,
)
from orchestune.dispatch.cycle_records import CompletionReceipt
from orchestune.dispatch.gc import (
    _handle_completed_event_outcome,
    _record_completed_worktree,
)
from orchestune.dispatch.gc.completion_disposition import UnclassifiedCompletionError
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
                active.core.issue_number
            ), "record_completion must not fire before save_run_state succeeds"

        with patch(
            "orchestune.dispatch.gc.save_run_state", side_effect=_fake_save
        ) as mock_save:
            _record_completed_worktree(
                ctx,
                "1",
                active,
                task,
                WorktreeCompletion(
                    issue_number=280,
                    worktree_path=active.core.worktree_path,
                    action="completed",
                ),
                may_issue_receipt=True,
            )

        mock_save.assert_called_once_with(
            ctx.run_state,
            ctx.config.run_state_path,
            launch_window_seconds=ctx.config.window_seconds,
            open_prs=ctx.prs,
        )
        assert calls == ["save"]
        assert ctx.is_completion_confirmed(active.core.issue_number) is True
        assert "1" not in ctx.run_state.active_worktrees
        assert len(ctx.run_state.completed_worktrees) == 1

    def test_verified_already_merged_records_completion_even_without_subtask_id(self):
        ctx, task = self._ctx_with_task(subtask_id="")
        active = _active()
        ctx.run_state.active_worktrees["1"] = active

        with patch("orchestune.dispatch.gc.save_run_state", autospec=True):
            _record_completed_worktree(
                ctx,
                "1",
                active,
                task,
                WorktreeCompletion(
                    issue_number=280,
                    worktree_path=active.core.worktree_path,
                    action="already_merged",
                ),
                may_issue_receipt=True,
            )

        assert ctx.is_completion_confirmed(active.core.issue_number) is True

    def test_local_not_needed_action_records_only_after_successful_save(self):
        ctx, task = self._ctx_with_task()
        active = _active()
        ctx.run_state.active_worktrees["1"] = active
        calls: list[str] = []

        def save(*_args, **_kwargs):
            calls.append("save")
            assert ctx.is_completion_confirmed(active.core.issue_number) is False

        with patch(
            "orchestune.dispatch.gc.save_run_state", side_effect=save
        ) as mock_save:
            _record_completed_worktree(
                ctx,
                "1",
                active,
                task,
                TaskWorktreeCompletion(
                    issue_number=280,
                    subtask_id=task.subtask_id,
                    worktree_path=active.core.worktree_path,
                    action="not_needed",
                ),
                may_issue_receipt=True,
            )

        mock_save.assert_called_once()
        assert calls == ["save"]
        assert ctx.is_completion_confirmed(280) is True
        assert "1" not in ctx.run_state.active_worktrees
        assert len(ctx.run_state.completed_worktrees) == 1

    def test_special_completed_action_uses_the_same_not_needed_receipt_path(self):
        ctx, task = self._ctx_with_task()
        active = _active()
        ctx.run_state.active_worktrees["1"] = active
        event = TaskWorktreeCompletion(
            issue_number=280,
            subtask_id=task.subtask_id,
            worktree_path=active.core.worktree_path,
            action="not_needed",
        )

        with patch("orchestune.dispatch.gc.save_run_state", autospec=True) as save:
            outcome = _handle_completed_event_outcome(ctx, "1", active, task, event)

        save.assert_called_once()
        assert outcome is not None
        assert outcome.completion_event is event
        assert ctx.is_completion_confirmed(280) is True
        assert "1" not in ctx.run_state.active_worktrees

    def test_not_needed_save_failure_restores_active_entry_and_aborts(self):
        ctx, task = self._ctx_with_task()
        active = _active()
        ctx.run_state.active_worktrees["1"] = active

        with (
            patch(
                "orchestune.dispatch.gc.save_run_state",
                side_effect=OSError("disk full"),
            ),
            pytest.raises(OSError, match="persist not-needed"),
        ):
            _record_completed_worktree(
                ctx,
                "1",
                active,
                task,
                TaskWorktreeCompletion(
                    issue_number=280,
                    subtask_id=task.subtask_id,
                    worktree_path=active.core.worktree_path,
                    action="not_needed",
                ),
                may_issue_receipt=True,
            )

        assert ctx.run_state.active_worktrees["1"] is active
        assert ctx.run_state.completed_worktrees == []
        assert ctx.is_completion_confirmed(280) is False

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
                WorktreeCompletion(
                    issue_number=280,
                    worktree_path=active.core.worktree_path,
                    action="escalated_token_limit_exceeded",
                ),
                may_issue_receipt=False,
            )

        assert ctx.is_completion_confirmed(active.core.issue_number) is False
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
            _record_completed_worktree(
                ctx,
                "1",
                active,
                task,
                WorktreeCompletion(
                    issue_number=280,
                    worktree_path=active.core.worktree_path,
                    action="completed",
                ),
                may_issue_receipt=True,
            )

        assert ctx.is_completion_confirmed(active.core.issue_number) is False
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
                ctx,
                "1",
                active,
                task,
                WorktreeCompletion(
                    issue_number=280,
                    worktree_path=active.core.worktree_path,
                    action="completed",
                ),
                may_issue_receipt=True,
            )

        mock_save.assert_not_called()
        assert ctx.is_completion_confirmed(active.core.issue_number) is False
        assert outcome.completion_event.to_dict()["action"] == "completed"

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
                ctx,
                "1",
                active,
                upstream,
                WorktreeCompletion(
                    issue_number=280,
                    worktree_path=active.core.worktree_path,
                    action="completed",
                ),
                may_issue_receipt=True,
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


class TestCompletedEventRoutes:
    """#1349: 後処理の経路は`completion_handling`の分類だけで決まる。"""

    def _setup(self):
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx(tasks_by_issue={task.issue_number: task})
        ctx.config.apply = True
        active = _active()
        ctx.run_state.active_worktrees["1"] = active
        return ctx, task, active

    @pytest.mark.parametrize(
        "make_event",
        [
            lambda a: ForgeFailureCompletion(
                issue_number=280, worktree_path=a.core.worktree_path
            ),
            lambda a: WorktreeCompletionHold(
                issue_number=280,
                worktree_path=a.core.worktree_path,
                action="completion_skipped_forge_error",
            ),
            lambda a: WorktreeCompletionHold(
                issue_number=280,
                worktree_path=a.core.worktree_path,
                action="completion_skipped_prior_merge_indeterminate",
            ),
            lambda a: TaskWorktreeCompletion(
                issue_number=280,
                subtask_id="s",
                worktree_path=a.core.worktree_path,
                action="completion_skipped_dirty_worktree",
            ),
            lambda a: DirtyWorktreeEscalatedCompletion(
                issue_number=280, worktree_path=a.core.worktree_path
            ),
            lambda a: PriorMergeEvidenceCompletion(
                issue_number=280,
                action="already_merged",
                pr_number=1,
                base_ref="main",
                merged_at="2026-01-01T00:00:00Z",
                reason="r",
            ),
        ],
    )
    def test_keep_events_change_no_ledger_state(self, make_event):
        ctx, task, active = self._setup()
        event = make_event(active)

        with patch("orchestune.dispatch.gc.save_run_state", autospec=True) as save:
            outcome = _handle_completed_event_outcome(ctx, "1", active, task, event)

        assert outcome is not None
        assert outcome.completion_event is event
        assert outcome.terminal is True
        save.assert_not_called()
        assert ctx.run_state.active_worktrees == {"1": active}
        assert ctx.run_state.completed_worktrees == []
        assert ctx.is_completion_confirmed(280) is False

    def test_token_escalation_is_history_only_and_confirms_nothing(self):
        ctx, task, active = self._setup()
        event = WorktreeCompletion(
            issue_number=280,
            worktree_path=active.core.worktree_path,
            action="escalated_token_limit_exceeded",
        )

        with patch("orchestune.dispatch.gc.save_run_state", autospec=True) as save:
            outcome = _handle_completed_event_outcome(ctx, "1", active, task, event)

        assert outcome is not None and outcome.completion_event is event
        save.assert_called_once()
        assert len(ctx.run_state.completed_worktrees) == 1
        assert "1" not in ctx.run_state.active_worktrees
        assert ctx.is_completion_confirmed(280) is False

    @pytest.mark.parametrize(
        "make_event",
        [
            lambda a: EarlyDeathRequeuedCompletion(
                issue_number=280,
                worktree_path=a.core.worktree_path,
                early_death_retry_at=1.0,
            ),
            lambda a: WorktreeCompletion(
                issue_number=280,
                worktree_path=a.core.worktree_path,
                action="blocked_unknown_reason",
            ),
            lambda a: TaskWorktreeCompletion(
                issue_number=280,
                subtask_id="s",
                worktree_path=a.core.worktree_path,
                action="not_needed_review_dispatched",
            ),
        ],
    )
    def test_retire_events_drop_the_active_entry_without_history_or_receipt(
        self, make_event
    ):
        ctx, task, active = self._setup()
        event = make_event(active)

        with patch("orchestune.dispatch.gc.save_run_state", autospec=True) as save:
            outcome = _handle_completed_event_outcome(ctx, "1", active, task, event)

        assert outcome is not None and outcome.completion_event is event
        save.assert_not_called()
        assert ctx.run_state.active_worktrees == {}
        assert ctx.run_state.completed_worktrees == []
        assert ctx.is_completion_confirmed(280) is False

    def test_unclassified_action_fails_before_any_side_effect(self):
        ctx, task, active = self._setup()
        event = WorktreeCompletion(
            issue_number=280,
            worktree_path=active.core.worktree_path,
            action=cast(Any, "brand_new_action"),
        )

        with patch("orchestune.dispatch.gc.save_run_state", autospec=True) as save:
            with pytest.raises(UnclassifiedCompletionError):
                _handle_completed_event_outcome(ctx, "1", active, task, event)

        save.assert_not_called()
        assert ctx.run_state.active_worktrees == {"1": active}
        assert ctx.run_state.completed_worktrees == []
