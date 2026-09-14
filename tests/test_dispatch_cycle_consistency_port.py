"""#886: CycleActionAdapter (reconcile_recovery/execute_repair).

Wiring these ports into the live `cycle.py` pipeline is #873's job; these
tests exercise `CycleActionAdapter` directly against a bound `CycleQueries`
view (a real `CycleContext`, which already satisfies the Protocol).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from orchestune.consistency.models import ConsistencyScope, RepairCommand
from orchestune.consistency.repairs.execution import COMMAND_BOOKKEEPING
from orchestune.consistency.repairs.status import COMMAND_TRANSITION_LABEL
from orchestune.dispatch.cycle_actions import CycleActionAdapter
from orchestune.dispatch.state import RunState
from orchestune.labels import StatusLabel
from tests.conftest import make_issue, make_task
from tests.dispatch_gc_test_support import _ctx, _task
from tests.test_consistency_status_repair import _config, _plan


class TestBindContextContractExtendsToConsistencyPorts:
    def test_using_either_port_before_bind_raises(self):
        adapter = CycleActionAdapter(RunState(active_worktrees={}), _ctx().config, 0.0)

        with pytest.raises(ValueError):
            adapter.reconcile_recovery()
        with pytest.raises(ValueError):
            adapter.execute_repair(
                RepairCommand(
                    code=COMMAND_BOOKKEEPING,
                    scope=ConsistencyScope.TASK,
                    subject_id="1",
                    idempotency_key="k",
                )
            )

    def test_a_non_cyclecontext_cyclequeries_binding_fails_closed_and_clearly(self):
        """#886 Codex review: `reconcile_recovery`/`execute_repair`'s status
        branch reuse existing helpers written against the concrete
        `CycleContext` (not just the declared `CycleQueries` Protocol
        surface `bind_context` accepts) -- binding anything else must raise
        a clear `TypeError` here, not an `AttributeError` deep inside
        `reconciliation.py`/`cycle_records.py`.
        """
        not_a_cycle_context = MagicMock()
        run_state = RunState(active_worktrees={})
        adapter = CycleActionAdapter(run_state, _ctx().config, now=0.0)
        adapter.bind_context(not_a_cycle_context)

        with pytest.raises(TypeError):
            adapter.reconcile_recovery()
        with pytest.raises(TypeError):
            adapter.execute_repair(
                RepairCommand(
                    code=COMMAND_TRANSITION_LABEL,
                    scope=ConsistencyScope.TASK,
                    subject_id="1",
                    idempotency_key="k",
                )
            )
        # The non-status fail-closed path needs no concrete CycleContext.
        result = adapter.execute_repair(
            RepairCommand(
                code=COMMAND_BOOKKEEPING,
                scope=ConsistencyScope.TASK,
                subject_id="1",
                idempotency_key="k",
            )
        )
        assert result.status.value == "failed"


class TestExecuteRepair:
    def test_non_status_command_fails_closed_without_a_generic_handler(self):
        run_state = RunState(active_worktrees={})
        ctx = _ctx(tasks_by_issue={}, run_state=run_state)
        adapter = CycleActionAdapter(run_state, ctx.config, now=0.0)
        adapter.bind_context(ctx)
        command = RepairCommand(
            code=COMMAND_BOOKKEEPING,
            scope=ConsistencyScope.TASK,
            subject_id="1",
            idempotency_key="k",
        )

        result = adapter.execute_repair(command)

        assert result.status.value == "failed"

    def test_status_command_applies_against_freshly_refetched_state(
        self, tmp_path, in_memory_forge
    ):
        """The fresh executor re-fetches Forge state itself (#872's
        resolver/Assessment/policy), rather than reusing the adapter's
        construction-time view -- matching cycle.py's `_DispatchConsistencyAdapter`
        fresh/cached split.
        """
        before = (StatusLabel.DONE, StatusLabel.QUEUED)
        task = make_task(1, status_labels=before, parent_number=None)
        in_memory_forge.seed_issue(make_issue(1, labels=before))
        run_state = RunState(active_worktrees={})
        config = _config(tmp_path, in_memory_forge)
        ctx = _ctx(tasks_by_issue={1: task}, run_state=run_state, config=config)
        adapter = CycleActionAdapter(run_state, config, now=0.0)
        adapter.bind_context(ctx)
        command = _plan({1: task})[1][0]

        result = adapter.execute_repair(command)

        assert result.status.value == "applied"
        assert in_memory_forge.get_issue_labels(1) == (StatusLabel.QUEUED,)
        # The transition was recorded onto the *bound* view -- the canonical
        # context the rest of the cycle observes -- not a throwaway fresh one.
        assert ctx.task(1).status_labels == (StatusLabel.QUEUED,)

    def test_conflict_becomes_failed_diagnostic_without_forge_rollback(
        self, tmp_path, in_memory_forge
    ):
        before = (StatusLabel.DONE, StatusLabel.QUEUED)
        task = make_task(1, status_labels=before, parent_number=None)
        in_memory_forge.seed_issue(make_issue(1, labels=before))
        run_state = RunState(active_worktrees={})
        config = _config(tmp_path, in_memory_forge)
        # A ctx that does not know issue #1 forces record_transition to
        # CONFLICT (unknown-issue) once the callback runs.
        ctx = _ctx(tasks_by_issue={}, run_state=run_state, config=config)
        adapter = CycleActionAdapter(run_state, config, now=0.0)
        adapter.bind_context(ctx)
        command = _plan({1: task})[1][0]

        result = adapter.execute_repair(command)

        assert result.status.value == "failed"
        assert "unknown-issue" in result.diagnostics[0]
        # Forge already committed the label change; the conflict must not
        # roll it back.
        assert in_memory_forge.get_issue_labels(1) == (StatusLabel.QUEUED,)


class TestReconcileRecovery:
    def test_blocked_recompute_recovery_reaches_the_bound_context(self, tmp_path):
        run_state = RunState(active_worktrees={})
        task = _task(
            issue_number=1, status_labels=(StatusLabel.BLOCKED,), depends_on=()
        )
        from orchestune.dispatch.dependency_resolution import TaskDependencies

        ctx = _ctx(
            tasks_by_issue={1: task},
            run_state=run_state,
            dependency_resolution={1: TaskDependencies()},
        )
        ctx.config.apply = True
        fake_forge = MagicMock()
        fake_forge.get_issue_state.return_value = "OPEN"
        fake_forge.get_issue_labels.return_value = (StatusLabel.QUEUED,)
        ctx.config.forge = fake_forge
        adapter = CycleActionAdapter(run_state, ctx.config, now=0.0)
        adapter.bind_context(ctx)

        with (
            patch.object(
                ctx,
                "issue_records",
                return_value=(make_issue(1, labels=(StatusLabel.BLOCKED_RECOMPUTE,)),),
            ),
            patch(
                "orchestune.dispatch.reconciliation.check_footprint_deviation",
                autospec=True,
                return_value=(),
            ),
        ):
            events = adapter.reconcile_recovery()

        assert events == ({"issue_number": 1, "subtask_id": "task-a"},)
        assert ctx.task(1).status_labels == (StatusLabel.QUEUED,)

    def test_requires_only_bind_context_not_a_populated_active_worktrees(self):
        """No arguments beyond `self`: everything comes from the bound view
        and the adapter's own run_state.
        """
        run_state = RunState(active_worktrees={})
        ctx = _ctx(tasks_by_issue={}, run_state=run_state)
        adapter = CycleActionAdapter(run_state, ctx.config, now=0.0)
        adapter.bind_context(ctx)

        events = adapter.reconcile_recovery()

        assert events == ()

    def test_uses_the_same_cycle_completions_process_active_worktrees_computed(
        self,
    ):
        """#886 Codex review (P1): `record_completion`'s same-cycle side
        effect does not substitute for the explicit `completed_issue_numbers`
        overlay -- dry runs never call `record_completion`, and
        `_rule_not_needed` can report an outcome-based completion without
        recording one. `reconcile_recovery` must reuse the same set
        `process_active_worktrees` computed this cycle, matching cycle.py's
        existing `run_post_gc_reconciliation` call (which passes
        `completed_in_cycle` through unchanged).
        """
        run_state = RunState(active_worktrees={})
        ctx = _ctx(tasks_by_issue={}, run_state=run_state)
        adapter = CycleActionAdapter(run_state, ctx.config, now=0.0)
        adapter.bind_context(ctx)

        with patch(
            "orchestune.dispatch.cycle_actions._run_active_worktree_rules",
            return_value=([], [], False, {7, 9}),
        ):
            adapter.process_active_worktrees()

        with (
            patch.object(ctx, "issue_records", return_value=()),
            patch(
                "orchestune.dispatch.cycle_actions._handle_blocked_recompute_recovery",
                return_value=[],
            ) as blocked_recompute,
            patch(
                "orchestune.dispatch.cycle_actions._handle_base_branch_red_recovery",
                return_value=[],
            ) as base_branch_red,
        ):
            adapter.reconcile_recovery()

        assert blocked_recompute.call_args.args[3] == {7, 9}
        assert base_branch_red.call_args.args[2] == {7, 9}
