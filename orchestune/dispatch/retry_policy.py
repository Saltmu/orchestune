"""Pure retry decisions shared by every path that requeues a task (#1189).

This module owns the *meaning* of a retry limit, pending-reservation reuse and the
exponential backoff schedule. Callers own persistence and external effects (ledger
saves, labels, comments, worktree removal) and decide where ``pending`` is cleared.

It knows nothing about ``DispatcherConfig``, the ledger DTOs, the forge or the clock:
the caller passes plain numbers and the current time.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# Defaults for the finite retry settings. ``DispatcherConfig`` references these so a
# default is changed in exactly one place.
DEFAULT_REVIEW_TIMEOUT_MAX_ATTEMPTS = 2
DEFAULT_REVIEW_TIMEOUT_BACKOFF_SECONDS = 60
DEFAULT_EARLY_DEATH_MAX_RETRIES = 2
DEFAULT_EARLY_DEATH_BACKOFF_SECONDS = 60


@dataclass(frozen=True)
class RetryPolicy:
    """How many requeues are allowed and the base backoff for the first one."""

    max_requeues: int
    backoff_seconds: float


@dataclass(frozen=True)
class RetryState:
    """The persisted retry bookkeeping of one retry kind for one task."""

    count: int = 0
    retry_at: float = 0.0
    pending: bool = False


class RetryDisposition(Enum):
    NEW = "new"
    RESUME = "resume"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class RetryPlan:
    disposition: RetryDisposition
    state: RetryState


def review_timeout_policy(
    max_attempts: int,
    backoff_seconds: float = DEFAULT_REVIEW_TIMEOUT_BACKOFF_SECONDS,
) -> RetryPolicy:
    """The configured number N counts attempts: the last timeout is terminal.

    N therefore allows N-1 requeues. The backoff is irrelevant to a pure
    classification, so it may be omitted when only the disposition is needed.
    """
    return RetryPolicy(max_requeues=max_attempts - 1, backoff_seconds=backoff_seconds)


def early_death_policy(
    max_retries: int,
    backoff_seconds: float = DEFAULT_EARLY_DEATH_BACKOFF_SECONDS,
) -> RetryPolicy:
    """The configured number N counts retries: N allows exactly N requeues."""
    return RetryPolicy(max_requeues=max_retries, backoff_seconds=backoff_seconds)


def retry_disposition(
    policy: RetryPolicy, count: int, pending: bool
) -> RetryDisposition:
    """Single owner of the limit comparison.

    A pending reservation is always resumed, even when the limit has since been
    lowered, so a retry that was already reserved is never counted twice.
    """
    if pending:
        return RetryDisposition.RESUME
    if count >= policy.max_requeues:
        return RetryDisposition.EXHAUSTED
    return RetryDisposition.NEW


def plan_retry(policy: RetryPolicy, state: RetryState, *, now: float) -> RetryPlan:
    """Decide the next retry state without mutating ``state``.

    ``pending`` is only ever set here, never cleared: confirming the requeue (clearing
    ``pending``) happens in the caller once its own external effects are complete.
    """
    disposition = retry_disposition(policy, state.count, state.pending)
    if disposition is RetryDisposition.NEW:
        return RetryPlan(
            disposition,
            RetryState(
                count=state.count + 1,
                retry_at=now + policy.backoff_seconds * 2**state.count,
                pending=True,
            ),
        )
    return RetryPlan(disposition, state)
