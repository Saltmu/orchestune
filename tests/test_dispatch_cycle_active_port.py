"""#884: CycleActionAdapter (process_active_worktrees/run_gc only).

Wiring the adapter into the live `cycle.py` pipeline is #873's job; these
tests exercise `CycleActionAdapter` directly against a bound `CycleQueries`
view (a real `CycleContext`, which already satisfies the Protocol), and
compare it against the legacy `phase_reconciliation._process_active_worktrees`
path it was relocated from.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestune.dispatch.cycle_actions import CycleActionAdapter
from orchestune.dispatch.gc.completion import is_completion_hold_event
from orchestune.dispatch.phase_reconciliation import _process_active_worktrees
from orchestune.dispatch.state import RunState
from orchestune.models import PrRecord
from tests.dispatch_gc_test_support import _active, _ctx, _task


class TestBindContext:
    def test_using_either_port_before_bind_raises(self, tmp_path):
        adapter = CycleActionAdapter(RunState(active_worktrees={}), _ctx().config, 0.0)

        with pytest.raises(ValueError):
            adapter.process_active_worktrees()
        with pytest.raises(ValueError):
            adapter.run_gc(())

    def test_binding_twice_raises(self):
        adapter = CycleActionAdapter(RunState(active_worktrees={}), _ctx().config, 0.0)
        view = _ctx()

        adapter.bind_context(view)

        with pytest.raises(ValueError):
            adapter.bind_context(view)


class TestProcessActiveWorktrees:
    def test_returns_immutable_tuples(self, fake_forge):
        active = _active(pid=123, started_at=1_699_999_000.0)
        task = _task(status_labels=("status:in-progress",))
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(forge=fake_forge, tasks_by_issue={280: task}, run_state=run_state)
        ctx.config.apply = True
        adapter = CycleActionAdapter(run_state, ctx.config, now=0.0)
        adapter.bind_context(ctx)

        with patch(
            "orchestune.dispatch.gc.is_process_alive", autospec=True, return_value=True
        ):
            result = adapter.process_active_worktrees()

        assert isinstance(result.completion_events, tuple)
        assert isinstance(result.deviation_events, tuple)

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
        assert args[1] == {280: task}
        assert args[2] is ctx.config
        assert args[3] == list(events)
        assert args[4] == (pr,)
        assert kwargs["now"] == 123.0
        assert result is mock_run_gc.return_value


class TestParityWithLegacyPath:
    """The relocated loop must reproduce the pre-#884 behavior exactly."""

    def _build(self, fake_forge, *, forced_serial: bool):
        active = _active(
            pid=123, started_at=1_699_999_000.0, forced_serial=forced_serial
        )
        task = _task(status_labels=("status:in-progress",), depends_on=())
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(forge=fake_forge, tasks_by_issue={280: task}, run_state=run_state)
        ctx.config.apply = True
        return ctx, run_state

    def test_matches_legacy_completion_events_and_forced_serial(self, fake_forge):
        legacy_ctx, _ = self._build(fake_forge, forced_serial=True)
        new_ctx, new_run_state = self._build(fake_forge, forced_serial=True)

        with patch(
            "orchestune.dispatch.gc.is_process_alive", autospec=True, return_value=True
        ):
            (
                legacy_completion,
                legacy_deviation,
                legacy_forced_serial,
                _,
            ) = _process_active_worktrees(legacy_ctx)

            adapter = CycleActionAdapter(new_run_state, new_ctx.config, now=0.0)
            adapter.bind_context(new_ctx)
            result = adapter.process_active_worktrees()

        assert list(result.completion_events) == legacy_completion
        assert list(result.deviation_events) == legacy_deviation
        assert result.any_forced_serial == legacy_forced_serial is True
