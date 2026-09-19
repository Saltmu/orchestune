"""CycleActionAdapter active-worktree and GC ports."""

from __future__ import annotations

from unittest.mock import patch

from orchestune.dispatch.cycle_actions import CycleActionAdapter
from orchestune.dispatch.gc.completion import is_completion_hold_event
from orchestune.dispatch.state import RunState
from orchestune.models import PrRecord
from orchestune.task_metadata import CycleTask
from tests.dispatch_gc_test_support import _active, _ctx, _task


class TestProcessActiveWorktrees:
    def test_completion_reaches_the_adapters_own_run_state(self, fake_forge):
        active = _active(pid=123, started_at=1_699_999_000.0)
        task = _task(status_labels=("status:in-progress",))
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(forge=fake_forge, tasks_by_issue={280: task}, run_state=run_state)
        ctx.config.apply = True
        adapter = CycleActionAdapter(run_state, ctx.config, now=1_700_000_000.0)
        adapter.bind_context(ctx)

        with (
            patch(
                "orchestune.dispatch.gc._is_worktree_complete",
                autospec=True,
                return_value=True,
            ),
            patch(
                "orchestune.dispatch.gc._finalize_completed_worktree",
                autospec=True,
                return_value={"action": "completed", "commit_sha": "abc123d"},
            ),
            patch("orchestune.dispatch.gc.save_run_state", autospec=True) as mock_save,
        ):
            result = adapter.process_active_worktrees()

        # The mutation and the save both landed on the exact object the
        # adapter was constructed with -- not a copy.
        assert mock_save.call_args.args[0] is run_state
        assert "1" not in run_state.active_worktrees
        assert len(run_state.completed_worktrees) == 1
        assert len(result.completion_events) == 1
        assert result.completion_events[0]["action"] == "completed"

    def test_dirty_hold_event_is_still_recognized_downstream(self, fake_forge):
        active = _active(pid=123, started_at=1_699_999_000.0)
        task = _task(status_labels=("status:in-progress",))
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(forge=fake_forge, tasks_by_issue={280: task}, run_state=run_state)
        ctx.config.apply = True
        adapter = CycleActionAdapter(run_state, ctx.config, now=0.0)
        adapter.bind_context(ctx)

        with (
            patch(
                "orchestune.dispatch.gc._is_worktree_complete",
                autospec=True,
                return_value=True,
            ),
            patch(
                "orchestune.dispatch.gc._finalize_completed_worktree",
                autospec=True,
                return_value={
                    "action": "completion_skipped_dirty_worktree",
                    "worktree_path": active.worktree_path,
                },
            ),
        ):
            result = adapter.process_active_worktrees()

        assert len(result.completion_events) == 1
        assert is_completion_hold_event(result.completion_events[0])

        with patch(
            "orchestune.dispatch.cycle_actions.run_gc_phase", autospec=True
        ) as mock_run_gc:
            adapter.run_gc(result.completion_events)

        passed_events = mock_run_gc.call_args.args[3]
        assert passed_events == list(result.completion_events)
        assert is_completion_hold_event(passed_events[0])


class TestExecutionContext:
    def test_issue_number_by_subtask_id_is_populated_from_the_bound_view(self):
        """Codex #900 review: this map is not display-only -- footprint
        deviation handling (`rebase.notify_recompute`) uses it to find and
        transition the actually-blocked issue, so it must be reconstructed
        from the bound view rather than left empty.
        """
        run_state = RunState(active_worktrees={})
        upstream = _task(issue_number=280, subtask_id="task-a")
        downstream = _task(issue_number=281, subtask_id="task-b")
        no_subtask = _task(issue_number=282, subtask_id="")
        ctx = _ctx(
            tasks_by_issue={280: upstream, 281: downstream, 282: no_subtask},
            run_state=run_state,
        )
        adapter = CycleActionAdapter(run_state, ctx.config, now=0.0)
        adapter.bind_context(ctx)

        execution_ctx = adapter._execution_context()

        assert execution_ctx.issue_number_by_subtask_id == {
            "task-a": 280,
            "task-b": 281,
        }


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
            "orchestune.dispatch.cycle_actions.run_gc_phase", autospec=True
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
            pid=123, started_at=1_699_999_000.0, forced_serial=forced_serial
        )
        task = _task(status_labels=("status:in-progress",), depends_on=())
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(forge=fake_forge, tasks_by_issue={280: task}, run_state=run_state)
        ctx.config.apply = True
        return ctx, run_state

    def test_reports_completion_events_and_forced_serial(self, fake_forge):
        ctx, run_state = self._build(fake_forge, forced_serial=True)

        with patch(
            "orchestune.dispatch.gc.is_process_alive", autospec=True, return_value=True
        ):
            adapter = CycleActionAdapter(run_state, ctx.config, now=0.0)
            adapter.bind_context(ctx)
            result = adapter.process_active_worktrees()

        assert isinstance(result.completion_events, tuple)
        assert isinstance(result.deviation_events, tuple)
        assert result.any_forced_serial is True
