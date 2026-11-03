"""CycleActionAdapter active-worktree and GC ports."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from orchestune.dispatch.cycle.actions import CycleActionAdapter
from orchestune.dispatch.cycle.events import (
    AlreadyForcedSerialDeviation,
    ForcedSerialDeviation,
    RecomputedDeviation,
    WorktreeCompletion,
    WorktreeCompletionHold,
)
from orchestune.dispatch.gc.completion import is_completion_hold_event
from orchestune.dispatch.rebase import _rule_footprint_deviation
from orchestune.ledger.run_state import RunState
from orchestune.models import PrRecord
from orchestune.task_metadata import CycleTask
from tests.dispatch_gc_test_support import _active, _ctx, _task


class TestProcessActiveWorktrees:
    def test_completion_reaches_the_adapters_own_run_state(self, fake_forge):
        active = _active(pid=4_200_123, started_at=1_699_999_000.0)
        task = _task(status_labels=("status:in-progress",))
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(forge=fake_forge, tasks_by_issue={280: task}, run_state=run_state)
        ctx.config.apply = True
        adapter = CycleActionAdapter(run_state, ctx.config, now=1_700_000_000.0)
        adapter.bind_context(ctx)

        with (
            patch(
                "orchestune.dispatch.gc.rule_chain._is_worktree_complete",
                autospec=True,
                return_value=True,
            ),
            patch(
                "orchestune.dispatch.gc.rule_chain._finalize_completed_worktree",
                autospec=True,
                return_value=WorktreeCompletion(
                    issue_number=280,
                    worktree_path=active.core.worktree_path,
                    action="completed",
                    commit_sha="abc123d",
                ),
            ),
            patch(
                "orchestune.dispatch.gc.rule_chain.save_run_state", autospec=True
            ) as mock_save,
        ):
            result = adapter.process_active_worktrees()

        # The mutation and the save both landed on the exact object the
        # adapter was constructed with -- not a copy.
        assert mock_save.call_args.args[0] is run_state
        assert "1" not in run_state.active_worktrees
        assert len(run_state.completed_worktrees) == 1
        assert len(result.completion_events) == 1
        assert result.completion_events[0].action == "completed"

    def test_dirty_hold_event_is_still_recognized_downstream(self, fake_forge):
        active = _active(pid=4_200_123, started_at=1_699_999_000.0)
        task = _task(status_labels=("status:in-progress",))
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(forge=fake_forge, tasks_by_issue={280: task}, run_state=run_state)
        ctx.config.apply = True
        adapter = CycleActionAdapter(run_state, ctx.config, now=0.0)
        adapter.bind_context(ctx)

        with (
            patch(
                "orchestune.dispatch.gc.rule_chain._is_worktree_complete",
                autospec=True,
                return_value=True,
            ),
            patch(
                "orchestune.dispatch.gc.rule_chain._finalize_completed_worktree",
                autospec=True,
                return_value=WorktreeCompletionHold(
                    issue_number=280,
                    worktree_path=active.core.worktree_path,
                    action="completion_skipped_dirty_worktree",
                ),
            ),
        ):
            result = adapter.process_active_worktrees()

        assert len(result.completion_events) == 1
        assert is_completion_hold_event(result.completion_events[0])

        with patch(
            "orchestune.dispatch.cycle.actions.run_gc_phase", autospec=True
        ) as mock_run_gc:
            adapter.run_gc(result.completion_events)

        passed_events = mock_run_gc.call_args.args[3]
        assert passed_events == list(result.completion_events)
        assert is_completion_hold_event(passed_events[0])


class TestFootprintDeviationRule:
    """`_rule_footprint_deviation` builds both lookup maps from `ctx.queries`.

    Codex #900 review: `issue_number_by_subtask_id` is not display-only --
    footprint deviation handling (`rebase.notify_recompute`) uses it to find
    and transition the actually-blocked issue, so it must be reconstructed
    from the bound view rather than left empty.
    """

    @staticmethod
    def _rule_inputs(tasks_by_issue):
        run_state = RunState(active_worktrees={})
        ctx = _ctx(tasks_by_issue=tasks_by_issue, run_state=run_state)
        adapter = CycleActionAdapter(run_state, ctx.config, now=0.0)
        adapter.bind_context(ctx)
        queries = MagicMock(wraps=ctx)
        execution_ctx = replace(adapter._execution_context(), queries=queries)
        return ctx, queries, execution_ctx

    @pytest.mark.parametrize(
        ("event", "forced_serial"),
        [
            (
                ForcedSerialDeviation(
                    issue_number=280, deviated_files=("a.py",), recompute_count=1
                ),
                True,
            ),
            (
                AlreadyForcedSerialDeviation(
                    issue_number=280, deviated_files=("a.py",)
                ),
                True,
            ),
            (
                RecomputedDeviation(
                    issue_number=280, deviated_files=("a.py",), conflicts=()
                ),
                False,
            ),
        ],
    )
    def test_maps_are_built_from_one_queries_snapshot(self, event, forced_serial):
        # Inserted in descending order; #282 has no subtask_id; #285 repeats
        # task-a, and the highest issue number must win (query order is ascending).
        ctx, queries, execution_ctx = self._rule_inputs(
            {
                285: _task(issue_number=285, subtask_id="task-a"),
                282: _task(issue_number=282, subtask_id=""),
                281: _task(issue_number=281, subtask_id="task-b"),
                280: _task(issue_number=280, subtask_id="task-a"),
            }
        )
        active = _active()

        with (
            patch(
                "orchestune.dispatch.rebase.check_footprint_deviation",
                autospec=True,
                return_value=["a.py"],
            ),
            patch(
                "orchestune.dispatch.rebase._handle_footprint_deviation",
                autospec=True,
                return_value=event,
            ) as handler,
        ):
            outcome = _rule_footprint_deviation(execution_ctx, "1", active, None)

        queries.tasks.assert_called_once_with()
        args = handler.call_args.args
        assert args[0] is active
        assert args[1] == ["a.py"]
        assert args[2] == {task.issue_number: task for task in ctx.tasks()}
        assert sorted(args[2]) == [280, 281, 282, 285]
        assert all(isinstance(task, CycleTask) for task in args[2].values())
        assert args[3] == {"task-a": 285, "task-b": 281}
        assert args[4] is execution_ctx.config
        assert args[5] is execution_ctx.dag_inputs
        assert outcome.deviation_event is event
        assert outcome.forced_serial is forced_serial
        assert outcome.terminal is True

    @pytest.mark.parametrize("deviated", [[], None])
    def test_no_deviation_returns_before_reading_tasks(self, deviated):
        _, queries, execution_ctx = self._rule_inputs({280: _task()})

        with (
            patch(
                "orchestune.dispatch.rebase.check_footprint_deviation",
                autospec=True,
                return_value=deviated,
            ),
            patch(
                "orchestune.dispatch.rebase._handle_footprint_deviation",
                autospec=True,
            ) as handler,
        ):
            outcome = _rule_footprint_deviation(execution_ctx, "1", _active(), None)

        handler.assert_not_called()
        queries.tasks.assert_not_called()
        assert outcome.deviation_event is None
        assert outcome.forced_serial is False
        assert outcome.terminal is True


class TestRunGc:
    def test_uses_the_adapters_own_run_state_and_query_derived_args(self):
        run_state = RunState(active_worktrees={})
        task = _task(status_labels=("status:queued",))
        pr = PrRecord(
            number=1,
            head_ref="claude/issue-280-task-a",
            changed_files=(),
            closes_issue_numbers=(280,),
        )
        ctx = _ctx(tasks_by_issue={280: task}, run_state=run_state, prs=[pr])
        adapter = CycleActionAdapter(run_state, ctx.config, now=123.0)
        adapter.bind_context(ctx)
        events = (
            {"action": "completion_skipped_dirty_worktree", "worktree_path": "w1"},
        )

        with patch(
            "orchestune.dispatch.cycle.actions.run_gc_phase", autospec=True
        ) as mock_run_gc:
            result = adapter.run_gc(events)

        args, kwargs = mock_run_gc.call_args
        assert args[0] is run_state
        assert args[1] == {280: CycleTask.from_task(task)}
        assert args[2] is ctx.config
        assert args[3] == list(events)
        assert args[4] == (pr,)
        assert kwargs["now"] == 123.0
        assert result is mock_run_gc.return_value


class TestActivePhaseResult:
    def _build(self, fake_forge, *, forced_serial: bool):
        active = _active(
            pid=4_200_123, started_at=1_699_999_000.0, forced_serial=forced_serial
        )
        task = _task(status_labels=("status:in-progress",), depends_on=())
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(forge=fake_forge, tasks_by_issue={280: task}, run_state=run_state)
        ctx.config.apply = True
        return ctx, run_state

    def test_reports_completion_events_and_forced_serial(self, fake_forge):
        ctx, run_state = self._build(fake_forge, forced_serial=True)

        with patch(
            "orchestune.dispatch.gc.rule_chain.is_process_alive",
            autospec=True,
            return_value=True,
        ):
            adapter = CycleActionAdapter(run_state, ctx.config, now=0.0)
            adapter.bind_context(ctx)
            result = adapter.process_active_worktrees()

        assert isinstance(result.completion_events, tuple)
        assert isinstance(result.deviation_events, tuple)
        assert result.any_forced_serial is True
