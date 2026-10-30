"""Ack deadline and single-retry decision of an unanswered review request (#1436)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from orchestune.dispatch.semantic_review_ack import AckAction, assess_review_ack

T0 = datetime(2026, 10, 26, 13, 58, tzinfo=UTC)


def _assess(
    *,
    triggers: tuple[datetime, ...] = (T0,),
    seconds: int = 0,
    acknowledged: bool = False,
    auto_retry: bool = False,
    ack: int = 600,
    backoff: int = 300,
):
    return assess_review_ack(
        trigger_times=triggers,
        now=T0 + timedelta(seconds=seconds),
        acknowledged=acknowledged,
        ack_seconds=ack,
        auto_retry=auto_retry,
        backoff_seconds=backoff,
    )


class TestAckDeadline:
    def test_no_trigger_means_nothing_to_judge(self) -> None:
        result = _assess(triggers=())
        assert result.action is AckAction.NOT_APPLICABLE
        assert result.deadline is None

    def test_acknowledged_review_is_never_stopped(self) -> None:
        assert (
            _assess(seconds=99999, acknowledged=True).action is AckAction.NOT_APPLICABLE
        )

    def test_waits_until_the_deadline(self) -> None:
        result = _assess(seconds=599)
        assert result.action is AckAction.WAIT
        assert result.deadline == T0 + timedelta(seconds=600)
        assert result.elapsed_seconds == 599

    def test_escalates_exactly_at_the_deadline(self) -> None:
        assert _assess(seconds=600).action is AckAction.ESCALATE

    def test_escalation_does_not_depend_on_the_retry_option_when_off(self) -> None:
        assert _assess(seconds=601, auto_retry=False).action is AckAction.ESCALATE


class TestSingleRetry:
    def test_retry_waits_for_the_backoff_after_the_deadline(self) -> None:
        early = _assess(seconds=899, auto_retry=True)
        assert early.action is AckAction.WAIT
        assert early.retry_at == T0 + timedelta(seconds=900)

    def test_retry_is_due_after_deadline_plus_backoff(self) -> None:
        assert _assess(seconds=900, auto_retry=True).action is AckAction.RETRY

    def test_zero_backoff_retries_at_the_deadline(self) -> None:
        assert _assess(seconds=600, auto_retry=True, backoff=0).action is (
            AckAction.RETRY
        )

    def test_the_retry_request_restarts_the_ack_window(self) -> None:
        second = T0 + timedelta(seconds=900)
        waiting = _assess(triggers=(T0, second), seconds=900 + 599, auto_retry=True)
        assert waiting.action is AckAction.WAIT
        assert waiting.deadline == second + timedelta(seconds=600)

    def test_budget_is_one_extra_request_then_escalate(self) -> None:
        second = T0 + timedelta(seconds=900)
        result = _assess(triggers=(T0, second), seconds=900 + 600, auto_retry=True)
        assert result.action is AckAction.ESCALATE
        assert result.retries_used == 1

    def test_three_requests_on_record_never_retry_again(self) -> None:
        triggers = (T0, T0 + timedelta(seconds=900), T0 + timedelta(seconds=1800))
        result = _assess(triggers=triggers, seconds=9999, auto_retry=True)
        assert result.action is AckAction.ESCALATE

    def test_late_acknowledgement_after_a_retry_is_not_stopped(self) -> None:
        second = T0 + timedelta(seconds=900)
        result = _assess(
            triggers=(T0, second), seconds=5000, auto_retry=True, acknowledged=True
        )
        assert result.action is AckAction.NOT_APPLICABLE


@pytest.mark.parametrize("ack", [0, -1])
def test_non_positive_ack_window_is_rejected(ack: int) -> None:
    with pytest.raises(ValueError, match="ack_seconds"):
        _assess(ack=ack)
