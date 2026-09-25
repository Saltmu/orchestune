"""Issue-only idempotent posting for completion Outcome Records."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orchestune.outcome_record import OutcomeRecord, parse_from_comments

_Runner = Callable[[list[str], str | None], Any]


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


def _run_json(runner: _Runner, args: list[str], input_text: str | None = None) -> Any:
    raw = runner(args, input_text)
    return json.loads(raw) if isinstance(raw, str) else raw


def _comment_pages(raw: Any) -> list[dict[str, Any]]:
    """Normalize ``gh api --paginate --slurp`` output without assuming one page."""
    pages = raw if isinstance(raw, list) else [raw]
    comments: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, list):
            raise OutcomeLookupUnknownError(
                "GitHub comments response has an invalid page"
            )
        comments.extend(comment for comment in page if isinstance(comment, dict))
    return comments


def _comment_evidence(comment: dict[str, Any]) -> tuple[str, str] | None:
    comment_id = comment.get("id")
    comment_url = comment.get("html_url")
    if comment_id is None or not isinstance(comment_url, str) or not comment_url:
        return None
    return str(comment_id), comment_url


def _find_existing(request: PostingRequest, runner: _Runner) -> PostingResult | None:
    endpoint = (
        f"repos/{{owner}}/{{repo}}/issues/{request.issue_number}/comments?per_page=100"
    )
    try:
        comments = _comment_pages(
            _run_json(runner, ["gh", "api", "--paginate", "--slurp", endpoint])
        )
    except OutcomeLookupUnknownError:
        raise
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


def _post_new(request: PostingRequest, runner: _Runner) -> PostingResult:
    endpoint = f"repos/{{owner}}/{{repo}}/issues/{request.issue_number}/comments"
    response = _run_json(
        runner,
        ["gh", "api", "--method", "POST", endpoint, "--input", "-"],
        json.dumps({"body": request.outcome_record.render()}),
    )
    if not isinstance(response, dict):
        raise OutcomePostingError("GitHub did not return a comment object")
    evidence = _comment_evidence(response)
    if evidence is None:
        raise OutcomePostingError("GitHub did not return a comment id and HTML URL")
    return PostingResult(*evidence, reused=False)


def post_issue_outcome(request: PostingRequest, *, runner: _Runner) -> PostingResult:
    """Find or post exactly one Outcome Record on the Issue itself.

    A failed POST is ambiguous: GitHub may have accepted it while its response was
    lost. Recheck every comment page and never retry POST blindly.
    """
    existing = _find_existing(request, runner)
    if existing is not None:
        return existing

    try:
        return _post_new(request, runner)
    except Exception as post_error:
        try:
            recovered = _find_existing(request, runner)
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
