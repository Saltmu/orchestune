"""Production adapters, the Event correspondence table and execution drivers (#1264).

`EVENT_BY_SOURCE` maps every production label path - each `CALL_SITES` key, each
`OUT_OF_SCOPE_PATHS` entry and the three label-invariant completion paths - to
the Event routes it can produce. A route names the executed cases that cover
it, so a source or branch without an executed case fails the build.

The drivers below run the real production functions against an in-memory forge.
Only collaborators outside the label decision (worktree removal, process
liveness, external-execution holds, evidence lookups that need git or the
network) are stubbed; forge mutations are never replaced by a mock.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.retry_policy import (
    RetryDisposition,
    RetryState,
    early_death_policy,
    plan_retry,
    review_timeout_policy,
)
from orchestune.ledger.status_events import (
    BackoffState,
    BudgetLimits,
    Event,
    Kind,
)
from tests.dispatch_test_support import make_test_dispatcher_config

#: `attempt >= 3` is a literal in the production base-branch-red paths
#: (`dispatch/gc/policies.py::_prepare`, `dispatch/gc/outcome_decision.py`).
BASE_BRANCH_RED_ATTEMPTS = 3


def production_backoff_planner(config: DispatcherConfig) -> Any:
    """The model's backoff decision, delegated to `retry_policy.plan_retry`."""

    def plan(kind: Kind, state: BackoffState, now: float) -> BackoffState | None:
        policy = (
            early_death_policy(
                config.max_early_death_retries, config.early_death_backoff_seconds
            )
            if kind is Kind.EARLY_DEATH
            else review_timeout_policy(
                config.max_review_timeout_retries,
                config.review_timeout_backoff_seconds,
            )
        )
        current = RetryState(state.count, state.retry_at, state.pending)
        planned = plan_retry(policy, current, now=now)
        if planned.disposition is RetryDisposition.EXHAUSTED:
            return None
        return BackoffState(
            planned.state.count, planned.state.retry_at, planned.state.pending
        )

    return plan


def limits_from_config(config: DispatcherConfig) -> BudgetLimits:
    return BudgetLimits(
        max_task_reclaims=config.max_task_reclaims,
        max_recompute_retries=config.max_recompute_retries,
        base_branch_red_attempts=BASE_BRANCH_RED_ATTEMPTS,
        plan_backoff=production_backoff_planner(config),
    )


def production_limits(state_root: Path | None = None, **overrides: Any) -> BudgetLimits:
    """Limits built from a `DispatcherConfig` with the given field overrides."""
    return limits_from_config(make_test_dispatcher_config(state_root, **overrides))


@dataclass(frozen=True)
class Route:
    """One Event a source can produce, and the executed cases that cover it."""

    event: Event
    kind: Kind = Kind.PLAIN
    cases: tuple[str, ...] = ()
