"""Pure detection and reset-time resolution for claude-cli session limits (#1270).

This module owns *what* a session-limit exit looks like and *when* the limit lifts.
It performs no I/O: callers pass already-read log bytes, ``now``/anchor timestamps and
an explicit IANA timezone. Persistence, log reading, labels and notifications belong to
``orchestune.dispatch.gc.usage_limit``.

Only an unambiguous signal is recognised. A bare ``429``, generic transport errors and
the phrase appearing inside quotes, code or prose are never a session limit; when the
text cannot be classified the caller keeps its existing handling.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from orchestune.dispatch.retry_policy import (
    RetryDisposition,
    RetryPlan,
    RetryPolicy,
    RetryState,
    plan_retry,
)

#: Bounded read window for the end of a run's log (bytes).
LOG_TAIL_MAX_BYTES = 64 * 1024
#: The signal must be among the last non-empty lines: a run that kept working after
#: printing the phrase did not terminate because of the limit.
_TAIL_LINES = 5
#: An absolute reset further away than this is treated as implausible.
MAX_RESET_HORIZON_SECONDS = 7 * 24 * 3600

_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-Z\\-_]|\][^\x07\x1b]*(?:\x07|\x1b\\))"
)
# The apostrophe is straight in the CLI text but may be typographic once rendered.
_PHRASE = r"You['\u2019]ve hit your session limit"
_LINE_RE = re.compile(
    r"^(?:(?:API )?Error:\s*)?"
    + _PHRASE
    + r"(?:\s*[·•|\-:,]\s*resets?\s+(?P<reset>.+?))?\s*\.?$",
    re.IGNORECASE,
)
_STRUCTURED_TYPE = "usage_limit_reached"
_TWELVE_HOUR_RE = re.compile(
    r"^(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)$", re.IGNORECASE
)
_TWENTY_FOUR_HOUR_RE = re.compile(r"^(?P<h>\d{1,2}):(?P<m>\d{2})$")
_EPOCH_RE = re.compile(r"^\d{9,11}(?:\.\d+)?$")


@dataclass(frozen=True)
class UsageLimitSignal:
    """A recognised session-limit termination and the raw reset text, if any."""

    kind: Literal["structured", "session_limit_line"]
    reset_text: str | None


@dataclass(frozen=True)
class ResetResolution:
    """Outcome of resolving the reset text. ``reset_at`` is UTC epoch seconds."""

    reset_at: float | None
    timezone: str | None
    reason: str

    @property
    def known(self) -> bool:
        return self.reset_at is not None


def sanitize_log_tail(raw: bytes, *, max_bytes: int = LOG_TAIL_MAX_BYTES) -> str:
    """Decode the end of ``raw`` (bounded) and strip ANSI escape sequences."""
    tail = raw[-max_bytes:] if max_bytes > 0 else b""
    text = tail.decode("utf-8", errors="replace")
    return _ANSI_RE.sub("", text)


def detect_usage_limit(text: str) -> UsageLimitSignal | None:
    """Return the session-limit signal ending ``text``, or ``None`` when unsure."""
    cleaned = _ANSI_RE.sub("", text)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    for line in reversed(lines[-_TAIL_LINES:]):
        signal = _signal_from_line(line)
        if signal is not None:
            return signal
    return None


def _signal_from_line(line: str) -> UsageLimitSignal | None:
    if line.startswith("{"):
        return _signal_from_json(line)
    match = _LINE_RE.match(line)
    if match is None:
        return None
    return UsageLimitSignal("session_limit_line", _clean_reset(match.group("reset")))


def _signal_from_json(line: str) -> UsageLimitSignal | None:
    try:
        payload = json.loads(line)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    error_dict = error if isinstance(error, dict) else {}
    if (
        payload.get("type") == _STRUCTURED_TYPE
        or error_dict.get("type") == _STRUCTURED_TYPE
    ):
        return UsageLimitSignal("structured", _structured_reset(payload, error_dict))
    result = payload.get("result")
    if payload.get("is_error") is True and isinstance(result, str):
        match = _LINE_RE.match(result.strip())
        if match is not None:
            return UsageLimitSignal("structured", _clean_reset(match.group("reset")))
    return None


def _structured_reset(payload: dict, error: dict) -> str | None:
    for source in (error, payload):
        value = source.get("resets_at")
        if isinstance(value, str | int | float) and not isinstance(value, bool):
            return str(value)
    return None


def _clean_reset(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().rstrip(".").strip()
    return cleaned or None


def _unknown(reason: str, timezone: str | None = None) -> ResetResolution:
    return ResetResolution(None, timezone, reason)


def resolve_reset(
    signal: UsageLimitSignal,
    *,
    now: float,
    anchor: float,
    timezone: str | None,
) -> ResetResolution:
    """Resolve the reset text to a UTC epoch, or an explicit *unknown* with a reason.

    ``anchor`` is when the message was written: a wall-clock time such as ``1pm`` means
    the first such time after the anchor. If that moment is already past ``now`` the
    reset is unknown rather than guessed. An absolute time with an offset is preferred;
    a wall-clock time is only interpreted in the explicitly configured ``timezone``
    (never as an implicit UTC).
    """
    text = signal.reset_text
    if text is None:
        return _unknown("no reset time in the message")
    absolute = _parse_absolute(text)
    if absolute is not None:
        reset_at, zone_label = absolute
        return _check_horizon(reset_at, zone_label, now)
    return _resolve_wall_clock(text, now=now, anchor=anchor, timezone=timezone)


def _parse_absolute(text: str) -> tuple[float, str] | None:
    if _EPOCH_RE.match(text):
        return float(text), "UTC"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    offset = parsed.utcoffset()
    assert offset is not None
    label = (
        "UTC"
        if text.endswith(("Z", "z")) or offset == timedelta(0)
        else _format_offset(offset)
    )
    return parsed.timestamp(), label


def _format_offset(offset: timedelta) -> str:
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    hours, rem = divmod(abs(total), 3600)
    return f"{sign}{hours:02d}:{rem // 60:02d}"


def _check_horizon(reset_at: float, zone: str | None, now: float) -> ResetResolution:
    if reset_at <= now:
        return _unknown("reset time is already in the past", zone)
    if reset_at - now > MAX_RESET_HORIZON_SECONDS:
        return _unknown("reset time is implausibly far away", zone)
    return ResetResolution(reset_at, zone, "absolute")


def _parse_clock(text: str) -> tuple[int, int] | None:
    candidate = text.strip()
    twelve = _TWELVE_HOUR_RE.match(candidate)
    if twelve is not None:
        hour, minute = int(twelve["h"]), int(twelve["m"] or 0)
        if not 1 <= hour <= 12 or minute > 59:
            return None
        hour = hour % 12 + (12 if twelve["ap"].lower() == "pm" else 0)
        return hour, minute
    twenty_four = _TWENTY_FOUR_HOUR_RE.match(candidate)
    if twenty_four is not None:
        hour, minute = int(twenty_four["h"]), int(twenty_four["m"])
        if hour > 23 or minute > 59:
            return None
        return hour, minute
    return None


def _resolve_wall_clock(
    text: str, *, now: float, anchor: float, timezone: str | None
) -> ResetResolution:
    clock = _parse_clock(text)
    if clock is None:
        return _unknown("unsupported reset time format")
    if not timezone:
        return _unknown("wall-clock reset needs an explicit usage_limit_timezone")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return _unknown(f"unknown timezone {timezone!r}")
    local_anchor = datetime.fromtimestamp(anchor, tz=zone)
    hour, minute = clock
    day = local_anchor.date()
    for _ in range(2):
        naive = datetime(day.year, day.month, day.day, hour, minute)
        problem = _dst_problem(naive, zone)
        if problem is not None:
            return _unknown(problem, timezone)
        candidate = naive.replace(tzinfo=zone)
        if candidate.timestamp() > anchor:
            return _check_horizon(candidate.timestamp(), timezone, now)
        day += timedelta(days=1)
    return _unknown("reset time could not be placed after the message", timezone)


def _dst_problem(naive: datetime, zone: ZoneInfo) -> str | None:
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        return "ambiguous local time (dst transition)"
    roundtrip = first.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if roundtrip != naive:
        return "nonexistent local time (dst transition)"
    return None


def plan_usage_limit_retry(
    policy: RetryPolicy,
    state: RetryState,
    reset: ResetResolution,
    *,
    grace_seconds: float,
    now: float,
) -> RetryPlan:
    """Plan the next session-limit retry state without mutating ``state``.

    The limit comparison and pending reuse come from ``retry_policy.plan_retry``. When
    the reset is known the retry waits for ``reset + grace``; otherwise the finite
    exponential backoff applies, so an unknown reset never retries immediately.
    """
    plan = plan_retry(policy, state, now=now)
    if plan.disposition is not RetryDisposition.NEW or reset.reset_at is None:
        return plan
    return RetryPlan(
        plan.disposition,
        replace(plan.state, retry_at=reset.reset_at + grace_seconds),
    )
