"""Bind worker judgments to a freshly acquired, head-bound PR review round."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

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
    MAX_REVIEW_ROUNDS,
    ReviewTrigger,
    precise_created_at,
    restore_triggers,
    trigger_comment_ids,
)


class IssueReference(Protocol):
    """What a follow-up reference must reveal: its state and whether it is a PR."""

    @property
    def state(self) -> str: ...
    @property
    def is_pull_request(self) -> bool: ...


class ReviewEvidenceForge(Protocol):
    """Only review acquisition and follow-up lookups; the shared Forge protocol stays stable."""

    def get_repository_slug(self) -> str: ...
    def get_issue_reference(self, number: int) -> IssueReference | None:
        """The Issue-or-PR `number`, or None once the forge confirms it is absent."""
        ...

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


def _missing(message: str) -> CompletionJournalError:
    return CompletionJournalError(CompleteFailureReason.EVIDENCE_MISSING, message)


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
    # The latest trigger is open-ended. A reused Codex tracker updated since the
    # trigger holds completion unless its commit explicitly names another head.
    result = collect_review_state(
        state,
        reviewer,
        exclude_issue_comment_ids=trigger_comment_ids(state["issue_comments"]),
        round_started_at=trigger.created_at,
        requested_head_sha=requested,
        activity_started_at=precise_created_at(state["issue_comments"], trigger),
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


_URL = re.compile(r"https?://[^\s<>()\[\]\"'`,;]+")
_PROSE_TAIL = ".:!?*_~'\""
_SHORT = re.compile(r"(?<![\w/&#])#([1-9][0-9]*)(?!\w)")
_ISSUE_PATH = re.compile(r"/([^/]+)/([^/]+)/issues/([1-9][0-9]*)/?")


def _reference_candidates(evidence: str, slug: str | None) -> set[int]:
    """Issue numbers `evidence` names in this repository.

    A URL is parsed as a structure: its fragment never becomes a `#N` shorthand,
    and only an https://github.com/<this repository>/issues/<N> URL counts.
    """
    numbers: set[int] = set()
    for url in _URL.findall(evidence):
        try:
            # Sentence punctuation and Markdown emphasis end the prose, not the URL.
            parts = urlsplit(url.rstrip(_PROSE_TAIL))
            port, host = parts.port, parts.hostname
        except ValueError:
            continue
        match = _ISSUE_PATH.fullmatch(parts.path)
        if (
            slug is not None
            and parts.scheme == "https"
            and host == "github.com"
            and port is None
            and parts.username is None
            and match
            and f"{match[1]}/{match[2]}".lower() == slug.lower()
        ):
            numbers.add(int(match[3]))
    numbers.update(int(n) for n in _SHORT.findall(_URL.sub(" ", evidence)))
    return numbers


@dataclass
class _FollowUps:
    """Verifies final-round follow-up references, sharing each lookup in one completion."""

    forge: ReviewEvidenceForge
    own_numbers: frozenset[int]
    cache: dict[int, IssueReference | None | Exception] = field(default_factory=dict)
    slug: str | None = None

    def _lookup(self, number: int) -> IssueReference | None | Exception:
        if number not in self.cache:
            try:
                self.cache[number] = self.forge.get_issue_reference(number)
            except Exception as error:
                self.cache[number] = error
        return self.cache[number]

    def _repository(self, evidence: str) -> str | None:
        if self.slug is None and _URL.search(evidence):
            try:
                self.slug = self.forge.get_repository_slug()
            except Exception as error:
                raise _missing("Repository identity is unavailable") from error
        return self.slug

    def verify(self, evidence: str) -> None:
        """Pass when one candidate is an OPEN Issue; otherwise reject or hold."""
        candidates = _reference_candidates(evidence, self._repository(evidence))
        unknown = False
        for number in sorted(candidates - self.own_numbers):
            found = self._lookup(number)
            if isinstance(found, Exception):
                unknown = True
            elif (
                found is not None
                and not found.is_pull_request
                and found.state.upper() == "OPEN"
            ):
                return
        if unknown:
            raise _missing("Follow-up Issue lookup failed")
        raise _invalid("Required deferred finding lacks a valid open follow-up Issue")


def _judgments(
    request: CompleteRequest, forge: ReviewEvidenceForge, result: dict[str, Any]
) -> tuple[str, dict[str, int]]:
    payload = request.payload
    assert isinstance(payload, DonePayload)
    if payload.review_reply is None:
        raise _invalid("--review-reply is required for claude/codex")
    try:
        table = parse_judgments(payload.review_reply.read_text(encoding="utf-8"))
        validate_coverage(table, result)
    except (OSError, UnicodeError, ValueError) as error:
        raise _invalid(f"Review judgments are invalid: {error}") from error
    rows = table["findings"]
    final = result["round"] >= MAX_REVIEW_ROUNDS
    followups = _FollowUps(forge, frozenset({request.issue_number, payload.pr}))
    required_deferred = 0
    for row in rows:
        required = row["judgment"] in {"adopt", "needs_information"}
        if row["status"] == "unresolved" or (
            row["judgment"] == "needs_information" and row["status"] != "deferred"
        ):
            raise _invalid("Review judgments contain unresolved findings")
        if row["status"] == "deferred" and required:
            if not final:
                raise _invalid(
                    "Required findings can be deferred only in the final round"
                )
            followups.verify(row["evidence"])
            required_deferred += 1
    digest, counts = judgment_digest(table)
    if required_deferred:
        counts = {**counts, "required_deferred": required_deferred}
    return digest, counts


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
        digest, counts = _judgments(request, forge, result)
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
