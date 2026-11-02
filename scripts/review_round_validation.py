"""Online round-selection checks. Re-exported from `scripts.wait_for_review`."""

from __future__ import annotations

from typing import Any

from orchestune.review.judgment import validate_previous_round_reply
from orchestune.review.rounds import (
    MAX_REVIEW_ROUNDS,
    previous_round_window,
    restore_triggers,
)


class MaxRoundsExceededError(RuntimeError):
    """Raised when the maximum number of review rounds is exceeded."""


def _validate_review_selection(
    data: dict[str, Any], bot_name: str, switch: bool
) -> None:
    if bot_name not in {"claude", "codex", "skip"}:
        raise ValueError("reviewer must be claude, codex or skip")
    triggers = restore_triggers(data.get("issue_comments", []))
    if not triggers:
        return
    reviewer = triggers[-1].reviewer
    if reviewer != bot_name and not switch:
        raise ValueError(
            f"previous reviewer is {reviewer}; explicit --switch-reviewer required"
        )


def _validate_review_reply(
    data: dict[str, Any], bot_name: str, round_num: int, body_file: str | None
) -> None:
    if round_num < 2:
        return
    if not body_file:
        raise ValueError("round 2+ requires --body-file with review judgments")
    with open(body_file, encoding="utf-8") as stream:
        reply = stream.read()
    # Legacy-tolerant reading of earlier triggers; the offline path is strict.
    window = previous_round_window(
        data.get("issue_comments", []), round_num, strict=False
    )
    validate_previous_round_reply(data, window, reply)


def _check_round_limit(current_round: int, max_rounds: int) -> None:
    effective_limit = min(max_rounds, MAX_REVIEW_ROUNDS)
    if current_round > effective_limit:
        raise MaxRoundsExceededError(
            f"Maximum review rounds ({effective_limit}) exceeded (attempted round {current_round})."
        )


def _validate_resume_reply(data, current_round, existing, body_file):
    if current_round < 2:
        return
    window = previous_round_window(data["issue_comments"], current_round, strict=True)
    try:
        posted = validate_previous_round_reply(data, window, existing.get("body") or "")
        if body_file:
            with open(body_file, encoding="utf-8") as stream:
                given = validate_previous_round_reply(data, window, stream.read())
            if given.judgment_digest != posted.judgment_digest:
                raise ValueError("supplied table differs from the posted trigger table")
    except ValueError as error:
        raise ValueError(
            "posted judgments are invalid or differ from --body-file; use a validated next round"
        ) from error


def _validate_online_operation(
    bot_name, post_trigger, repair_trigger_markers, round_num
):
    if not post_trigger and bot_name == "skip":
        raise ValueError("--no-post is read-only and cannot publish a skip selection")
    if repair_trigger_markers and not post_trigger:
        raise ValueError("--no-post is read-only; marker repair requires posting mode")
    if repair_trigger_markers and (round_num is None or bot_name == "skip"):
        raise ValueError(
            "marker repair requires an explicit posted --round and reviewer"
        )
