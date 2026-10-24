"""Diagnostics for a review wait that ended without a result (#1388).

A timeout (Exit 20) or a stalled tracker (Exit 21) used to leave only a stderr
line, and an earlier successful `--output-file` stayed in place. The polling
layer now attaches what it observed to the exception it raises, and the CLI
turns that into an `acquisition_status: unavailable` record. Nothing here
claims a reviewed or target SHA: a wait that ended is never review evidence.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from orchestune.review.acquisition import ACQUISITION_UNAVAILABLE, SCHEMA_VERSION

EXIT_TIMEOUT = 20  # Timeout waiting for review activity
EXIT_STALLED = 21  # In-progress tracker comment stopped changing; job likely ended

REASON_TIMEOUT = "review_timeout"
REASON_STALLED = "review_stalled"

SECTIONS = ("issue_comments", "reviews", "inline_comments")


def body_digest(item: dict[str, Any]) -> str:
    return hashlib.sha256(str(item.get("body") or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TrackerObservation:
    """The last current-round bot activity seen; its body is kept as a digest only."""

    id: int | str | None
    section: str
    status: str
    updated_at: str
    body_sha256: str


@dataclass(frozen=True)
class WaitContext:
    """What the poll loop knew when the wait ended. Unknown values stay None."""

    repository: str | None
    pr_number: int
    reviewer: str
    round: int | None
    trigger_id: int | str | None
    triggered_at: str | None
    requested_head_sha: str | None
    elapsed_seconds: float | None
    timeout_seconds: float | None
    stall_grace_seconds: float | None
    completed_grace_seconds: float | None
    last_poll_ok: bool
    last_tracker: TrackerObservation | None = None
    # Bot items seen since the trigger, as provenance only; never acquired content.
    observed_items: tuple[dict[str, Any], ...] = field(default_factory=tuple)


class ReviewWaitEnded(Exception):
    """A wait that ended without a review result, with its observed context."""

    reason_code = REASON_TIMEOUT
    exit_code = EXIT_TIMEOUT

    def __init__(self, message: str, context: WaitContext | None = None) -> None:
        super().__init__(message)
        self.context = context

    def attach(self, context: WaitContext) -> ReviewWaitEnded:
        if self.context is None:
            self.context = context
        return self


class ReviewTimeoutError(ReviewWaitEnded, TimeoutError):
    """The overall wait budget ran out (Exit 20)."""


class StalledReviewError(ReviewWaitEnded, RuntimeError):
    """Raised when a bot's in-progress tracker comment stops changing.

    This is distinct from a plain timeout: it means the review polling loop
    positively observed the same "in progress" tracker signature for longer
    than the stall grace window, which is strong evidence the workflow run
    that owns the comment already ended (success or failure) without ever
    posting a final result, rather than merely being slow.
    """

    reason_code = REASON_STALLED
    exit_code = EXIT_STALLED


def diagnostic_result(
    error: BaseException,
    *,
    pr_number: int,
    reviewer: str,
    round_num: int | None,
    current_head_sha: str | None,
) -> dict[str, Any]:
    """The `--output-file` record for an Exit 20/21 wait.

    `pr_number` / `reviewer` / `round_num` are the command-line request, used only
    when the wait ended before the poll loop knew more. `current_head_sha` is a
    fresh lookup; None means unknown and is never replaced by a requested SHA.
    """
    context = getattr(error, "context", None)
    stalled = isinstance(error, StalledReviewError)
    known = context is not None and context.last_poll_ok
    wait: dict[str, Any] = {
        "elapsed_seconds": None,
        "timeout_seconds": None,
        "stall_grace_seconds": None,
        "completed_grace_seconds": None,
        "last_tracker": None,
        "observed_items": [],
    }
    if context is not None:
        wait.update(
            elapsed_seconds=context.elapsed_seconds,
            timeout_seconds=context.timeout_seconds,
            stall_grace_seconds=context.stall_grace_seconds,
            completed_grace_seconds=context.completed_grace_seconds,
            last_tracker=(
                asdict(context.last_tracker) if context.last_tracker else None
            ),
            observed_items=list(context.observed_items),
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "acquisition_status": ACQUISITION_UNAVAILABLE,
        "reason": str(error),
        "reason_code": REASON_STALLED if stalled else REASON_TIMEOUT,
        "exit_code": EXIT_STALLED if stalled else EXIT_TIMEOUT,
        "repository": context.repository if context else None,
        "pr_number": context.pr_number if context else pr_number,
        "reviewer": context.reviewer if context else reviewer,
        "round": context.round if context else round_num,
        "trigger_id": context.trigger_id if context else None,
        "triggered_at": context.triggered_at if context else None,
        "requested_head_sha": context.requested_head_sha if context else None,
        "reviewed_head_sha": None,
        "current_head_sha": current_head_sha,
        "review_target_sha": None,
        "review_target_sha_source": "unknown",
        "review_items": [],
        "review_body": "",
        "inline_comments": [],
        "jev_evaluations": [],
        # Sections are only "complete" when the last poll fetched all of them.
        "completeness": dict.fromkeys(SECTIONS, "complete" if known else "unknown"),
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "wait_diagnostics": wait,
    }
