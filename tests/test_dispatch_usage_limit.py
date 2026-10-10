"""#1270: claude-cli session-limit detection and reset-time resolution (pure)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from orchestune.dispatch.retry_policy import (
    RetryDisposition,
    RetryState,
    usage_limit_policy,
)
from orchestune.dispatch.usage_limit import (
    UsageLimitSignal,
    detect_usage_limit,
    plan_usage_limit_retry,
    resolve_reset,
    sanitize_log_tail,
)

TOKYO = "Asia/Tokyo"
NEW_YORK = "America/New_York"


def _epoch(year: int, month: int, day: int, hour: int, minute: int, tz: str) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz)).timestamp()


# Sanitized representations of the two formats named in #1270.
SESSION_LINE = "You've hit your session limit · resets 1pm"
STRUCTURED_ERROR = json.dumps(
    {
        "type": "error",
        "error": {
            "type": "usage_limit_reached",
            "message": "The session usage limit has been reached",
            "resets_at": "2026-10-10T13:00:00+09:00",
        },
    }
)
STRUCTURED_RESULT = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": SESSION_LINE,
    }
)


class TestDetectUsageLimit:
    def test_session_limit_line_at_the_end_is_detected(self) -> None:
        log = f"working...\nrunning tests\n{SESSION_LINE}\n"
        signal = detect_usage_limit(log)
        assert signal == UsageLimitSignal(kind="session_limit_line", reset_text="1pm")

    def test_structured_error_is_detected_with_its_absolute_reset(self) -> None:
        signal = detect_usage_limit(f"noise\n{STRUCTURED_ERROR}\n")
        assert signal is not None
        assert signal.kind == "structured"
        assert signal.reset_text == "2026-10-10T13:00:00+09:00"

    def test_structured_result_with_error_flag_is_detected(self) -> None:
        signal = detect_usage_limit(STRUCTURED_RESULT + "\n")
        assert signal is not None
        assert signal.reset_text == "1pm"

    def test_error_prefix_before_the_phrase_is_accepted(self) -> None:
        signal = detect_usage_limit(
            "Error: You've hit your session limit · resets 12am"
        )
        assert signal is not None
        assert signal.reset_text == "12am"

    def test_typographic_apostrophe_is_accepted(self) -> None:
        signal = detect_usage_limit(
            "You\u2019ve hit your session limit \u00b7 resets 1pm"
        )
        assert signal == UsageLimitSignal(kind="session_limit_line", reset_text="1pm")

    def test_phrase_without_reset_is_still_a_signal_with_unknown_reset(self) -> None:
        signal = detect_usage_limit("You've hit your session limit")
        assert signal == UsageLimitSignal(kind="session_limit_line", reset_text=None)

    def test_ansi_sequences_are_stripped_before_matching(self) -> None:
        signal = detect_usage_limit(
            "\x1b[31mYou've hit your session limit\x1b[0m · resets 3am\n"
        )
        assert signal is not None
        assert signal.reset_text == "3am"

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "all good\nPR created\n",
            "HTTP 429 Too Many Requests",
            "429 rate_limit_error: slow down",
            '{"type":"error","error":{"type":"overloaded_error"}}',
            "connection reset by peer",
        ],
    )
    def test_unrelated_or_generic_errors_are_not_a_session_limit(
        self, text: str
    ) -> None:
        assert detect_usage_limit(text) is None

    @pytest.mark.parametrize(
        "line",
        [
            f'The agent printed "{SESSION_LINE}" earlier',
            f"> {SESSION_LINE}",
            f"`{SESSION_LINE}`",
            f'    assert "{SESSION_LINE}" in log',
            f"Handle the message: {SESSION_LINE}",
        ],
    )
    def test_quoted_or_embedded_phrase_is_not_a_signal(self, line: str) -> None:
        assert detect_usage_limit(f"{line}\nmore output\n") is None

    def test_signal_followed_by_later_work_is_ignored(self) -> None:
        # The process kept running after the phrase appeared, so it did not
        # terminate because of the limit.
        tail = "\n".join(f"step {i}" for i in range(40))
        assert detect_usage_limit(f"{SESSION_LINE}\n{tail}\n") is None

    def test_invalid_json_lines_do_not_raise(self) -> None:
        assert detect_usage_limit('{"type": "error", "error": {oops') is None


class TestSanitizeLogTail:
    def test_keeps_only_the_bounded_tail_and_strips_ansi(self) -> None:
        raw = ("x" * 100_000 + "\x1b[1mtail\x1b[0m").encode()
        text = sanitize_log_tail(raw, max_bytes=64)
        assert "\x1b" not in text
        assert text.endswith("tail")
        assert len(text.encode()) <= 64

    def test_truncation_in_the_middle_of_a_multibyte_character_is_tolerated(
        self,
    ) -> None:
        raw = ("あ" * 50).encode()
        text = sanitize_log_tail(raw, max_bytes=10)
        assert set(text) <= {"あ", "�"}

    def test_invalid_utf8_is_replaced_not_raised(self) -> None:
        assert "�" in sanitize_log_tail(b"ok \xff\xfe end", max_bytes=100)


def _signal(text: str | None, kind: str = "session_limit_line") -> UsageLimitSignal:
    return UsageLimitSignal(kind=kind, reset_text=text)  # type: ignore[arg-type]


class TestResolveResetAbsolute:
    def test_iso_with_offset_is_preferred_and_converted_to_utc(self) -> None:
        now = _epoch(2026, 10, 10, 9, 0, TOKYO)
        result = resolve_reset(
            _signal("2026-10-10T13:00:00+09:00"), now=now, anchor=now, timezone=None
        )
        assert result.reset_at == _epoch(2026, 10, 10, 13, 0, TOKYO)
        assert result.timezone == "+09:00"
        assert result.known

    def test_iso_z_suffix_means_utc(self) -> None:
        now = datetime(2026, 10, 10, 0, 0, tzinfo=UTC).timestamp()
        result = resolve_reset(
            _signal("2026-10-10T04:00:00Z"), now=now, anchor=now, timezone=None
        )
        assert result.reset_at == datetime(2026, 10, 10, 4, 0, tzinfo=UTC).timestamp()
        assert result.timezone == "UTC"

    def test_epoch_seconds_are_accepted(self) -> None:
        now = 1_800_000_000.0
        result = resolve_reset(
            _signal(str(int(now) + 3600)), now=now, anchor=now, timezone=None
        )
        assert result.reset_at == now + 3600

    def test_naive_iso_is_not_implicitly_utc(self) -> None:
        now = _epoch(2026, 10, 10, 9, 0, TOKYO)
        result = resolve_reset(
            _signal("2026-10-10T13:00:00"), now=now, anchor=now, timezone=None
        )
        assert not result.known
        assert result.reset_at is None

    def test_past_absolute_reset_is_unknown(self) -> None:
        now = _epoch(2026, 10, 10, 15, 0, TOKYO)
        result = resolve_reset(
            _signal("2026-10-10T13:00:00+09:00"), now=now, anchor=now, timezone=None
        )
        assert not result.known

    def test_absurdly_far_reset_is_unknown(self) -> None:
        now = _epoch(2026, 10, 10, 9, 0, TOKYO)
        result = resolve_reset(
            _signal("2027-10-10T13:00:00+09:00"), now=now, anchor=now, timezone=None
        )
        assert not result.known


class TestResolveResetWallClock:
    def test_requires_an_explicit_timezone(self) -> None:
        now = _epoch(2026, 10, 10, 9, 0, TOKYO)
        result = resolve_reset(_signal("1pm"), now=now, anchor=now, timezone=None)
        assert not result.known
        assert "timezone" in result.reason

    def test_later_today(self) -> None:
        now = _epoch(2026, 10, 10, 9, 0, TOKYO)
        result = resolve_reset(_signal("1pm"), now=now, anchor=now, timezone=TOKYO)
        assert result.reset_at == _epoch(2026, 10, 10, 13, 0, TOKYO)
        assert result.timezone == TOKYO

    def test_rolls_over_to_the_next_day(self) -> None:
        now = _epoch(2026, 10, 10, 15, 0, TOKYO)
        result = resolve_reset(_signal("1pm"), now=now, anchor=now, timezone=TOKYO)
        assert result.reset_at == _epoch(2026, 10, 11, 13, 0, TOKYO)

    def test_rolls_over_a_month_boundary(self) -> None:
        now = _epoch(2026, 10, 31, 23, 0, TOKYO)
        result = resolve_reset(_signal("1am"), now=now, anchor=now, timezone=TOKYO)
        assert result.reset_at == _epoch(2026, 11, 1, 1, 0, TOKYO)

    @pytest.mark.parametrize(
        ("text", "hour", "minute"),
        [
            ("12am", 0, 0),
            ("12pm", 12, 0),
            ("12:30am", 0, 30),
            ("12:30pm", 12, 30),
            ("1am", 1, 0),
            ("11pm", 23, 0),
            ("1:45pm", 13, 45),
            ("13:00", 13, 0),
            ("00:15", 0, 15),
            ("23:59", 23, 59),
        ],
    )
    def test_twelve_and_twenty_four_hour_notation(
        self, text: str, hour: int, minute: int
    ) -> None:
        now = _epoch(2026, 10, 10, 0, 0, TOKYO) - 1
        result = resolve_reset(_signal(text), now=now, anchor=now, timezone=TOKYO)
        assert result.reset_at == _epoch(2026, 10, 10, hour, minute, TOKYO)

    @pytest.mark.parametrize(
        "text", ["13pm", "0pm", "25:00", "1:75pm", "soon", "tomorrow", "1"]
    )
    def test_unsupported_notation_is_unknown(self, text: str) -> None:
        now = _epoch(2026, 10, 10, 9, 0, TOKYO)
        result = resolve_reset(_signal(text), now=now, anchor=now, timezone=TOKYO)
        assert not result.known

    def test_invalid_timezone_name_is_unknown_not_utc(self) -> None:
        now = _epoch(2026, 10, 10, 9, 0, TOKYO)
        result = resolve_reset(
            _signal("1pm"), now=now, anchor=now, timezone="Mars/Olympus"
        )
        assert not result.known

    def test_nonexistent_dst_time_is_unknown(self) -> None:
        # 2026-03-08 02:30 does not exist in New York (spring forward).
        now = _epoch(2026, 3, 8, 0, 0, NEW_YORK)
        result = resolve_reset(
            _signal("2:30am"), now=now, anchor=now, timezone=NEW_YORK
        )
        assert not result.known
        assert "dst" in result.reason

    def test_ambiguous_dst_time_is_unknown(self) -> None:
        # 2026-11-01 01:30 happens twice in New York (fall back).
        now = _epoch(2026, 11, 1, 0, 0, NEW_YORK)
        result = resolve_reset(
            _signal("1:30am"), now=now, anchor=now, timezone=NEW_YORK
        )
        assert not result.known
        assert "dst" in result.reason

    def test_next_day_across_dst_uses_the_local_wall_clock(self) -> None:
        now = _epoch(2026, 11, 1, 12, 0, NEW_YORK)
        result = resolve_reset(_signal("9am"), now=now, anchor=now, timezone=NEW_YORK)
        assert result.reset_at == _epoch(2026, 11, 2, 9, 0, NEW_YORK)

    def test_wall_clock_is_anchored_to_when_the_message_was_written(self) -> None:
        # The limit message was written at 12:00 and said "resets 1pm". The
        # dispatcher only notices at 14:00, after the reset: that is a past reset.
        anchor = _epoch(2026, 10, 10, 12, 0, TOKYO)
        now = _epoch(2026, 10, 10, 14, 0, TOKYO)
        result = resolve_reset(_signal("1pm"), now=now, anchor=anchor, timezone=TOKYO)
        assert not result.known
        assert "past" in result.reason

    def test_absent_reset_text_is_unknown(self) -> None:
        now = _epoch(2026, 10, 10, 9, 0, TOKYO)
        result = resolve_reset(_signal(None), now=now, anchor=now, timezone=TOKYO)
        assert not result.known


class TestPlanUsageLimitRetry:
    def _policy(self, retries: int = 2):
        return usage_limit_policy(retries, 900)

    def test_known_reset_waits_until_reset_plus_grace(self) -> None:
        now = _epoch(2026, 10, 10, 9, 0, TOKYO)
        reset = resolve_reset(_signal("1pm"), now=now, anchor=now, timezone=TOKYO)
        plan = plan_usage_limit_retry(
            self._policy(), RetryState(), reset, grace_seconds=30, now=now
        )
        assert plan.disposition is RetryDisposition.NEW
        assert plan.state == RetryState(
            count=1, retry_at=_epoch(2026, 10, 10, 13, 0, TOKYO) + 30, pending=True
        )

    def test_unknown_reset_uses_the_finite_exponential_backoff(self) -> None:
        now = 1000.0
        unknown = resolve_reset(_signal(None), now=now, anchor=now, timezone=None)
        first = plan_usage_limit_retry(
            self._policy(), RetryState(), unknown, grace_seconds=30, now=now
        )
        assert first.state == RetryState(count=1, retry_at=now + 900, pending=True)
        second = plan_usage_limit_retry(
            self._policy(), RetryState(count=1), unknown, grace_seconds=30, now=now
        )
        assert second.state == RetryState(count=2, retry_at=now + 1800, pending=True)

    def test_exhausted_after_the_configured_extra_launches(self) -> None:
        unknown = resolve_reset(_signal(None), now=1.0, anchor=1.0, timezone=None)
        plan = plan_usage_limit_retry(
            self._policy(2), RetryState(count=2), unknown, grace_seconds=30, now=1.0
        )
        assert plan.disposition is RetryDisposition.EXHAUSTED

    def test_zero_retries_never_requeues_automatically(self) -> None:
        unknown = resolve_reset(_signal(None), now=1.0, anchor=1.0, timezone=None)
        plan = plan_usage_limit_retry(
            self._policy(0), RetryState(), unknown, grace_seconds=30, now=1.0
        )
        assert plan.disposition is RetryDisposition.EXHAUSTED

    def test_pending_reservation_is_resumed_without_counting_again(self) -> None:
        pending = RetryState(count=1, retry_at=5000.0, pending=True)
        unknown = resolve_reset(_signal(None), now=1.0, anchor=1.0, timezone=None)
        plan = plan_usage_limit_retry(
            self._policy(1), pending, unknown, grace_seconds=30, now=9999.0
        )
        assert plan.disposition is RetryDisposition.RESUME
        assert plan.state == pending
