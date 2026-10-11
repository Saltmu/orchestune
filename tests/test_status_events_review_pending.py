"""A labelled not-needed completion is not complete while its review is pending (#1278).

Expected values come from the issue's effect table and timeline, not from
`apply_event`.  Production drives the sequence in `check_labeled_review_sequence`.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from orchestune.labels import StatusLabel
from orchestune.ledger.status_events import (
    EVENT_SPECS,
    REVIEW_PENDING_EFFECTS,
    Applied,
    Event,
    EventInput,
    ExecutionIdentity,
    Kind,
    NoOp,
    PendingEffect,
    Rejected,
    Stage,
    TaskModel,
    apply_event,
    restart,
)
from tests.status_event_review_support import (
    UNCLAIMED_CASE,
    check_labeled_review_sequence,
)
from tests.status_event_test_support import production_limits
from tests.verification_contract_test_support import expect_violation

Q, P, N = StatusLabel.QUEUED, StatusLabel.IN_PROGRESS, StatusLabel.NOT_NEEDED
H = StatusLabel.BLOCKED_HUMAN_REVIEW
LIMITS = production_limits()
RUN = ExecutionIdentity("launch-a")
_E, _K = Event, Kind
REVIEW_COMPLETION = (_E.NOT_NEEDED, _K.COMPLETION_REVIEW)
PASS = (_E.COMPLETE_WITHOUT_LABEL, _K.REVIEW_PASSED)
REJECT = (_E.REVIEW_REJECT, _K.PLAIN)
ESCALATE = (_E.ESCALATE, _K.PLAIN)
CYCLE = (_E.COMPLETE_WITHOUT_LABEL, _K.CYCLE)
PRIOR_MERGE = (_E.COMPLETE_WITHOUT_LABEL, _K.PRIOR_MERGE)


def _model(*labels: str, **overrides: object) -> TaskModel:
    return replace(TaskModel.from_labels(labels), **overrides)  # type: ignore[arg-type]


def _send(state: TaskModel, key: tuple[Event, Kind]) -> Applied | NoOp | Rejected:
    return apply_event(state, EventInput(*key), LIMITS)


def _applied(state: TaskModel, key: tuple[Event, Kind]) -> TaskModel:
    result = _send(state, key)
    assert isinstance(result, Applied), (key, result)
    return result.state


def _pending(**overrides: object) -> TaskModel:
    """The state right after a labelled not-needed completion of a running task."""
    return _applied(_model(P, execution_identity=RUN, **overrides), REVIEW_COMPLETION)


class TestReviewPendingInterval:
    def test_review_completion_is_not_done_until_the_review_passes(self) -> None:
        state = _pending()
        assert state.lifecycle == {N}
        assert state.review_pending and not state.completion_done
        assert not state.execution_active
        passed = _applied(state, PASS)
        assert passed.completion_done and not passed.review_pending
        assert passed.completion_confirmed and passed.persistent_completion

    def test_an_interruption_after_the_label_keeps_the_wait(self) -> None:
        # Production reserves the review policy before it publishes the label.
        running = _model(P, execution_identity=RUN)
        stopped = apply_event(
            running,
            EventInput(
                *REVIEW_COMPLETION, operation="op", stop_after=Stage.LABEL_ADDED
            ),
            LIMITS,
        )
        assert isinstance(stopped, Applied)
        assert N in stopped.state.lifecycle
        assert stopped.state.review_pending and not stopped.state.completion_done
        restarted = restart(stopped.state, ledger_loss=False)
        assert restarted.review_pending and not restarted.completion_done
        resumed = apply_event(
            stopped.state, EventInput(*REVIEW_COMPLETION, operation="op"), LIMITS
        )
        assert isinstance(resumed, Applied)
        assert resumed.state.review_pending and not resumed.state.completion_done

    def test_local_completion_stays_immediately_done(self) -> None:
        state = _applied(
            _model(P, execution_identity=RUN), (_E.NOT_NEEDED, _K.COMPLETION)
        )
        assert state.completion_done and not state.review_pending

    def test_a_duplicate_pass_is_a_noop(self) -> None:
        passed = _applied(_pending(), PASS)
        assert _send(passed, PASS) == NoOp("already-complete")

    @pytest.mark.parametrize(
        ("key", "lifecycle"),
        [(REJECT, {Q}), (ESCALATE, {H})],
        ids=["reject", "timeout"],
    )
    def test_a_rejection_or_timeout_withdraws_the_wait(
        self, key: tuple[Event, Kind], lifecycle: set[StatusLabel]
    ) -> None:
        state = _applied(_pending(), key)
        assert state.lifecycle == lifecycle
        assert not state.review_pending and not state.completion_done
        assert not state.completion_confirmed and not state.persistent_completion

    def test_a_final_label_stays_terminal_for_launch_while_waiting(self) -> None:
        launch = EventInput(_E.LAUNCH, _K.PLAIN, execution=ExecutionIdentity("b"))
        assert apply_event(_pending(), launch, LIMITS) == Rejected("terminal-state")

    def test_the_wait_clears_earlier_completion_evidence(self) -> None:
        state = _pending(completion_confirmed=True, persistent_completion=True)
        assert not state.completion_done
        assert not state.completion_confirmed and not state.persistent_completion
        # Resolving the wait must not revive the earlier evidence.
        assert not _applied(state, REJECT).completion_done

    @pytest.mark.parametrize("evidence", [CYCLE, PRIOR_MERGE], ids=["cycle", "prior"])
    @pytest.mark.parametrize(
        "resolution", [REJECT, ESCALATE], ids=["reject", "timeout"]
    )
    def test_other_completion_evidence_does_not_end_the_wait(
        self,
        evidence: tuple[Event, Kind],
        resolution: tuple[Event, Kind],
    ) -> None:
        state = _applied(_pending(), evidence)
        assert state.review_pending and not state.completion_done
        assert not state.completion_confirmed and not state.persistent_completion
        resolved = _applied(state, resolution)
        assert not resolved.completion_done
        assert resolved.lifecycle & {Q, H}

    def test_replan_leaves_the_wait_untouched(self) -> None:
        replanned = _applied(_pending(), (_E.NOT_NEEDED, _K.REPLAN))
        assert replanned.review_pending and not replanned.completion_done


class TestReviewPendingRestart:
    def test_a_process_restart_keeps_the_wait(self) -> None:
        restarted = restart(_pending(), ledger_loss=False)
        assert restarted.review_pending and not restarted.completion_done

    def test_ledger_loss_while_waiting_falls_back_to_the_label(self) -> None:
        # The unprotected path: the held review is gone and the label completes.
        lost = restart(_pending(), ledger_loss=True)
        assert not lost.review_pending and lost.completion_done

    @pytest.mark.parametrize("key", [REJECT, ESCALATE], ids=["reject", "timeout"])
    def test_ledger_loss_after_a_withdrawal_stays_incomplete(
        self, key: tuple[Event, Kind]
    ) -> None:
        lost = restart(_applied(_pending(), key), ledger_loss=True)
        assert not lost.completion_done

    @pytest.mark.parametrize("key", [REJECT, ESCALATE], ids=["reject", "timeout"])
    def test_ledger_loss_after_other_evidence_keeps_the_withdrawal(
        self, key: tuple[Event, Kind]
    ) -> None:
        state = _applied(_applied(_pending(), CYCLE), key)
        assert not restart(state, ledger_loss=True).completion_done

    def test_a_pass_survives_ledger_loss(self) -> None:
        lost = restart(_applied(_pending(), PASS), ledger_loss=True)
        assert lost.completion_done and not lost.review_pending


#: What each spec that can hold `status:not-needed` does to the wait (#1278 table).
_EXPECTED_EFFECTS = {
    REVIEW_COMPLETION: PendingEffect.START,
    PASS: PendingEffect.RESOLVE,
    REJECT: PendingEffect.WITHDRAW,
    ESCALATE: PendingEffect.WITHDRAW,
    CYCLE: PendingEffect.KEEP_UNCONFIRMED,
    PRIOR_MERGE: PendingEffect.KEEP_UNCONFIRMED,
    (_E.NOT_NEEDED, _K.REPLAN): PendingEffect.KEEP,
    (_E.NOT_NEEDED, _K.PLAIN): PendingEffect.KEEP,
    (_E.NOT_NEEDED, _K.COMPLETION): PendingEffect.KEEP,
    (_E.RELEASE_HOLD, _K.RECOMPUTE): PendingEffect.KEEP,
    (_E.RELEASE_HOLD, _K.BASE_BRANCH_RED): PendingEffect.KEEP,
    (_E.RELEASE_HOLD, _K.EXTERNAL_LOCK): PendingEffect.KEEP,
}


def _can_hold_not_needed(key: tuple[Event, Kind]) -> bool:
    spec = EVENT_SPECS[key]
    return N in spec.sources or spec.target is N


class TestEffectTableCoverage:
    def test_every_spec_that_can_hold_not_needed_is_classified(self) -> None:
        holding = {key for key in EVENT_SPECS if _can_hold_not_needed(key)}
        assert holding == REVIEW_PENDING_EFFECTS.keys()

    def test_the_effect_table_matches_the_issue(self) -> None:
        assert dict(REVIEW_PENDING_EFFECTS) == _EXPECTED_EFFECTS


class TestLabeledNotNeededReviewSequence:
    """Production `complete` + policy runner vs the model, step by step."""

    @pytest.mark.parametrize("verdict", ["passed", "failed", "timeout"])
    def test_each_verdict_agrees_with_production(
        self, verdict: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        check_labeled_review_sequence(verdict, tmp_path, monkeypatch)

    @pytest.mark.parametrize("verdict", ["passed", "failed", "timeout"])
    def test_ledger_loss_after_the_verdict_agrees_with_production(
        self, verdict: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        check_labeled_review_sequence(verdict, tmp_path, monkeypatch, "resolved")

    def test_ledger_loss_while_pending_agrees_with_production(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        check_labeled_review_sequence("passed", tmp_path, monkeypatch, "pending")

    def test_control_a_suppressed_review_policy_is_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        for name in ("normal", "fault"):
            (tmp_path / name).mkdir()
        check_labeled_review_sequence("passed", tmp_path / "normal", monkeypatch)
        monkeypatch.setattr(
            "orchestune.complete.unclaimed.not_needed_policies", lambda *a, **k: ()
        )
        with expect_violation("P3A-DYNAMIC-CONFORMANCE"):
            check_labeled_review_sequence("passed", tmp_path / "fault", monkeypatch)

    def test_the_expected_kind_is_fixed_by_the_case_not_by_production(self) -> None:
        from tests.status_event_test_support import route_for

        source = "complete/status_labels.py::_completion_mutate"
        assert route_for(source, UNCLAIMED_CASE).kind is Kind.COMPLETION_REVIEW
        assert route_for(source, "not-needed-local-active").kind is Kind.COMPLETION
