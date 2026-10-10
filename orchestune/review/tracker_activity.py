"""Associate a reused Codex tracker comment with a review round (#1274).

Codex edits one tracker issue comment for every round, so its ``created_at``
stays at the first round while ``updated_at`` follows the latest status. This
module decides when such a comment is *current activity* of a round. It is
telemetry only: ordinary comments, formal reviews and inline comments keep
being attributed by their creation / submission time, and a tracker is never
promoted to review content or to a reviewed-SHA proof.

Pure functions: no network, filesystem or clock access (the grace timer takes
the current monotonic time from its caller).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from orchestune.review.progress_tracker import (
    parse_codex_tracker_commit,
    parse_codex_tracker_status,
)

DEFAULT_COMPLETED_GRACE_SECONDS = 30
PRECISE_SUFFIX = "_precise"

_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")


def parse_precise_utc(value: object) -> datetime | None:
    """A timezone-aware UTC moment that keeps fractional seconds, else ``None``.

    Naive, blank or malformed input is not evidence of new activity.
    """
    if not isinstance(value, str) or value != value.strip() or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def format_precise_utc(moment: datetime) -> str:
    """UTC text that keeps fractional seconds (``...:00.250000Z``)."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _field_time(item: dict[str, Any], field: str) -> tuple[bool, datetime | None]:
    """(present, moment); a preserved sub-second twin wins over the rounded text."""
    value = item.get(f"{field}{PRECISE_SUFFIX}") or item.get(field)
    if value is None or value == "":
        return False, None
    return True, parse_precise_utc(value)


def activity_time(item: dict[str, Any]) -> datetime | None:
    """When the item was last active: ``updated_at``, else ``created_at``.

    Only a missing / empty ``updated_at`` falls back; a present but invalid one
    is not rescued, so it can never count as current activity.
    """
    present, moment = _field_time(item, "updated_at")
    if present:
        return moment
    return _field_time(item, "created_at")[1]


def is_codex_tracker(item: dict[str, Any]) -> bool:
    return parse_codex_tracker_status(str(item.get("body") or "")) is not None


def _commit_conflicts(item: dict[str, Any], requested_head_sha: str | None) -> bool:
    """True only for a known tracker commit that is not a prefix of a known SHA."""
    if not requested_head_sha or not _FULL_SHA.fullmatch(requested_head_sha):
        return False
    commit = parse_codex_tracker_commit(str(item.get("body") or ""))
    return commit is not None and not requested_head_sha.lower().startswith(commit)


def tracker_is_current(
    item: dict[str, Any],
    *,
    started_at: str,
    ended_at: str = "",
    requested_head_sha: str | None = None,
) -> bool:
    """Whether a Codex tracker comment is activity of ``[started_at, ended_at)``.

    The interval is start-inclusive and end-exclusive; an empty bound is open.
    A tracker last updated at or after ``ended_at`` shows a later state than the
    closed round had, which is never reconstructed. A commit that explicitly
    differs from the requested SHA is another review's activity; a missing or
    unusable commit / SHA proves nothing either way.
    """
    if not is_codex_tracker(item):
        return False
    moment = activity_time(item)
    if moment is None:
        return False
    start = parse_precise_utc(started_at) if started_at else None
    end = parse_precise_utc(ended_at) if ended_at else None
    if (started_at and start is None) or (ended_at and end is None):
        return False
    if start is not None and moment < start:
        return False
    if end is not None and moment >= end:
        return False
    return not _commit_conflicts(item, requested_head_sha)


def tracker_digest(item: dict[str, Any]) -> str:
    """A short body digest for change detection; the body itself is never exposed."""
    body = str(item.get("body") or "")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


@dataclass
class CompletedGrace:
    """Deadline for a review to appear after its tracker reports Completed.

    The window opens when the process first observes Completed and is capped by
    the time left until the overall timeout. Observing Completed again never
    extends it; a return to Running resets it so a later Completed starts anew.
    """

    seconds: float = DEFAULT_COMPLETED_GRACE_SECONDS
    deadline: float | None = None

    @property
    def active(self) -> bool:
        return self.deadline is not None

    def observe_completed(self, *, now: float, timeout_remaining: float) -> None:
        if self.deadline is None:
            self.deadline = now + min(self.seconds, max(0.0, timeout_remaining))

    def reset(self) -> None:
        self.deadline = None

    def remaining(self, now: float) -> float | None:
        return None if self.deadline is None else max(0.0, self.deadline - now)

    def expired(self, now: float) -> bool:
        return self.deadline is not None and now >= self.deadline
