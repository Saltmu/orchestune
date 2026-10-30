"""Ack deadline and single-retry decision of an unanswered review request (#1436)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from orchestune.dispatch.semantic_review_ack import AckAction, assess_review_ack
from orchestune.integrator.semantic_claim import ClaimResult
from orchestune.review.semantic_gate import (
    SemanticReviewPhase,
    SemanticReviewRetryV1,
    format_semantic_review_retry_marker,
)
from tests.dispatch.test_postcycle_semantic_review import (
    ATTEMPT,
    HEAD,
    PARENT,
    PR,
    REVIEWER,
    World,
    _phases,
)

P = SemanticReviewPhase

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


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


class TestAckDeadlineAndRetry:
    """#1436: an unresponsive reviewer stops at the ack deadline, not the review timeout."""

    def test_unanswered_request_stops_at_the_ack_deadline(self, world: World) -> None:
        world.seed()
        world.cycle()
        assert world.cycle(minutes=8)["status"] == "held"
        report = world.cycle(minutes=3)  # ~12 min after the request, review timeout 30
        assert report["next_phase"] == "escalated"
        assert world.state.escalation_reason == "review_not_acknowledged"
        assert world.escalations == ["review_not_acknowledged"]
        assert "status:blocked-human-review" in world.labels[PARENT]

    def test_notice_names_the_reviewer_its_origin_and_the_way_forward(
        self, world: World
    ) -> None:
        world.seed()
        world.cycle()
        world.cycle(minutes=11)
        notice = "\n".join(c["body"] for c in world.comments[PARENT])
        assert "codex" in notice and "auto" in notice and "claude-cli" in notice
        assert f"semantic-review:{ATTEMPT}" in notice or ATTEMPT in notice
        assert "semantic_review_provider" in notice
        assert f"orchestune review retry --pr {PR}" in notice
        assert f"orchestune review override --pr {PR}" in notice

    def test_the_notice_is_not_repeated(self, world: World) -> None:
        world.seed()
        world.cycle()
        world.cycle(minutes=11)
        before = len(world.comments[PARENT])
        assert world.cycle()["status"] == "held"
        assert world.cycle(minutes=30)["status"] == "held"
        assert len(world.comments[PARENT]) == before

    def test_a_reviewer_that_answers_in_time_is_not_stopped(self, world: World) -> None:
        world.seed()
        world.cycle()
        world.cycle(minutes=5)
        world.review()
        assert world.cycle(minutes=1)["next_phase"] == "fix_in_progress"

    def test_a_shorter_review_timeout_wins_over_the_ack_window(
        self, tmp_path: Path
    ) -> None:
        world = World(tmp_path, semantic_review_timeout_seconds=300)
        world.seed()
        world.cycle()
        assert world.cycle(minutes=6)["next_phase"] == "escalated"

    def test_without_the_option_no_second_request_is_posted(self, world: World) -> None:
        world.seed()
        world.cycle()
        world.cycle(minutes=11)
        assert len(world.triggers()) == 1

    def test_opt_in_retry_posts_one_more_request_after_the_backoff(
        self, tmp_path: Path
    ) -> None:
        world = World(tmp_path, semantic_review_auto_retry=True)
        world.seed()
        world.cycle()
        assert world.cycle(minutes=10)["status"] == "held"  # past ack, in backoff
        assert len(world.triggers()) == 1
        report = world.cycle(minutes=5)
        assert (report["status"], report["next_phase"]) == (
            "advanced",
            "review_pending",
        )
        assert report["details"]["reason"] == "review_request_retried"
        assert len(world.triggers()) == 2
        assert world.triggers()[0] == world.triggers()[1]
        assert world.state.phase is P.REVIEW_PENDING
        assert world.escalations == []

    def test_retry_is_posted_once_and_the_second_window_escalates(
        self, tmp_path: Path
    ) -> None:
        world = World(tmp_path, semantic_review_auto_retry=True)
        world.seed()
        world.cycle()
        world.cycle(minutes=16)
        assert len(world.triggers()) == 2
        assert world.cycle(minutes=5)["status"] == "held"
        assert len(world.triggers()) == 2
        report = world.cycle(minutes=6)
        assert report["next_phase"] == "escalated"
        assert len(world.triggers()) == 2
        assert world.state.escalation_reason == "review_not_acknowledged"

    def test_a_concurrent_runner_that_lost_the_race_does_not_double_post(
        self, tmp_path: Path
    ) -> None:
        world = World(tmp_path, semantic_review_auto_retry=True)
        world.seed()
        world.cycle()
        world.cycle(minutes=16)
        assert world.cycle(worker="w2", minutes=0.1)["status"] == "held"
        assert len(world.triggers()) == 2

    def test_a_finished_tracker_without_content_is_reviewer_activity(
        self, tmp_path: Path
    ) -> None:
        world = World(tmp_path, semantic_review_auto_retry=True)
        world.seed()
        world.cycle()
        tracker = (
            "<!-- codex-pull-request-review-summary -->\n"
            "| Check | Status |\n| --- | --- |\n| Code Review | Completed |"
        )
        world.post(PR, tracker, REVIEWER)
        assert world.cycle(minutes=16)["status"] == "held"
        assert len(world.triggers()) == 1 and world.escalations == []

    def test_an_answer_that_arrives_before_the_retry_claim_cancels_the_retry(
        self, tmp_path: Path
    ) -> None:
        world = World(tmp_path, semantic_review_auto_retry=True)
        world.seed()
        world.cycle()
        acquire = world.claims().acquire

        def late_answer(*args: Any, **kwargs: Any) -> ClaimResult:
            world.review()  # the reviewer answers after the cycle observed nothing
            return acquire(*args, **kwargs)

        world.claim_overrides["acquire"] = late_answer
        report = world.cycle(minutes=16)
        assert report["details"]["reason"] == "retry_not_due"
        assert len(world.triggers()) == 1
        world.claim_overrides.clear()
        assert world.cycle(minutes=1)["next_phase"] == "fix_in_progress"

    def test_answer_to_the_retried_request_continues_the_loop(
        self, tmp_path: Path
    ) -> None:
        world = World(tmp_path, semantic_review_auto_retry=True)
        world.seed()
        world.cycle()
        world.cycle(minutes=16)
        world.review()
        assert world.cycle(minutes=1)["next_phase"] == "fix_in_progress"

    def test_a_human_retry_requests_the_review_again_and_gets_a_fresh_window(
        self, world: World
    ) -> None:
        world.seed()
        world.cycle()
        world.cycle(minutes=11)
        request = SemanticReviewRetryV1(ATTEMPT, HEAD, "req-ack", "retry", "app fixed")
        world.post(PR, format_semantic_review_retry_marker(request), "alice")
        report = world.cycle()
        assert _phases(report) == ("escalated", "review_pending")
        assert len(world.triggers()) == 2
        assert world.cycle(minutes=5)["status"] == "held"
        assert len(world.triggers()) == 2
        assert world.cycle(minutes=6)["next_phase"] == "escalated"
