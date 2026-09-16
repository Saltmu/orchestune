"""#885: CycleActionAdapter (scan_external_locks/select_tasks/launch_tasks).

Wiring these ports into the live `cycle.py` pipeline is #873's job; these
tests exercise `CycleActionAdapter` directly against a bound `CycleQueries`
view (a real `CycleContext`, which already satisfies the Protocol).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestune.dispatch.cycle_action_contracts import StackBase
from orchestune.dispatch.cycle_actions import CycleActionAdapter
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.task_metadata import CycleTask
from tests.dispatch_gc_test_support import _ctx, _task


class TestBindContextContractExtendsToNewPorts:
    def test_using_any_of_the_three_ports_before_bind_raises(self):
        adapter = CycleActionAdapter(RunState(active_worktrees={}), _ctx().config, 0.0)

        with pytest.raises(ValueError):
            adapter.scan_external_locks()
        with pytest.raises(ValueError):
            adapter.select_tasks(())
        with pytest.raises(ValueError):
            adapter.launch_tasks((), (), ())


class TestScanExternalLocks:
    def test_delegates_to_the_existing_sync_with_adapter_state(self):
        run_state = RunState(active_worktrees={})
        task = _task(status_labels=("status:queued",))
        ctx = _ctx(tasks_by_issue={280: task}, run_state=run_state)
        adapter = CycleActionAdapter(run_state, ctx.config, now=0.0)
        adapter.bind_context(ctx)

        with patch(
            "orchestune.dispatch.cycle_actions._sync_external_locks", autospec=True
        ) as mock_sync:
            result = adapter.scan_external_locks()

        args, kwargs = mock_sync.call_args
        assert args[0] == {280: CycleTask.from_task(task)}
        assert args[1] == []
        assert args[2] is run_state
        assert args[3] is ctx.config
        assert kwargs["view"] is ctx
        assert result is mock_sync.return_value


class TestSelectTasks:
    def test_selection_is_score_based_not_candidate_input_order(self):
        run_state = RunState(active_worktrees={})
        low = _task(
            issue_number=1, subtask_id="task-1", priority="low", status_labels=()
        )
        high = _task(
            issue_number=2, subtask_id="task-2", priority="high", status_labels=()
        )
        ctx = _ctx(tasks_by_issue={1: low, 2: high}, run_state=run_state)
        ctx.config.max_launches_per_window = 1
        adapter = CycleActionAdapter(run_state, ctx.config, now=1_800_000_000.0)
        adapter.bind_context(ctx)

        # Candidates passed in ascending issue-number order (low priority
        # first) -- the adapter must not assume or preserve this as the
        # selection order; only the underlying selector's scoring decides.
        result = adapter.select_tasks((low, high))

        assert len(result.decisions) == 2
        assert [task.issue_number for task in result.selected] == [2]

    def test_uses_the_full_task_population_for_quota_and_conflict_graph(self):
        """`queries.tasks()` (not just `candidates`) must feed
        `known_tasks`/`build_task_conflict_graph`, per the Issue body.
        """
        run_state = RunState(active_worktrees={})
        candidate = _task(
            issue_number=1, subtask_id="task-1", status_labels=(), depends_on=()
        )
        # Not in `candidates`, but part of the full population `known_tasks`
        # must see so critical-path ranking is not underestimated.
        downstream = _task(
            issue_number=2,
            subtask_id="task-2",
            status_labels=(),
            depends_on=("task-1",),
        )
        ctx = _ctx(tasks_by_issue={1: candidate, 2: downstream}, run_state=run_state)
        adapter = CycleActionAdapter(run_state, ctx.config, now=1_800_000_000.0)
        adapter.bind_context(ctx)

        with patch(
            "orchestune.dispatch.cycle_actions.select_tasks_with_decisions",
            autospec=True,
        ) as mock_select:
            adapter.select_tasks((candidate,))

        _, kwargs = mock_select.call_args
        assert set(kwargs["known_tasks"]) == {
            CycleTask.from_task(candidate),
            CycleTask.from_task(downstream),
        }


class TestLaunchTasks:
    def _adapter(self, run_state, ctx):
        adapter = CycleActionAdapter(run_state, ctx.config, now=1.0)
        adapter.bind_context(ctx)
        return adapter

    def test_dry_run_returns_selected_without_launching(self):
        run_state = RunState(active_worktrees={})
        task = _task()
        ctx = _ctx(tasks_by_issue={280: task}, run_state=run_state)
        ctx.config.apply = False
        adapter = self._adapter(run_state, ctx)

        with patch(
            "orchestune.dispatch.cycle_actions._launch_selected_tasks", autospec=True
        ) as mock_launch:
            result = adapter.launch_tasks((task,), (), (task,))

        mock_launch.assert_not_called()
        assert result == (task,)

    def test_converts_stack_bases_to_a_local_map_without_mutating_the_input(self):
        run_state = RunState(active_worktrees={})
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx(tasks_by_issue={280: task}, run_state=run_state)
        ctx.config.apply = True
        adapter = self._adapter(run_state, ctx)
        bases = (StackBase(issue_number=999, branch="parent/issue-100"),)
        bases_snapshot = tuple(bases)

        with (
            patch(
                "orchestune.dispatch.cycle_actions._launch_selected_tasks",
                autospec=True,
                return_value=[],
            ) as mock_launch,
            patch("orchestune.dispatch.cycle_actions.save_run_state", autospec=True),
        ):
            adapter.launch_tasks((), bases, ())

        launch_ctx = mock_launch.call_args.args[0]
        assert launch_ctx.task_to_base_branch == {999: "parent/issue-100"}
        assert bases == bases_snapshot

    def test_calls_the_underlying_launch_exactly_once(self):
        run_state = RunState(active_worktrees={})
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx(tasks_by_issue={280: task}, run_state=run_state)
        ctx.config.apply = True
        adapter = self._adapter(run_state, ctx)

        with (
            patch(
                "orchestune.dispatch.cycle_actions._launch_selected_tasks",
                autospec=True,
                return_value=[task],
            ) as mock_launch,
            patch("orchestune.dispatch.cycle_actions.save_run_state", autospec=True),
        ):
            result = adapter.launch_tasks((task,), (), (task,))

        mock_launch.assert_called_once()
        assert result == (task,)

    def test_committed_launch_fact_survives_a_later_forge_failure(self):
        """The only record point is the post-save `on_launch_committed`
        callback (#871's mechanism): once it fires, a later failure inside
        the same launch batch must not roll back the recorded fact.
        """
        run_state = RunState(active_worktrees={})
        task = _task(status_labels=("status:queued",))
        ctx = _ctx(tasks_by_issue={280: task}, run_state=run_state)
        ctx.config.apply = True
        adapter = self._adapter(run_state, ctx)
        active = ActiveWorktree(
            issue_number=280,
            branch="claude/issue-280-task-a",
            worktree_path="worktrees/w1",
            pid=123,
            started_at=1.0,
            declared_footprint=(),
            launch_phase="launched",
        )

        def _launch(launch_ctx):
            launch_ctx.on_launch_committed(active)
            raise RuntimeError("label API failed")

        with (
            patch(
                "orchestune.dispatch.cycle_actions._launch_selected_tasks",
                autospec=True,
                side_effect=_launch,
            ),
            pytest.raises(RuntimeError, match="label API failed"),
        ):
            adapter.launch_tasks((task,), (), (task,))

        assert ctx.launch_fact(280) is not None

    def test_record_launch_conflict_propagates(self):
        run_state = RunState(active_worktrees={})
        ctx = _ctx(tasks_by_issue={}, run_state=run_state)
        ctx.config.apply = True
        adapter = self._adapter(run_state, ctx)
        active = ActiveWorktree(
            issue_number=999,
            branch="claude/issue-999-unknown",
            worktree_path="worktrees/unknown",
            pid=123,
            started_at=1.0,
            declared_footprint=(),
            launch_phase="launched",
        )

        def _launch(launch_ctx):
            launch_ctx.on_launch_committed(active)
            raise AssertionError("record conflict must stop the launch batch")

        with (
            patch(
                "orchestune.dispatch.cycle_actions._launch_selected_tasks",
                autospec=True,
                side_effect=_launch,
            ),
            pytest.raises(
                RuntimeError,
                match="record_launch conflict for issue #999: unknown-issue",
            ),
        ):
            adapter.launch_tasks((), (), ())
