"""Pure completion-outcome decisions shared by GC completion handling."""

from __future__ import annotations

from orchestune.dispatch.state import RunState
from orchestune.outcome_record import (
    REASON_BASE_BRANCH_RED,
    REASON_REVIEW_TIMEOUT,
    RESULT_BLOCKED,
    RESULT_DONE,
    RESULT_NOT_NEEDED,
    OutcomeRecord,
)


def _decide_action_from_outcome(
    outcome: OutcomeRecord | None,
    has_new_commits: bool,
    review_timeout_retry_count: int = 0,
    max_review_timeout_retries: int = 2,
    review_timeout_retry_pending: bool = False,
) -> str:
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
            escalated = (
                review_timeout_retry_count >= max_review_timeout_retries - 1
                and not review_timeout_retry_pending
            )
            return "escalated_review_timeout" if escalated else "blocked_review_timeout"
        return "blocked_unknown_reason"
    if outcome.result == RESULT_DONE:
        return "completed" if has_new_commits else "completed_no_commits"
    return "blocked_unknown_reason"


def _get_review_timeout_retry_state(
    run_state: RunState | None, issue_number: int
) -> tuple[int, bool]:
    record = run_state.task_reclaim_counts.get(issue_number) if run_state else None
    if record is not None:
        return record.review_timeout_retry_count, record.review_timeout_retry_pending
    return 0, False


__all__ = ["_decide_action_from_outcome", "_get_review_timeout_retry_state"]
