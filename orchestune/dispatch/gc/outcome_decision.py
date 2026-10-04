"""Pure completion-outcome decisions shared by GC completion handling."""

from __future__ import annotations

from typing import Literal

from orchestune.complete.contracts import CompleteStage
from orchestune.dispatch.retry_policy import (
    DEFAULT_REVIEW_TIMEOUT_MAX_ATTEMPTS,
    RetryDisposition,
    RetryState,
    retry_disposition,
    review_timeout_policy,
)
from orchestune.ledger.run_state import ActiveWorktree, RunState, TaskReclaimRecord
from orchestune.outcome_record import (
    REASON_BASE_BRANCH_RED,
    REASON_REVIEW_TIMEOUT,
    RESULT_BLOCKED,
    RESULT_DONE,
    RESULT_NOT_NEEDED,
    OutcomeRecord,
)


def _is_handoff_ready(active: ActiveWorktree) -> bool:
    """#1004: handoff-ready 状態かどうかを判定する。"""
    completion = active.completion
    return bool(
        completion.completion_handoff_ready
        and completion.completion_stage == CompleteStage.HANDED_OFF.value
    )


def _is_handoff_retained_dirty(
    active: ActiveWorktree, outcome: OutcomeRecord | None
) -> bool:
    """#1004: handoff-ready かつ not-needed/blocked の dirty worktree 保持判定。"""
    return bool(
        _is_handoff_ready(active)
        and outcome is not None
        and outcome.result in (RESULT_NOT_NEEDED, RESULT_BLOCKED)
    )


def _decide_action_from_outcome(
    outcome: OutcomeRecord | None,
    has_new_commits: bool,
    review_timeout_retry_count: int = 0,
    max_review_timeout_retries: int = DEFAULT_REVIEW_TIMEOUT_MAX_ATTEMPTS,
    review_timeout_retry_pending: bool = False,
) -> Literal[
    "completed",
    "completed_no_commits",
    "completed_without_outcome",
    "not_needed",
    "escalated_base_branch_red",
    "blocked_base_branch_red",
    "escalated_review_timeout",
    "blocked_review_timeout",
    "blocked_unknown_reason",
]:
    if outcome is None:
        return (
            "completed_without_outcome" if has_new_commits else "completed_no_commits"
        )
    if outcome.result == RESULT_NOT_NEEDED:
        return "not_needed"
    if outcome.result == RESULT_BLOCKED:
        if outcome.reason == REASON_BASE_BRANCH_RED:
            attempt = outcome.attempt if outcome.attempt is not None else 1
            return (
                "escalated_base_branch_red"
                if attempt >= 3
                else "blocked_base_branch_red"
            )
        if outcome.reason == REASON_REVIEW_TIMEOUT:
            disposition = retry_disposition(
                review_timeout_policy(max_review_timeout_retries),
                review_timeout_retry_count,
                review_timeout_retry_pending,
            )
            return (
                "escalated_review_timeout"
                if disposition is RetryDisposition.EXHAUSTED
                else "blocked_review_timeout"
            )
        return "blocked_unknown_reason"
    if outcome.result == RESULT_DONE:
        return "completed" if has_new_commits else "completed_no_commits"
    return "blocked_unknown_reason"


RetryKind = Literal["early_death", "review_timeout"]

# JSON-facing field names of the ledger record per retry kind. This table is the only
# connection information; limits and the backoff formula live in `retry_policy`.
_RETRY_FIELDS: dict[RetryKind, tuple[str, str, str]] = {
    "early_death": (
        "early_death_retry_count",
        "early_death_retry_at",
        "early_death_retry_pending",
    ),
    "review_timeout": (
        "review_timeout_retry_count",
        "review_timeout_retry_at",
        "review_timeout_retry_pending",
    ),
}


def read_retry_state(record: TaskReclaimRecord | None, kind: RetryKind) -> RetryState:
    """Read one retry kind of a (possibly absent) ledger record."""
    if record is None:
        return RetryState()
    count, retry_at, pending = (getattr(record, name) for name in _RETRY_FIELDS[kind])
    return RetryState(count=count, retry_at=retry_at, pending=pending)


def write_retry_state(
    record: TaskReclaimRecord, kind: RetryKind, state: RetryState
) -> None:
    """Store a planned retry state into the ledger record's fields for ``kind``."""
    count, retry_at, pending = _RETRY_FIELDS[kind]
    setattr(record, count, state.count)
    setattr(record, retry_at, state.retry_at)
    setattr(record, pending, state.pending)


def _get_review_timeout_retry_state(
    run_state: RunState | None, issue_number: int
) -> tuple[int, bool]:
    record = run_state.task_reclaim_counts.get(issue_number) if run_state else None
    state = read_retry_state(record, "review_timeout")
    return state.count, state.pending


__all__ = [
    "_decide_action_from_outcome",
    "_get_review_timeout_retry_state",
    "_is_handoff_ready",
    "_is_handoff_retained_dirty",
    "RetryKind",
    "read_retry_state",
    "write_retry_state",
]
