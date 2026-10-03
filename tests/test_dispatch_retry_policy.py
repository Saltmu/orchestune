"""The retry decision (limit, pending reuse, backoff) has one pure owner (#1189)."""

from __future__ import annotations

import pytest

from orchestune.dispatch.retry_policy import (
    RetryDisposition,
    RetryPlan,
    RetryPolicy,
    RetryState,
    early_death_policy,
    plan_retry,
    retry_disposition,
    review_timeout_policy,
)

NEW = RetryDisposition.NEW
RESUME = RetryDisposition.RESUME
EXHAUSTED = RetryDisposition.EXHAUSTED


class TestPolicyConversion:
    """The configured number means different things per retry kind."""

    @pytest.mark.parametrize(
        ("configured", "requeues"), [(0, -1), (1, 0), (2, 1), (3, 2)]
    )
    def test_review_timeout_allows_one_fewer_requeue_than_configured(
        self, configured, requeues
    ):
        assert review_timeout_policy(configured, 60).max_requeues == requeues

    @pytest.mark.parametrize("configured", [0, 1, 2, 3])
    def test_early_death_allows_exactly_the_configured_requeues(self, configured):
        assert early_death_policy(configured, 60).max_requeues == configured

    def test_policies_carry_the_backoff_seconds(self):
        assert review_timeout_policy(2, 15).backoff_seconds == 15
        assert early_death_policy(2, 30).backoff_seconds == 30


class TestDisposition:
    @pytest.mark.parametrize(
        ("configured", "count", "expected"),
        [
            (0, 0, EXHAUSTED),
            (1, 0, EXHAUSTED),
            (2, 0, NEW),
            (2, 1, EXHAUSTED),
            (3, 1, NEW),
            (3, 2, EXHAUSTED),
        ],
    )
    def test_review_timeout_boundary(self, configured, count, expected):
        policy = review_timeout_policy(configured, 60)
        assert retry_disposition(policy, count, False) is expected

    @pytest.mark.parametrize(
        ("configured", "count", "expected"),
        [
            (0, 0, EXHAUSTED),
            (1, 0, NEW),
            (1, 1, EXHAUSTED),
            (2, 1, NEW),
            (2, 2, EXHAUSTED),
        ],
    )
    def test_early_death_boundary(self, configured, count, expected):
        policy = early_death_policy(configured, 60)
        assert retry_disposition(policy, count, False) is expected

    @pytest.mark.parametrize("count", [0, 1, 2, 9])
    def test_pending_reservation_is_resumed_even_past_the_limit(self, count):
        policy = review_timeout_policy(2, 60)
        assert retry_disposition(policy, count, True) is RESUME


class TestPlan:
    policy = RetryPolicy(max_requeues=3, backoff_seconds=60)

    def test_new_retry_increments_and_reserves_with_exponential_backoff(self):
        first = plan_retry(self.policy, RetryState(), now=1000.0)
        assert first == RetryPlan(NEW, RetryState(1, 1060.0, True))
        second = plan_retry(self.policy, RetryState(1, 0.0, False), now=1000.0)
        assert second == RetryPlan(NEW, RetryState(2, 1120.0, True))
        third = plan_retry(self.policy, RetryState(2, 0.0, False), now=1000.0)
        assert third == RetryPlan(NEW, RetryState(3, 1240.0, True))

    def test_resume_keeps_count_time_and_pending_without_extending_the_time(self):
        reserved = RetryState(count=2, retry_at=500.0, pending=True)
        plan = plan_retry(self.policy, reserved, now=10_000.0)
        assert plan == RetryPlan(RESUME, reserved)

    def test_exhausted_returns_the_state_unchanged(self):
        state = RetryState(count=3, retry_at=42.0, pending=False)
        assert plan_retry(self.policy, state, now=1000.0) == RetryPlan(EXHAUSTED, state)

    def test_planning_does_not_mutate_the_input_state(self):
        state = RetryState(count=1, retry_at=7.0, pending=False)
        plan_retry(self.policy, state, now=1000.0)
        assert state == RetryState(1, 7.0, False)

    def test_plan_agrees_with_disposition_for_every_boundary_state(self):
        for count in range(0, 6):
            for pending in (False, True):
                state = RetryState(count=count, retry_at=1.0, pending=pending)
                assert plan_retry(
                    self.policy, state, now=0.0
                ).disposition is retry_disposition(self.policy, count, pending)

    def test_a_retry_is_never_confirmed_as_not_pending_by_the_policy(self):
        for count in range(0, 5):
            plan = plan_retry(self.policy, RetryState(count=count), now=1.0)
            if plan.disposition is not EXHAUSTED:
                assert plan.state.pending is True
