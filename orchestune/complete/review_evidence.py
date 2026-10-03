"""Bind worker judgments to a freshly acquired, head-bound PR review round."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any, Protocol

from orchestune.complete.contracts import (
    CompleteFailureReason,
    CompleteRequest,
    DonePayload,
)
from orchestune.complete.journal import CompletionJournalError
from orchestune.outcome_record import ReviewSummary
from orchestune.review.acquisition import collect_review_state, normalize_review_state
from orchestune.review.judgment import FIELDS, parse_judgments, validate_coverage
from orchestune.review.markers import (
    derive_review_target,
    parse_head_marker,
    parse_round_marker,
    parse_selection_marker,
    parse_trigger_reviewer,
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


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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


def _latest_trigger(comments: list[dict[str, Any]], reviewer: str) -> dict[str, Any]:
    triggers = [
        item for item in comments if parse_trigger_reviewer(item.get("body") or "")
    ]
    if not triggers:
        raise _invalid("Latest review trigger is missing")
    trigger = max(
        triggers,
        key=lambda item: (
            parse_round_marker(item.get("body") or "") or 0,
            str(item.get("created_at") or ""),
        ),
    )
    body = trigger.get("body") or ""
    if parse_trigger_reviewer(body) != reviewer:
        raise _invalid("Latest trigger reviewer differs from --reviewer")
    if not parse_round_marker(body) or not trigger.get("created_at"):
        raise _invalid("Latest trigger round/timestamp is missing")
    return trigger


def _acquire(state: dict[str, Any], reviewer: str, head: str | None) -> dict[str, Any]:
    trigger = _latest_trigger(state["issue_comments"], reviewer)
    body = trigger["body"]
    requested = parse_head_marker(body)
    # Even review_commit evidence requires a head-bound trigger; old requests
    # cannot certify which head was requested in this round.
    if requested is None or requested != head:
        raise _head_mismatch()
    trigger_ids = {
        item["id"]
        for item in state["issue_comments"]
        if parse_trigger_reviewer(item.get("body") or "") and item.get("id") is not None
    }
    result = collect_review_state(
        state, reviewer, exclude_ids=trigger_ids, round_started_at=trigger["created_at"]
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
        "round": parse_round_marker(body),
        "review_target_sha": target,
        "review_target_sha_source": source,
        "trigger_id": trigger.get("id"),
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
    # Normalize fields, row ordering and irrelevant YAML formatting before hashing.
    normalized = [{name: row[name].strip() for name in FIELDS} for row in rows]
    normalized.sort(key=lambda row: tuple(row[name] for name in FIELDS))
    digest = _digest({"round": table["round"], "findings": normalized})
    counts = Counter(row["judgment"] for row in normalized)
    counts.update(row["status"] for row in normalized)
    return digest, dict(sorted(counts.items()))


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
    if snapshot is not None:
        snapshot.update(summary=summary.to_dict(), snapshot_digest=_digest(state))
    return summary
