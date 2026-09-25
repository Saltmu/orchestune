"""Issue-only idempotent posting for completion Outcome Records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from orchestune.outcome_record import OutcomeRecord, parse_from_comments


class OutcomeIssueForge(Protocol):
    """Narrow public Forge capability required by Issue outcome posting."""

    def list_all_issue_comments(
        self, issue_number: int | str
    ) -> list[dict[str, Any]]: ...

    def create_issue_comment(
        self, issue_number: int | str, body: str
    ) -> dict[str, Any]: ...


class OutcomePostingError(RuntimeError):
    """Raised when GitHub rejects a completion outcome post."""


class OutcomeLookupUnknownError(OutcomePostingError):
    """Raised when all Issue comments cannot be conclusively inspected."""


@dataclass(frozen=True)
class PostingRequest:
    """The canonical Issue comment to find or post for one completion."""

    issue_number: int
    outcome_record: OutcomeRecord

    def __post_init__(self) -> None:
        if self.issue_number != self.outcome_record.issue:
            raise ValueError("posting Issue number must match outcome record")
        if not self.outcome_record.completion_id:
            raise ValueError("outcome record requires a completion_id before posting")


@dataclass(frozen=True)
class PostingResult:
    """Durable GitHub evidence for an outcome comment."""

    comment_id: str
    comment_url: str
    reused: bool


def _comment_evidence(comment: dict[str, Any]) -> tuple[str, str] | None:
    comment_id = comment.get("id")
    comment_url = comment.get("html_url")
    if comment_id is None or not isinstance(comment_url, str) or not comment_url:
        return None
    return str(comment_id), comment_url


def _find_existing(
    request: PostingRequest, forge: OutcomeIssueForge
) -> PostingResult | None:
    try:
        comments = forge.list_all_issue_comments(request.issue_number)
    except Exception as exc:
        raise OutcomeLookupUnknownError(
            "Unable to retrieve every Issue comment page"
        ) from exc

    for comment in comments:
        record = parse_from_comments([comment])
        if (
            record is None
            or record.completion_id != request.outcome_record.completion_id
        ):
            continue
        if record.issue != request.issue_number:
            continue
        evidence = _comment_evidence(comment)
        if evidence is None:
            raise OutcomeLookupUnknownError(
                "Matching outcome comment has no durable id and HTML URL"
            )
        return PostingResult(*evidence, reused=True)
    return None


def _post_new(request: PostingRequest, forge: OutcomeIssueForge) -> PostingResult:
    response = forge.create_issue_comment(
        request.issue_number, request.outcome_record.render()
    )
    if not isinstance(response, dict):
        raise OutcomePostingError("GitHub did not return a comment object")
    evidence = _comment_evidence(response)
    if evidence is None:
        raise OutcomePostingError("GitHub did not return a comment id and HTML URL")
    return PostingResult(*evidence, reused=False)


def post_issue_outcome(
    request: PostingRequest, *, forge: OutcomeIssueForge
) -> PostingResult:
    """Find or post exactly one Outcome Record on the Issue itself.

    A failed POST is ambiguous: GitHub may have accepted it while its response was
    lost. Recheck every comment page and never retry POST blindly.
    """
    existing = _find_existing(request, forge)
    if existing is not None:
        return existing

    try:
        return _post_new(request, forge)
    except Exception as post_error:
        try:
            recovered = _find_existing(request, forge)
        except OutcomeLookupUnknownError as lookup_error:
            raise OutcomeLookupUnknownError(
                "POST result is unknown and Issue comments could not be rechecked"
            ) from lookup_error
        if recovered is not None:
            return recovered
        raise OutcomePostingError(
            "POST failed and no matching Issue outcome was found"
        ) from post_error


__all__ = [
    "OutcomeLookupUnknownError",
    "OutcomePostingError",
    "PostingRequest",
    "PostingResult",
    "post_issue_outcome",
]
