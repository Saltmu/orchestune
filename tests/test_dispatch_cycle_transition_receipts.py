"""#883: apply_verified_transition and its status-executor/recovery wiring.

`apply_verified_transition(ctx, receipt, *, execution_active)` is a thin
bridge over #868's already-tested `CycleContext.record_transition`:
`execution_active=None` (the caller's execution observation is unknown this
cycle) holds -- `record_transition` is never called, so an uncertain
execution claim can never retire or assert a launch fact. Every other
CONFLICT/NOOP/APPLIED outcome comes straight from `record_transition`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from orchestune.consistency.intents import IntentJournal
from orchestune.consistency.models import RepairStatus
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_context_state import (
    REASON_STALE_OBSERVATION,
    REASON_TERMINAL_STATE,
    RecordStatus,
)
from orchestune.dispatch.cycle_records import (
    _on_status_transition_verified,
    apply_verified_transition,
)
from orchestune.dispatch.dependency_resolution import TaskDependencies
from orchestune.dispatch.reconciliation import (
    _handle_blocked_recompute_recovery,
)
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.state import RunState
from orchestune.dispatch.status_repair import (
    VerifiedStatusTransition,
    execute_status_repair_command,
)
from orchestune.labels import StatusLabel
from orchestune.models import Task
from tests.conftest import make_issue, make_task
from tests.test_consistency_status_repair import _config, _plan

_DEFAULTS = dict(
    run_state=RunState(active_worktrees={}),
    tasks_by_issue={},
    issue_number_by_subtask_id={},
    dependency_resolution={},
    done_issue_numbers=set(),
    ci_passed_pr_issue_numbers=set(),
    changes_requested_issue_numbers=set(),
    branch_by_issue_number={},
    prs=[],
    pr_by_branch={},
)


def _ctx(tmp_path, **overrides):
    defaults = dict(_DEFAULTS)
    defaults["config"] = DispatcherConfig(
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "run_state.json",
        worktree_root=tmp_path / "worktrees",
    )
    defaults.update(overrides)
    return CycleContext(**defaults)


def _task(**overrides):
    defaults = dict(
        issue_number=280,
        subtask_id="task-a",
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:blocked",),
        created_at="2026-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return Task(**defaults)


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


class TestStatusExecutorWiring:
    """`_on_status_transition_verified` bridges the real typed executor."""

    def test_verified_transition_reaches_context(self, tmp_path, in_memory_forge):
        before = (StatusLabel.DONE, StatusLabel.QUEUED)
        task = make_task(1, status_labels=before, parent_number=None)
        in_memory_forge.seed_issue(make_issue(1, labels=before))
        ctx = _ctx(tmp_path, tasks_by_issue={1: task})

        result = _execute(
            _remove_done_command(task),
            {1: task},
            _config(tmp_path, in_memory_forge),
            _on_status_transition_verified(ctx),
        )

        assert result.status is RepairStatus.APPLIED
        assert ctx.task(1).status_labels == (StatusLabel.QUEUED,)

    def test_conflict_becomes_failed_diagnostic_without_forge_rollback(
        self, tmp_path, in_memory_forge
    ):
        before = (StatusLabel.DONE, StatusLabel.QUEUED)
        task = make_task(1, status_labels=before, parent_number=None)
        in_memory_forge.seed_issue(make_issue(1, labels=before))
        # A ctx that does not know issue #1 forces `record_transition` to
        # CONFLICT (`unknown-issue`) once the callback runs.
        ctx = _ctx(tmp_path, tasks_by_issue={})

        result = _execute(
            _remove_done_command(task),
            {1: task},
            _config(tmp_path, in_memory_forge),
            _on_status_transition_verified(ctx),
        )

        assert result.status is RepairStatus.FAILED
        assert "unknown-issue" in result.diagnostics[0]
        # Forge already committed the label change; the conflict must not
        # roll it back.
        assert in_memory_forge.get_issue_labels(1) == (StatusLabel.QUEUED,)

    def test_journal_failure_leaves_context_untouched(self, tmp_path, in_memory_forge):
        before = (StatusLabel.DONE, StatusLabel.QUEUED)
        task = make_task(1, status_labels=before, parent_number=None)
        in_memory_forge.seed_issue(make_issue(1, labels=before))
        ctx = _ctx(tmp_path, tasks_by_issue={1: task})

        with patch.object(
            IntentJournal,
            "mark_verified",
            autospec=True,
            side_effect=OSError("journal verification failed"),
        ):
            result = _execute(
                _remove_done_command(task),
                {1: task},
                _config(tmp_path, in_memory_forge),
                _on_status_transition_verified(ctx),
            )

        assert result.status is RepairStatus.FAILED
        # Forge succeeded, but the callback was never invoked -> ctx keeps
        # its original (pre-repair) view rather than the live Forge state.
        assert ctx.task(1).status_labels == before
        assert in_memory_forge.get_issue_labels(1) == (StatusLabel.QUEUED,)


class TestRecomputeRecoveryWiring:
    def test_blocked_recompute_recovery_confirms_queued_transition(self, tmp_path):
        fake_forge = MagicMock()
        fake_forge.get_issue_state.return_value = "OPEN"
        fake_forge.get_issue_labels.return_value = (StatusLabel.QUEUED,)
        ctx = _ctx(
            tmp_path,
            tasks_by_issue={
                1: _task(
                    issue_number=1,
                    status_labels=(StatusLabel.BLOCKED,),
                    depends_on=(),
                )
            },
            dependency_resolution={1: TaskDependencies()},
        )
        ctx.config.apply = True
        ctx.config.forge = fake_forge

        class _Issues:
            def all(self):
                return [make_issue(1, labels=(StatusLabel.BLOCKED_RECOMPUTE,))]

        events = _handle_blocked_recompute_recovery(
            _Issues(), ctx.run_state, ctx, set(), ctx.config
        )

        assert events == [{"issue_number": 1, "subtask_id": "task-a"}]
        assert ctx.task(1).status_labels == (StatusLabel.QUEUED,)

    def test_transient_forge_read_failure_does_not_abort_the_recovery(self, tmp_path):
        """Codex #899 review: a live-verify read failure must fail closed
        (no receipt) rather than propagate and abort the whole recovery/cycle.
        """
        fake_forge = MagicMock()
        fake_forge.get_issue_state.side_effect = RuntimeError("transient API error")
        ctx = _ctx(
            tmp_path,
            tasks_by_issue={
                1: _task(
                    issue_number=1,
                    status_labels=(StatusLabel.BLOCKED,),
                    depends_on=(),
                )
            },
            dependency_resolution={1: TaskDependencies()},
        )
        ctx.config.apply = True
        ctx.config.forge = fake_forge

        class _Issues:
            def all(self):
                return [make_issue(1, labels=(StatusLabel.BLOCKED_RECOMPUTE,))]

        events = _handle_blocked_recompute_recovery(
            _Issues(), ctx.run_state, ctx, set(), ctx.config
        )

        # The label mutation and promotion event still happen; only the
        # ctx-side confirmation is withheld.
        assert events == [{"issue_number": 1, "subtask_id": "task-a"}]
        assert ctx.task(1).status_labels == (StatusLabel.BLOCKED,)
