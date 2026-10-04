"""Bind worker judgments to a freshly acquired, head-bound PR review round."""

from __future__ import annotations

import re
from typing import Any, Protocol

from orchestune.complete.contracts import (
    CompleteFailureReason,
    CompleteRequest,
    DonePayload,
)
from orchestune.complete.journal import CompletionJournalError
from orchestune.outcome_record import ReviewSummary
from orchestune.review.acquisition import collect_review_state, normalize_review_state
from orchestune.review.judgment import (
    judgment_digest,
    parse_judgments,
    stable_digest,
    validate_coverage,
)
from orchestune.review.markers import derive_review_target, parse_selection_marker
from orchestune.review.rounds import (
    ReviewTrigger,
    restore_triggers,
    trigger_comment_ids,
)


class ReviewEvidenceForge(Protocol):
    """Only review acquisition methods; the shared Forge protocol stays stable."""

    def list_all_issue_comments(
        self, issue_number: int | str
    ) -> list[dict[str, Any]]: ...
    def list_pull_request_reviews(
        self, pr_number: int | str
    ) -> list[dict[str, Any]]: ...
    def list_pull_request_review_comments(
        self, pr_number: int | str
    ) -> list[dict[str, Any]]: ...


def _invalid(message: str) -> CompletionJournalError:
    return CompletionJournalError(
        CompleteFailureReason.REVIEW_EVIDENCE_INVALID, message
    )


def _head_mismatch() -> CompletionJournalError:
    return CompletionJournalError(
        CompleteFailureReason.REVIEW_HEAD_MISMATCH,
        "Review target is unknown or differs from PR/local HEAD; return to Step 11 for re-review",
    )


def _fetch(forge: ReviewEvidenceForge, pr: int, reviewer: str) -> dict[str, Any]:
    try:
        state: dict[str, Any] = {"issue_comments": forge.list_all_issue_comments(pr)}
        if reviewer != "skip":
            state.update(
                reviews=forge.list_pull_request_reviews(pr),
                inline_comments=forge.list_pull_request_review_comments(pr),
            )
        # Every method returns only fully paginated results, or raises.
        normalized = normalize_review_state(state)
        return {**normalized, "completeness": dict.fromkeys(normalized, "complete")}
    except Exception as error:
        raise CompletionJournalError(
            CompleteFailureReason.EVIDENCE_MISSING,
            "Complete PR review snapshot is unavailable",
        ) from error


def _latest_trigger(comments: list[dict[str, Any]], reviewer: str) -> ReviewTrigger:
    """The PR-wide latest trigger; ambiguous or contradictory triggers are rejected."""
    try:
        triggers = restore_triggers(comments)
    except ValueError as error:
        raise _invalid(f"Review triggers are ambiguous: {error}") from error
    if not triggers:
        raise _invalid("Latest review trigger is missing")
    if triggers[-1].reviewer != reviewer:
        raise _invalid("Latest trigger reviewer differs from --reviewer")
    return triggers[-1]


def _acquire(state: dict[str, Any], reviewer: str, head: str | None) -> dict[str, Any]:
    trigger = _latest_trigger(state["issue_comments"], reviewer)
    requested = trigger.requested_head_sha
    # Even review_commit evidence requires a head-bound trigger; old requests
    # cannot certify which head was requested in this round.
    if requested is None or requested != head:
        raise _head_mismatch()
    result = collect_review_state(
        state,
        reviewer,
        exclude_issue_comment_ids=trigger_comment_ids(state["issue_comments"]),
        round_started_at=trigger.created_at,
    )
    if result["acquisition_status"] != "acquired" or any(
        status != "complete" for status in state["completeness"].values()
    ):
        raise _invalid("Review acquisition is incomplete or not final")
    target, source = derive_review_target(result["review_items"], requested, head)
    if target is None or target != head:
        raise _head_mismatch()
    return {
        **result,
        "round": trigger.round,
        "review_target_sha": target,
        "review_target_sha_source": source,
        "trigger_id": trigger.id,
    }


def _judgments(
    payload: DonePayload, result: dict[str, Any]
) -> tuple[str, dict[str, int]]:
    if payload.review_reply is None:
        raise _invalid("--review-reply is required for claude/codex")
    try:
        table = parse_judgments(payload.review_reply.read_text(encoding="utf-8"))
        validate_coverage(table, result)
    except (OSError, UnicodeError, ValueError) as error:
        raise _invalid(f"Review judgments are invalid: {error}") from error
    rows = table["findings"]
    if any(
        row["status"] == "unresolved"
        or row["judgment"] == "needs_information"
        or (row["status"] == "deferred" and row["judgment"] == "adopt")
        for row in rows
    ):
        raise _invalid(
            "Review judgments contain unresolved or required deferred findings"
        )
    return judgment_digest(table)


def _review_binding(result: dict[str, Any]) -> dict[str, Any]:
    """Bind current review content; unrelated PR conversation is not evidence."""
    return {
        "round": result["round"],
        "trigger_id": result["trigger_id"],
        "review_target_sha": result["review_target_sha"],
        **{
            section: [
                item for item in result[section] if item["provenance"] == "current"
            ]
            for section in ("review_items", "inline_comments")
        },
    }


def verify_review_evidence(
    request: CompleteRequest,
    forge: ReviewEvidenceForge,
    pr: Any,
    head_sha: str | None,
    *,
    snapshot: dict[str, Any] | None = None,
) -> ReviewSummary:
    """Only fresh acquisition plus a complete judgment table can produce pass."""
    payload = request.payload
    assert isinstance(payload, DonePayload)
    reviewer = payload.reviewer
    if reviewer not in {"claude", "codex", "skip"}:
        raise _invalid("--reviewer is required for done")
    if (
        not head_sha
        or not re.fullmatch(r"[0-9a-f]{40}", head_sha)
        or pr.head_sha != head_sha
    ):
        raise _head_mismatch()
    state = _fetch(forge, payload.pr, reviewer)
    if reviewer == "skip":
        if not any(
            parse_selection_marker(item.get("body") or "") == ("skip", head_sha)
            for item in state["issue_comments"]
        ):
            raise _invalid(
                "skip requires a review-selection marker for the current head"
            )
        summary = ReviewSummary(
            bot="skip", verdict="skipped", reviewed_head_sha=head_sha
        )
        binding = {"selection": ("skip", head_sha)}
    else:
        result = _acquire(state, reviewer, pr.head_sha)
        digest, counts = _judgments(payload, result)
        summary = ReviewSummary(
            bot=reviewer,
            rounds=result["round"],
            verdict="pass",
            reviewed_head_sha=head_sha,
            review_target_sha_source=result["review_target_sha_source"],
            judgment_digest=digest,
            judgment_counts=counts,
        )
        binding = _review_binding(result)
    if snapshot is not None:
        snapshot.update(
            summary=summary.to_dict(), snapshot_digest=stable_digest(binding)
        )
    return summary
