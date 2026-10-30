"""Ack deadline and opt-in single retry for an unanswered Layer-2 review (#1436).

Pure: the caller supplies the trusted request times of the current
(attempt, round, head, reviewer) and whether the reviewer showed any activity.
The retry budget is the number of requests already on the PR, so it survives
restarts and concurrent runners without any extra state.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

#: Requests allowed per round and head: the first one plus one retry.
MAX_REQUESTS = 2


class AckAction(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    WAIT = "wait"
    RETRY = "retry"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class AckAssessment:
    action: AckAction
    #: Deadline of the latest request; ``None`` without a request.
    deadline: datetime | None = None
    #: Earliest time the retry may be posted (set only while one is possible).
    retry_at: datetime | None = None
    elapsed_seconds: int = 0
    retries_used: int = 0


def assess_review_ack(
    *,
    trigger_times: Sequence[datetime],
    now: datetime,
    acknowledged: bool,
    ack_seconds: int,
    auto_retry: bool,
    backoff_seconds: int,
) -> AckAssessment:
    """What to do with a review request that has had no reviewer activity."""
    if ack_seconds < 1:
        raise ValueError("ack_seconds must be positive")
    if not trigger_times:
        return AckAssessment(AckAction.NOT_APPLICABLE)
    latest = max(trigger_times)
    used = len(trigger_times) - 1
    deadline = latest + timedelta(seconds=ack_seconds)
    elapsed = max(0, int((now - latest).total_seconds()))
    if acknowledged:
        return AckAssessment(AckAction.NOT_APPLICABLE, deadline, None, elapsed, used)
    if now < deadline:
        return AckAssessment(AckAction.WAIT, deadline, None, elapsed, used)
    if not auto_retry or len(trigger_times) >= MAX_REQUESTS:
        return AckAssessment(AckAction.ESCALATE, deadline, None, elapsed, used)
    retry_at = deadline + timedelta(seconds=backoff_seconds)
    action = AckAction.RETRY if now >= retry_at else AckAction.WAIT
    return AckAssessment(action, deadline, retry_at, elapsed, used)
