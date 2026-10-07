"""Unit cases for the pure Event model (#1264, design #1219 §1).

Expected values come from the specification (the issue body and the
production docstrings), not from `apply_event` or the retry helpers it calls.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from orchestune.labels import StatusLabel
from orchestune.ledger.status_events import (
    ALLOWED_TRANSITIONS,
    EVENT_SPECS,
    Applied,
    BackoffState,
    BudgetCounts,
    BudgetLimits,
    Event,
    EventInput,
    ExecutionIdentity,
    IndeterminateExecution,
    Kind,
    NoOp,
    ReclaimState,
    Rejected,
    RetryStates,
    Stage,
    TaskModel,
    apply_event,
    restart,
)
from tests.conftest import make_issue
from tests.dispatch_test_support import make_test_dispatcher_config
from tests.status_event_cases import (
    EVENT_CASES,
    FAIL_BEFORE,
    EventCase,
)
from tests.status_event_test_support import (
    EVENT_BY_SOURCE,
    LABEL_INVARIANT_COMPLETIONS,
    CaseEnv,
    FaultyForge,
    production_limits,
    route_for,
)
from tests.status_transition_callsite_drivers import ISSUE
from tests.test_status_transition_callsites import (
    CALL_SITES,
    CASES,
    OUT_OF_SCOPE_PATHS,
    Case,
)
from tests.test_status_transition_callsites import (
    _run as _run_callsite_case,
)

Q = StatusLabel.QUEUED
B = StatusLabel.BLOCKED
P = StatusLabel.IN_PROGRESS
D = StatusLabel.DONE
N = StatusLabel.NOT_NEEDED
H = StatusLabel.BLOCKED_HUMAN_REVIEW
M = StatusLabel.MANUAL_MERGE_REQUIRED
RC = StatusLabel.BLOCKED_RECOMPUTE
FS = StatusLabel.FORCE_SERIAL
EL = StatusLabel.EXTERNAL_LOCK
CI_RED = "ci:base-branch-red"
LIMITS = production_limits()
A = ExecutionIdentity("launch-a")
A2 = ExecutionIdentity("launch-b")


def _model(*labels: str, **overrides: object) -> TaskModel:
    return replace(TaskModel.from_labels(labels), **overrides)  # type: ignore[arg-type]


def _apply(
    state: TaskModel,
    event: Event,
    kind: Kind = Kind.PLAIN,
    limits: BudgetLimits = LIMITS,
    **fields: object,
) -> Applied | Rejected | NoOp:
    return apply_event(state, EventInput(event, kind, **fields), limits)  # type: ignore[arg-type]


def _applied(result: Applied | Rejected | NoOp) -> Applied:
    assert isinstance(result, Applied), result
    return result


class TestModelShape:
    def test_from_labels_splits_lifecycle_and_auxiliary(self) -> None:
        state = TaskModel.from_labels((Q, FS, CI_RED, "priority:high"))
        assert state.lifecycle == {Q}
        assert state.auxiliary == {FS, CI_RED}
        assert state.labels == {Q, FS, CI_RED}

    def test_from_labels_preserves_unknown_status_labels_for_strip(self) -> None:
        state = TaskModel.from_labels((Q, "status:legacy", "component:core"))
        assert state.lifecycle == {Q}
        assert state.auxiliary == {"status:legacy"}
        assert state.labels == {Q, "status:legacy"}

        applied = _applied(
            apply_event(
                state,
                EventInput(
                    Event.LAUNCH,
                    Kind.CLAIM,
                    execution=ExecutionIdentity("c1"),
                ),
                LIMITS,
            )
        )
        assert applied.state.labels == {P}
        assert "status:legacy" in applied.remove_labels

    def test_execution_active_is_derived_from_the_identity(self) -> None:
        assert not _model(P).execution_active
        assert _model(P, execution_identity=A).execution_active
        assert _model(
            P, execution_identity=IndeterminateExecution("prepared")
        ).execution_active

    def test_every_spec_target_pair_is_allowed_except_documented_reverts(self) -> None:
        exceptions = set()
        for (event, _kind), spec in EVENT_SPECS.items():
            if spec.target is None:
                continue
            for source in spec.sources:
                if (
                    source != spec.target
                    and (source, spec.target) not in ALLOWED_TRANSITIONS
                ):
                    exceptions.add((event, source, spec.target))
        assert exceptions == {
            (Event.MERGE_REVERT, D, Q),
            (Event.REVIEW_REJECT, N, Q),
            # replan retires escalated Issues too; it is an operator action.
            (Event.NOT_NEEDED, H, N),
            (Event.NOT_NEEDED, M, N),
        }

    def test_unknown_event_kind_pair_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="kind"):
            _apply(_model(Q), Event.QUEUE, Kind.EARLY_DEATH)

    def test_stop_requires_an_operation_identity(self) -> None:
        with pytest.raises(ValueError, match="operation"):
            _apply(_model(D), Event.MERGE_REVERT, stop_after=Stage.LABEL_ADDED)


class TestTransitions:
    def test_normal_transition_plans_add_and_removal(self) -> None:
        result = _applied(_apply(_model(B), Event.QUEUE))
        assert result.state.lifecycle == {Q}
        assert result.plan is not None
        assert (result.plan.add, result.plan.remove) == (Q, (B,))

    def test_self_transition_is_applied_not_deduplicated_by_label(self) -> None:
        result = _applied(_apply(_model(Q), Event.QUEUE))
        assert result.state.labels == {Q}
        assert result.plan is not None and result.plan.remove == ()

    def test_initial_assignment_and_repair_of_several_lifecycle_labels(self) -> None:
        assert _applied(_apply(_model(), Event.BLOCK)).state.lifecycle == {B}
        assert _applied(
            _apply(_model(Q, B), Event.LAUNCH, execution=A)
        ).state.lifecycle == {P}

    @pytest.mark.parametrize(
        ("held", "event", "reason"),
        [
            ((H,), Event.QUEUE, "escalation-held"),
            ((M,), Event.REQUEUE, "escalation-held"),
            ((H,), Event.RECLAIM, "escalation-held"),
            ((D,), Event.QUEUE, "final-held"),
            ((P,), Event.QUEUE, "invalid-source"),
            ((), Event.MERGE_REVERT, "no-lifecycle"),
        ],
    )
    def test_invalid_events_are_rejected_without_state_change(
        self, held: tuple[str, ...], event: Event, reason: str
    ) -> None:
        kind = Kind.RECOVERY if event is Event.REQUEUE else Kind.PLAIN
        result = _apply(_model(*held), event, kind)
        assert result == Rejected(reason)

    def test_auxiliary_effects_follow_the_event_kind(self) -> None:
        recompute = _applied(_apply(_model(Q), Event.BLOCK, Kind.RECOMPUTE))
        assert recompute.state.labels == {B, RC}
        released = _applied(_apply(_model(B, RC), Event.QUEUE, Kind.RECOMPUTE))
        assert released.state.labels == {Q}
        hold = _applied(_apply(_model(B, RC), Event.RELEASE_HOLD, Kind.RECOMPUTE))
        assert hold.state.labels == {B} and hold.plan is None
        assert hold.remove_labels == (RC,)

    def test_claim_strips_auxiliary_status_labels_but_not_ci_markers(self) -> None:
        result = _applied(
            _apply(_model(B, RC, EL, CI_RED), Event.LAUNCH, Kind.CLAIM, execution=A)
        )
        assert result.state.labels == {P, CI_RED}

    def test_merge_revert_is_the_done_to_queued_exception(self) -> None:
        result = _applied(_apply(_model(D), Event.MERGE_REVERT))
        assert result.state.lifecycle == {Q}
        assert not result.state.completion_confirmed

    def test_review_reject_withdraws_a_not_needed_completion(self) -> None:
        state = _model(N, completion_confirmed=True)
        result = _applied(_apply(state, Event.REVIEW_REJECT))
        assert result.state.lifecycle == {Q}
        assert not result.state.completion_confirmed


class TestCompletionEvidence:
    def test_completion_without_label_keeps_labels_and_retires_execution(self) -> None:
        result = _applied(
            _apply(
                _model(P, execution_identity=A),
                Event.COMPLETE_WITHOUT_LABEL,
                Kind.CYCLE,
            )
        )
        assert result.plan is None and result.state.labels == {P}
        assert result.state.completion_confirmed
        assert not result.state.execution_active
        assert A in result.state.retired_execution_identities

    @pytest.mark.parametrize(
        "state", [_model(P, completion_confirmed=True), _model(D), _model(N)]
    )
    def test_duplicate_completion_without_label_is_noop(self, state: TaskModel) -> None:
        assert _apply(state, Event.COMPLETE_WITHOUT_LABEL, Kind.CYCLE) == NoOp(
            "already-complete"
        )

    def test_not_needed_outcome_removes_in_progress_without_adding_a_label(
        self,
    ) -> None:
        result = _applied(
            _apply(_model(P), Event.COMPLETE_WITHOUT_LABEL, Kind.NOT_NEEDED_OUTCOME)
        )
        assert result.state.labels == frozenset()
        assert result.remove_labels == (P,)
        assert result.state.completion_confirmed

    @pytest.mark.parametrize("event", [Event.QUEUE, Event.BLOCK])
    def test_confirmed_completion_cannot_be_reactivated(self, event: Event) -> None:
        state = _model(B, completion_confirmed=True)
        assert _apply(state, event) == Rejected("completion-confirmed")


class TestExecutionIdentity:
    def test_launch_records_the_identity(self) -> None:
        result = _applied(_apply(_model(Q), Event.LAUNCH, execution=A))
        assert result.state.execution_identity == A

    def test_same_launch_fact_resend_is_noop(self) -> None:
        state = _model(P, execution_identity=A)
        assert _apply(state, Event.LAUNCH, execution=A) == NoOp("same-launch")

    def test_other_launch_while_active_is_rejected(self) -> None:
        state = _model(P, execution_identity=A)
        assert _apply(state, Event.LAUNCH, execution=A2) == Rejected("launch-mismatch")

    def test_indeterminate_launches_are_held(self) -> None:
        held = _model(P, execution_identity=IndeterminateExecution("ambiguous"))
        assert _apply(held, Event.LAUNCH, execution=A) == Rejected("launch-mismatch")
        invalid = IndeterminateExecution("no-handle")
        assert _apply(_model(Q), Event.LAUNCH, execution=invalid) == Rejected(
            "invalid-launch"
        )
        assert _apply(_model(Q), Event.LAUNCH) == Rejected("invalid-launch")

    @pytest.mark.parametrize(
        "state",
        [
            _model(D),
            _model(N),
            _model(H),
            _model(M),
            _model(Q, completion_confirmed=True),
        ],
    )
    def test_terminal_or_completed_tasks_are_never_relaunched(
        self, state: TaskModel
    ) -> None:
        assert _apply(state, Event.LAUNCH, execution=A) == Rejected("terminal-state")

    def test_merge_revert_makes_a_completed_task_launchable_again(self) -> None:
        reverted = _applied(_apply(_model(D), Event.MERGE_REVERT)).state
        assert isinstance(_apply(reverted, Event.LAUNCH, execution=A), Applied)

    def test_aba_late_event_of_the_retired_execution_is_rejected(self) -> None:
        state = _applied(_apply(_model(Q), Event.LAUNCH, execution=A)).state
        state = _applied(_apply(state, Event.RECLAIM, execution=A)).state
        state = _applied(_apply(state, Event.LAUNCH, execution=A2)).state
        # The labels are back to in-progress, so only the identity tells A apart.
        assert state.lifecycle == {P}
        late = _apply(state, Event.REQUEUE, Kind.RECOVERY, execution=A)
        assert late == Rejected("stale-execution")
        assert _apply(state, Event.LAUNCH, execution=A) == Rejected("launch-mismatch")

    def test_launch_with_retired_identity_while_queued_is_rejected(self) -> None:
        state = _applied(_apply(_model(Q), Event.LAUNCH, execution=A)).state
        queued = _applied(_apply(state, Event.RECLAIM, execution=A)).state
        assert queued.execution_identity is None
        assert A in queued.retired_execution_identities
        assert _apply(queued, Event.LAUNCH, execution=A) == Rejected("stale-execution")

    def test_events_without_identity_are_not_guarded(self) -> None:
        state = _model(
            P, execution_identity=A2, retired_execution_identities=frozenset({A})
        )
        assert isinstance(_apply(state, Event.REQUEUE, Kind.RECOVERY), Applied)

    def test_escalation_keeps_the_running_execution(self) -> None:
        result = _applied(_apply(_model(P, execution_identity=A), Event.ESCALATE))
        assert result.state.execution_identity == A

    def test_manual_merge_escalation_retires_the_execution(self) -> None:
        result = _applied(
            _apply(_model(P, execution_identity=A), Event.ESCALATE, Kind.MANUAL_MERGE)
        )
        assert result.state.execution_identity is None
        assert A in result.state.retired_execution_identities


class TestBudgets:
    def test_reclaim_counts_and_settles_within_budget(self) -> None:
        result = _applied(_apply(_model(P), Event.RECLAIM))
        assert result.state.lifecycle == {Q}
        assert result.state.retries.reclaim == ReclaimState(count=1, pending=False)
        assert not result.escalated

    def test_reclaim_over_budget_escalates_and_still_counts(self) -> None:
        state = _model(P, retries=RetryStates(reclaim=ReclaimState(count=3)))
        result = _applied(_apply(state, Event.RECLAIM))
        assert result.state.lifecycle == {H}
        assert result.state.retries.reclaim == ReclaimState(count=4, pending=False)
        assert result.escalated

    def test_pending_reclaim_reuses_its_count(self) -> None:
        state = _model(
            P, retries=RetryStates(reclaim=ReclaimState(count=2, pending=True))
        )
        result = _applied(_apply(state, Event.RECLAIM))
        assert result.state.retries.reclaim.count == 2

    def test_pending_reclaim_escalates_when_the_limit_was_lowered(self) -> None:
        # Unlike early death / review timeout, the reclaim limit is re-checked
        # against the reserved count (`_refresh_reclaim`).
        state = _model(
            P, retries=RetryStates(reclaim=ReclaimState(count=3, pending=True))
        )
        result = _applied(
            _apply(state, Event.RECLAIM, limits=production_limits(max_task_reclaims=2))
        )
        assert result.escalated and result.state.retries.reclaim.count == 3

    @pytest.mark.parametrize(
        ("kind", "count", "escalated"),
        [
            # N early-death retries allow N requeues (default N=2).
            (Kind.EARLY_DEATH, 0, False),
            (Kind.EARLY_DEATH, 1, False),
            (Kind.EARLY_DEATH, 2, True),
            # N review-timeout attempts allow N-1 requeues (default N=2).
            (Kind.REVIEW_TIMEOUT, 0, False),
            (Kind.REVIEW_TIMEOUT, 1, True),
        ],
    )
    def test_backoff_retry_boundaries(
        self, kind: Kind, count: int, escalated: bool
    ) -> None:
        field = "early_death" if kind is Kind.EARLY_DEATH else "review_timeout"
        retries = (
            RetryStates(early_death=BackoffState(count=count))
            if kind is Kind.EARLY_DEATH
            else RetryStates(review_timeout=BackoffState(count=count))
        )
        result = _applied(
            _apply(_model(P, retries=retries), Event.REQUEUE, kind, now=100.0)
        )
        assert result.escalated is escalated
        retry = getattr(result.state.retries, field)
        if escalated:
            assert result.state.lifecycle == {H}
            assert retry == BackoffState(count=count)
        else:
            assert result.state.lifecycle == {Q}
            # backoff 60s doubles per previous retry; the requeue is settled.
            assert retry == BackoffState(
                count=count + 1, retry_at=100.0 + 60 * 2**count
            )

    def test_pending_backoff_retry_resumes_without_new_consumption(self) -> None:
        reserved = BackoffState(count=2, retry_at=500.0, pending=True)
        state = _model(P, retries=RetryStates(early_death=reserved))
        # Even a lowered limit resumes the reservation and keeps retry_at.
        result = _applied(
            _apply(
                state,
                Event.REQUEUE,
                Kind.EARLY_DEATH,
                now=900.0,
                limits=production_limits(max_early_death_retries=0),
            )
        )
        assert not result.escalated
        assert result.state.retries.early_death == BackoffState(count=2, retry_at=500.0)

    def test_launch_confirms_reclaim_and_early_death_reservations(self) -> None:
        retries = RetryStates(
            reclaim=ReclaimState(count=1, pending=True),
            early_death=BackoffState(count=1, retry_at=5.0, pending=True),
            review_timeout=BackoffState(count=1, retry_at=5.0, pending=True),
        )
        result = _applied(_apply(_model(Q, retries=retries), Event.LAUNCH, execution=A))
        assert result.state.retries == RetryStates(
            reclaim=ReclaimState(count=1),
            early_death=BackoffState(count=1, retry_at=5.0),
            review_timeout=BackoffState(count=1, retry_at=5.0, pending=True),
        )

    def test_recompute_budget_falls_back_to_force_serial(self) -> None:
        first = _applied(_apply(_model(P), Event.RECOMPUTE))
        assert first.state.counts == BudgetCounts(recompute=1)
        assert first.state.labels == {P}
        state = _model(P, counts=BudgetCounts(recompute=2))
        forced = _applied(_apply(state, Event.RECOMPUTE))
        assert forced.state.labels == {P, FS} and forced.state.counts.recompute == 2
        assert _apply(forced.state, Event.RECOMPUTE) == NoOp("already-forced-serial")

    @pytest.mark.parametrize(
        ("attempts", "labels"), [(0, {B, CI_RED}), (1, {B, CI_RED}), (2, {H})]
    )
    def test_base_branch_red_escalates_on_the_third_attempt(
        self, attempts: int, labels: set[str]
    ) -> None:
        held = (P, CI_RED) if attempts else (P,)
        state = _model(*held, counts=BudgetCounts(base_branch_red=attempts))
        result = _applied(_apply(state, Event.BLOCK, Kind.BASE_BRANCH_RED))
        assert result.state.labels == labels
        assert result.state.counts.base_branch_red == attempts + 1


class TestOperationsAndRestart:
    def test_confirmed_operation_resend_is_noop(self) -> None:
        state = _applied(_apply(_model(P), Event.RECLAIM, operation="op-1")).state
        assert _apply(state, Event.RECLAIM, operation="op-1") == NoOp(
            "duplicate-operation"
        )

    def test_interrupted_operation_resumes_without_extra_budget(self) -> None:
        reserved = _applied(
            _apply(
                _model(P), Event.RECLAIM, operation="op-1", stop_after=Stage.RESERVED
            )
        ).state
        assert reserved.labels == {P}
        assert reserved.retries.reclaim == ReclaimState(count=1, pending=True)
        assert reserved.pending_operation is not None
        assert _apply(reserved, Event.RECLAIM, operation="op-2") == Rejected(
            "operation-pending"
        )
        added = _applied(
            _apply(
                reserved, Event.RECLAIM, operation="op-1", stop_after=Stage.LABEL_ADDED
            )
        ).state
        assert added.labels == {P, Q}
        done = _applied(_apply(added, Event.RECLAIM, operation="op-1")).state
        assert done.labels == {Q}
        assert done.retries.reclaim == ReclaimState(count=1, pending=False)
        assert done.pending_operation is None

    def test_interrupted_recompute_operation_resumes_without_extra_budget(
        self,
    ) -> None:
        limits = replace(LIMITS, max_recompute_retries=1)
        reserved = _applied(
            apply_event(
                _model(P),
                EventInput(
                    Event.RECOMPUTE,
                    Kind.PLAIN,
                    operation="op-rc",
                    stop_after=Stage.RESERVED,
                ),
                limits,
            )
        ).state
        assert reserved.counts.recompute == 1
        assert StatusLabel.FORCE_SERIAL not in reserved.auxiliary

        resumed = _applied(
            apply_event(
                reserved,
                EventInput(Event.RECOMPUTE, Kind.PLAIN, operation="op-rc"),
                limits,
            )
        )
        assert resumed.state.counts.recompute == 1
        assert StatusLabel.FORCE_SERIAL not in resumed.state.auxiliary
        assert not resumed.escalated

    def test_interrupted_escalated_operation_resumes_and_cleans_up(self) -> None:
        limits = replace(LIMITS, max_task_reclaims=0)
        added = _applied(
            apply_event(
                _model(P),
                EventInput(
                    Event.RECLAIM,
                    operation="op-esc",
                    stop_after=Stage.LABEL_ADDED,
                ),
                limits,
            )
        ).state
        assert added.labels == {P, H}
        assert added.pending_operation is not None
        assert added.pending_operation.stage is Stage.LABEL_ADDED

        done = _applied(
            apply_event(
                added,
                EventInput(Event.RECLAIM, operation="op-esc"),
                limits,
            )
        ).state
        assert done.labels == {H}
        assert done.pending_operation is None
        assert "op-esc" in done.confirmed_operations

    def test_interrupted_over_budget_recompute_operation_resumes_forced_serial(
        self,
    ) -> None:
        limits = replace(LIMITS, max_recompute_retries=1)
        # First delivery reaches retry limit and stops at Stage.RESERVED
        state = _model(P, counts=BudgetCounts(recompute=1))
        reserved = _applied(
            apply_event(
                state,
                EventInput(
                    Event.RECOMPUTE,
                    Kind.PLAIN,
                    operation="op-fs",
                    stop_after=Stage.RESERVED,
                ),
                limits,
            )
        ).state
        assert reserved.counts.recompute == 1
        assert StatusLabel.FORCE_SERIAL not in reserved.auxiliary
        assert reserved.pending_operation is not None

        # Resume the same operation: it must still take the forced-serial branch
        resumed = _applied(
            apply_event(
                reserved,
                EventInput(Event.RECOMPUTE, Kind.PLAIN, operation="op-fs"),
                limits,
            )
        )
        assert StatusLabel.FORCE_SERIAL in resumed.state.auxiliary
        assert resumed.escalated

    def test_interrupted_backoff_operation_resumes_without_reconsuming_retry(
        self,
    ) -> None:
        limits = replace(LIMITS)
        # First delivery plans backoff, adds label, and stops at Stage.LABEL_ADDED
        added = _applied(
            apply_event(
                _model(P),
                EventInput(
                    Event.REQUEUE,
                    Kind.EARLY_DEATH,
                    operation="op-ed",
                    now=10.0,
                    stop_after=Stage.LABEL_ADDED,
                ),
                limits,
            )
        ).state
        assert added.retries.early_death.count == 1
        assert not added.retries.early_death.pending
        assert added.pending_operation is not None
        assert added.pending_operation.stage is Stage.LABEL_ADDED

        # Resume the same operation: it must reuse the settled decision without consuming a second attempt
        done = _applied(
            apply_event(
                added,
                EventInput(
                    Event.REQUEUE, Kind.EARLY_DEATH, operation="op-ed", now=10.0
                ),
                limits,
            )
        ).state
        assert done.retries.early_death.count == 1
        assert done.labels == {Q}
        assert done.pending_operation is None

    def test_interrupted_over_budget_recompute_operation_resumes_when_already_forced_serial(
        self,
    ) -> None:
        limits = replace(LIMITS, max_recompute_retries=1)
        state = _model(P, counts=BudgetCounts(recompute=1))
        added = _applied(
            apply_event(
                state,
                EventInput(
                    Event.RECOMPUTE,
                    Kind.PLAIN,
                    operation="op-fs-label",
                    stop_after=Stage.LABEL_ADDED,
                ),
                limits,
            )
        ).state
        assert StatusLabel.FORCE_SERIAL in added.auxiliary
        assert added.pending_operation is not None
        assert added.pending_operation.stage is Stage.LABEL_ADDED

        done = _applied(
            apply_event(
                added,
                EventInput(Event.RECOMPUTE, Kind.PLAIN, operation="op-fs-label"),
                limits,
            )
        ).state
        assert StatusLabel.FORCE_SERIAL in done.auxiliary
        assert done.pending_operation is None
        assert "op-fs-label" in done.confirmed_operations

    def test_interrupted_reclaim_operation_preserves_escalation_at_stage_label_added_despite_limit_change(
        self,
    ) -> None:
        state = _model(
            P, retries=RetryStates(reclaim=ReclaimState(count=1, pending=False))
        )
        limits_initial = replace(LIMITS, max_task_reclaims=1)
        added = _applied(
            apply_event(
                state,
                EventInput(
                    Event.RECLAIM,
                    Kind.PLAIN,
                    operation="op-rec-esc",
                    stop_after=Stage.LABEL_ADDED,
                ),
                limits_initial,
            )
        ).state
        assert added.pending_operation is not None
        assert added.pending_operation.escalated
        assert added.pending_operation.stage is Stage.LABEL_ADDED

        limits_relaxed = replace(LIMITS, max_task_reclaims=5)
        done = _applied(
            apply_event(
                added,
                EventInput(Event.RECLAIM, Kind.PLAIN, operation="op-rec-esc"),
                limits_relaxed,
            )
        ).state
        assert done.labels == {H}
        assert done.pending_operation is None
        assert "op-rec-esc" in done.confirmed_operations

    def test_interrupted_normal_reclaim_operation_preserves_decision_when_limit_lowered(
        self,
    ) -> None:
        state = _model(P)
        limits_initial = replace(LIMITS, max_task_reclaims=5)
        added = _applied(
            apply_event(
                state,
                EventInput(
                    Event.RECLAIM,
                    Kind.PLAIN,
                    operation="op-rec-norm",
                    stop_after=Stage.LABEL_ADDED,
                ),
                limits_initial,
            )
        ).state
        assert added.pending_operation is not None
        assert not added.pending_operation.escalated
        assert added.pending_operation.stage is Stage.LABEL_ADDED

        limits_strict = replace(LIMITS, max_task_reclaims=0)
        done = _applied(
            apply_event(
                added,
                EventInput(Event.RECLAIM, Kind.PLAIN, operation="op-rec-norm"),
                limits_strict,
            )
        ).state
        assert done.labels == {Q}
        assert done.pending_operation is None
        assert "op-rec-norm" in done.confirmed_operations

    def test_restart_keeps_persisted_reservations_but_not_the_model_operation(
        self,
    ) -> None:
        reserved = _applied(
            _apply(
                _model(P), Event.RECLAIM, operation="op-1", stop_after=Stage.RESERVED
            )
        ).state
        restarted = restart(reserved, ledger_loss=False)
        assert restarted.pending_operation is None
        assert restarted.retries == reserved.retries
        # A fresh delivery after the restart resumes the persisted reservation.
        resumed = _applied(_apply(restarted, Event.RECLAIM)).state
        assert resumed.retries.reclaim == ReclaimState(count=1, pending=False)

    def test_ledger_loss_resets_only_local_budgets(self) -> None:
        state = _model(
            P,
            execution_identity=A,
            counts=BudgetCounts(recompute=1, base_branch_red=2),
            retries=RetryStates(
                reclaim=ReclaimState(count=2),
                early_death=BackoffState(count=1, retry_at=9.0),
                review_timeout=BackoffState(count=1, pending=True),
            ),
        )
        lost = restart(state, ledger_loss=True)
        assert lost.retries == RetryStates()
        assert lost.counts == state.counts
        assert lost.ledger_epoch == state.ledger_epoch + 1
        assert lost.execution_identity is None
        assert lost.labels == state.labels

    def test_restart_clears_retired_execution_identities(self) -> None:
        state = _model(Q, retired_execution_identities=frozenset({A}))
        restarted = restart(state, ledger_loss=False)
        assert restarted.retired_execution_identities == frozenset()

    def test_restart_clears_cycle_only_completion_but_preserves_persistent_completion(
        self,
    ) -> None:
        cycle_done = _applied(
            _apply(_model(P), Event.COMPLETE_WITHOUT_LABEL, Kind.CYCLE)
        ).state
        assert cycle_done.completion_confirmed
        restarted_cycle = restart(cycle_done, ledger_loss=False)
        assert not restarted_cycle.completion_confirmed
        resumed = apply_event(
            restarted_cycle, EventInput(Event.REQUEUE, Kind.RECOVERY), LIMITS
        )
        assert isinstance(resumed, Applied)

        prior_done = _applied(
            _apply(_model(P), Event.COMPLETE_WITHOUT_LABEL, Kind.PRIOR_MERGE)
        ).state
        assert prior_done.completion_confirmed
        restarted_prior = restart(prior_done, ledger_loss=False)
        assert restarted_prior.completion_confirmed

    def test_apply_event_is_pure(self) -> None:
        state = _model(P, execution_identity=A)
        snapshot = replace(state)
        first = apply_event(state, EventInput(Event.RECLAIM), LIMITS)
        second = apply_event(state, EventInput(Event.RECLAIM), LIMITS)
        assert state == snapshot and first == second


class TestEventRegistry:
    def test_event_by_source_covers_call_sites_and_out_of_scope_and_invariant(
        self,
    ) -> None:
        call_sites = set(CALL_SITES.keys())
        out_of_scope = {f"{f}::{fn}" for f, fn, _ in OUT_OF_SCOPE_PATHS}
        invariant = {f"{f}::{fn}" for f, fn, _ in LABEL_INVARIANT_COMPLETIONS}
        expected = call_sites | out_of_scope | invariant
        assert set(EVENT_BY_SOURCE.keys()) == expected

    def test_every_route_condition_is_executed_by_cases(self) -> None:
        all_route_conditions = {
            (source, c)
            for source, routes in EVENT_BY_SOURCE.items()
            for r in routes
            for c in r.cases
        }
        covered_conditions = {(c.site, c.condition) for c in CASES} | {
            (c.source, c.condition) for c in EVENT_CASES
        }
        assert all_route_conditions == covered_conditions


_DOCS_ROOT = Path(__file__).resolve().parent.parent / "docs"
_EVENT_TABLE_ROW = re.compile(
    r"^\|\s*`([A-Z_]+)`\s*\(`([A-Z_]+)`\)\s*\|([^|]*)\|([^|]*)\|\s*`([^`]+)`\s*\|",
    re.M,
)


@pytest.mark.parametrize("lang", ["ja", "en"])
class TestStatusLabelsDocumentMatchesEventTable:
    def test_document_lists_every_event_route(self, lang: str) -> None:
        doc = (_DOCS_ROOT / lang / "status-labels.md").read_text(encoding="utf-8")
        rows = _EVENT_TABLE_ROW.findall(doc)
        assert len(rows) == sum(len(routes) for routes in EVENT_BY_SOURCE.values())

        expected_rows = set()
        for source, routes in EVENT_BY_SOURCE.items():
            for r in routes:
                expected_rows.add((r.event.name, r.kind.name, source))

        documented_rows = {(ev, kind, src.strip()) for ev, kind, _, _, src in rows}
        assert documented_rows == expected_rows

    def test_document_event_targets_and_sources_match_specs(self, lang: str) -> None:
        doc = (_DOCS_ROOT / lang / "status-labels.md").read_text(encoding="utf-8")
        for ev, kind, src_cell, tgt_cell, src in _EVENT_TABLE_ROW.findall(doc):
            event = Event[ev]
            k = Kind[kind]
            spec = EVENT_SPECS[(event, k)]
            expected_sources = {s.value for s in spec.sources}
            doc_sources = {m.group(0) for m in re.finditer(r"status:[a-z-]+", src_cell)}
            assert doc_sources == expected_sources, (ev, kind, src)

            if spec.target is not None:
                assert spec.target.value in tgt_cell, (ev, kind, src)
            else:
                assert "-" in tgt_cell or "なし" in tgt_cell or "none" in tgt_cell, (
                    ev,
                    kind,
                    src,
                )


class TestEventModelConformance:
    @pytest.mark.parametrize("case", EVENT_CASES, ids=lambda c: c.id)
    def test_production_driver_matches_apply_event(
        self, case: EventCase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        route = route_for(case.source, case.condition)
        forge = FaultyForge()
        forge.issues[ISSUE] = make_issue(ISSUE, labels=case.held)
        config = make_test_dispatcher_config(tmp_path, forge=forge, apply=True)
        env = CaseEnv(forge, config, monkeypatch, tmp_path, case.held, case.params)
        observations = case.driver(env)
        assert len(observations) == len(case.steps)

        state = case.initial()
        for step, obs in zip(case.steps, observations, strict=False):
            if step == FAIL_BEFORE:
                applied_state = state
                res_tag = None
            else:
                stop_after = step if isinstance(step, Stage) else None
                inp = EventInput(
                    route.event, route.kind, stop_after=stop_after, **case.inputs
                )
                res = apply_event(state, inp, LIMITS)
                if isinstance(res, Applied):
                    applied_state = res.state
                    res_tag = "applied"
                elif isinstance(res, NoOp):
                    applied_state = state
                    res_tag = "noop"
                else:
                    applied_state = state
                    res_tag = "rejected"
                state = applied_state

            assert obs.labels == applied_state.labels
            if obs.result is not None:
                assert obs.result == res_tag
            if obs.completion is not None:
                assert obs.completion == applied_state.completion_done
            if obs.execution_active is not None:
                assert obs.execution_active == applied_state.execution_active
            if obs.retries is not None:
                assert obs.retries == applied_state.retries
            if obs.counts is not None:
                assert obs.counts == applied_state.counts


class TestCallSitesApplyEventConformance:
    """Every registered call-site case matches apply_event semantics."""

    @pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
    def test_call_site_case_matches_apply_event(
        self, case: Case, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls, forge = _run_callsite_case(case, monkeypatch, tmp_path)
        route = route_for(case.site, case.condition)
        execution = (
            ExecutionIdentity("launch-test") if route.event is Event.LAUNCH else None
        )
        inp = EventInput(route.event, route.kind, execution=execution)
        initial = TaskModel.from_labels(case.held)
        res = apply_event(initial, inp, LIMITS)
        assert isinstance(res, Applied)
        assert set(res.state.labels) == set(forge.get_issue_labels(ISSUE))
