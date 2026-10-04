"""Restore posted review triggers and select a round (no network or filesystem).

A trigger is recovered only from real marker lines of a normal PR comment: markers
inside code fences, quotes, indented code or inline prose are never a request. The
mention itself is the workflow's concern and is not required for restoration.
Ambiguity (one id with two bodies, two triggers for one round, round order that
contradicts creation time) is rejected instead of resolved by array order.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from orchestune.review.markers import parse_round_marker, parse_trigger_reviewer
from orchestune.review.snapshot import (
    EvidenceContractError,
    InsufficientEvidenceError,
    RoundLimitError,
    normalize_timestamp,
)

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
_TRIGGER_LINE = re.compile(
    r"<!--\s*orchestune:review-trigger bot=(claude|codex)\s*-->", re.I
)
_ROUND_LINE = re.compile(r"<!--\s*orchestune:review-round\s+(\d+)\s*-->", re.I)
_HEAD_LINE = re.compile(r"<!--\s*orchestune:review-head\s+([0-9a-f]{40})\s*-->", re.I)
_BARE_MENTIONS = frozenset({"@claude review", "@codex review"})


@dataclass(frozen=True)
class ReviewTrigger:
    id: Any
    reviewer: str
    round: int
    created_at: str
    requested_head_sha: str | None
    body: str


@dataclass(frozen=True)
class ReviewRoundContext:
    repository: str
    pr_number: int
    reviewer: str
    round: int
    trigger_id: Any
    started_at: str
    ended_at: str | None
    requested_head_sha: str | None
    is_latest: bool


@dataclass(frozen=True)
class PreviousRoundWindow:
    previous_round: int
    reviewer: str
    started_at: str
    ended_at: str | None
    exclude_ids: frozenset[Any]


def effective_lines(body: str | None) -> list[str]:
    """Stripped lines outside fenced code, block quotes and indented code.

    A fence closes only on the same character, at least as long as the opener and
    without an info string (CommonMark); anything else inside it stays content. A
    backtick opener whose info string has a backtick is not a fence at all.
    """
    lines: list[str] = []
    fence: str | None = None
    for raw in (body or "").splitlines():
        opening = _FENCE.match(raw)
        if opening:
            token, rest = opening[1], opening[2]
            if fence is None:
                if token[0] != "`" or "`" not in rest:  # a backtick info string
                    fence = token  # makes the line plain text, not a fence opener
                    continue
            elif token[0] == fence[0] and len(token) >= len(fence) and not rest.strip():
                fence = None
                continue
        stripped = raw.strip()
        if fence or stripped.startswith(">") or raw.startswith(("    ", "\t")):
            continue
        lines.append(stripped)
    return lines


def _marker_values(lines: list[str], name: str, strict: re.Pattern[str]) -> list[str]:
    """Strict matches of a standalone marker line; a malformed one is an error."""
    loose = [
        line
        for line in lines
        if line.startswith("<!--") and f"orchestune:review-{name}" in line.lower()
    ]
    values = [m[1] for line in loose if (m := strict.fullmatch(line))]
    if len(values) != len(loose):
        raise EvidenceContractError(f"malformed review-{name} marker line")
    return values


def parse_trigger(item: dict[str, Any]) -> ReviewTrigger | None:
    """The trigger a normal comment declares, None when it declares none."""
    body = str(item.get("body") or "")
    lines = effective_lines(body)
    bots = _marker_values(lines, "trigger", _TRIGGER_LINE)
    if not bots:
        return None
    rounds = _marker_values(lines, "round", _ROUND_LINE)
    heads = _marker_values(lines, "head", _HEAD_LINE)
    if len(bots) != 1 or len(rounds) != 1 or len(heads) > 1:
        raise EvidenceContractError(
            f"trigger {item.get('id')!r} needs exactly one trigger and round marker "
            "and at most one head marker"
        )
    if item.get("id") is None:
        raise EvidenceContractError("trigger comment lacks an id")
    created = normalize_timestamp(item.get("created_at"))
    if created is None:
        raise EvidenceContractError(
            f"trigger {item['id']!r} lacks a valid created_at timestamp"
        )
    if int(rounds[0]) < 1:
        raise EvidenceContractError("review round must be positive")
    return ReviewTrigger(
        id=item["id"],
        reviewer=bots[0].lower(),
        round=int(rounds[0]),
        created_at=created,
        requested_head_sha=heads[0].lower() if heads else None,
        body=body,
    )


def trigger_comment_ids(comments: Iterable[dict[str, Any]]) -> set[Any]:
    """Ids of normal comments that request a review; never review evidence.

    Deliberately broader than `parse_trigger`: a bot-authored request must not be
    read as its own review even when its markers are inconsistent.
    """
    ids: set[Any] = set()
    for item in comments:
        lines = effective_lines(item.get("body"))
        if any(
            line.startswith("<!--") and "orchestune:review-trigger" in line.lower()
            for line in lines
        ) or any(line.lower() in _BARE_MENTIONS for line in lines):
            ids.add(item.get("id"))
    ids.discard(None)
    return ids


def restore_triggers(comments: Iterable[dict[str, Any]]) -> list[ReviewTrigger]:
    """All posted triggers ordered by round, or an error when they are ambiguous."""
    seen: dict[str, tuple[dict[str, Any], ReviewTrigger]] = {}
    for item in comments:
        trigger = parse_trigger(item)
        if trigger is None:
            continue
        known = seen.get(str(trigger.id))
        if known is None:
            seen[str(trigger.id)] = (item, trigger)
        elif known[0] != item:
            raise EvidenceContractError(
                f"trigger id {trigger.id!r} appears twice with different content"
            )
    triggers = sorted(
        (trigger for _, trigger in seen.values()),
        key=lambda t: (t.round, t.created_at),
    )
    for earlier, later in pairwise(triggers):
        if earlier.round == later.round:
            raise EvidenceContractError(
                f"round {earlier.round} has multiple triggers; refusing to choose"
            )
        if later.created_at < earlier.created_at:
            raise EvidenceContractError(
                f"round {later.round} trigger predates round {earlier.round}: "
                "round order contradicts created_at"
            )
    return triggers


def select_round(
    triggers: Sequence[ReviewTrigger],
    *,
    repository: str,
    pr_number: int,
    requested_round: int | None = None,
    max_rounds: int = 5,
) -> ReviewRoundContext:
    """The posted round to evaluate: the PR-wide latest unless one is requested."""
    if not triggers:
        raise InsufficientEvidenceError(
            "no review trigger comment found in the snapshot; nothing to evaluate"
        )
    if requested_round is not None and requested_round > max_rounds:
        raise RoundLimitError(
            f"round {requested_round} exceeds the limit of {max_rounds} rounds"
        )
    latest = triggers[-1]
    chosen = latest
    if requested_round is not None:
        found = [t for t in triggers if t.round == requested_round]
        if not found:
            raise EvidenceContractError(
                f"round {requested_round} has no posted trigger "
                f"(latest posted round is {latest.round})"
            )
        chosen = found[0]
    if chosen.round > max_rounds:
        raise RoundLimitError(
            f"round {chosen.round} exceeds the limit of {max_rounds} rounds"
        )
    later = [t for t in triggers if t.round > chosen.round]
    return ReviewRoundContext(
        repository=repository,
        pr_number=pr_number,
        reviewer=chosen.reviewer,
        round=chosen.round,
        trigger_id=chosen.id,
        started_at=chosen.created_at,
        ended_at=later[0].created_at if later else None,
        requested_head_sha=chosen.requested_head_sha,
        is_latest=not later,
    )


def plan_next_round(
    triggers: Sequence[ReviewTrigger],
    *,
    bot: str,
    switch_reviewer: bool = False,
    max_rounds: int = 5,
    explicit_round: int | None = None,
) -> int:
    """The round a new request would open; the limit counts the whole PR."""
    latest = triggers[-1] if triggers else None
    next_round = (latest.round if latest else 0) + 1
    if explicit_round is not None and explicit_round != next_round:
        raise EvidenceContractError(
            f"round {explicit_round} is not the next round {next_round}; "
            "rounds cannot be skipped or repeated"
        )
    if next_round > max_rounds:
        raise RoundLimitError(
            f"round {next_round} would exceed the limit of {max_rounds} rounds"
        )
    if latest and latest.reviewer != bot and not switch_reviewer:
        raise EvidenceContractError(
            f"previous reviewer is {latest.reviewer}; explicit --switch-reviewer required"
        )
    return next_round


def _lenient_window(
    comments: list[dict[str, Any]], next_round: int
) -> PreviousRoundWindow:
    """The historical online reading: tolerant of legacy and repeated triggers."""
    previous_round = next_round - 1
    previous_candidates = [
        item
        for item in comments
        if parse_round_marker(item.get("body") or "") == previous_round
        and parse_trigger_reviewer(item.get("body") or "")
    ]
    if not previous_candidates:
        raise ValueError("previous round trigger is missing")
    previous = max(previous_candidates, key=lambda i: str(i.get("created_at") or ""))
    if not previous.get("created_at"):
        raise ValueError("previous round trigger timestamp is missing")
    triggers = [i for i in comments if parse_trigger_reviewer(i.get("body") or "")]
    next_times = [
        str(i["created_at"])
        for i in triggers
        if (parse_round_marker(i.get("body") or "") or 0) >= next_round
        and i.get("created_at")
    ]
    return PreviousRoundWindow(
        previous_round=previous_round,
        reviewer=parse_trigger_reviewer(previous.get("body") or "") or "",
        started_at=str(previous["created_at"]),
        ended_at=min(next_times) if next_times else None,
        exclude_ids=frozenset(i["id"] for i in triggers if i.get("id") is not None),
    )


def previous_round_window(
    comments: list[dict[str, Any]], next_round: int, *, strict: bool
) -> PreviousRoundWindow:
    """Boundaries of round `next_round - 1`, the round a reply table must judge.

    `strict` (offline) restores triggers with the full ambiguity checks;
    non-strict keeps the online reading that tolerates legacy triggers.
    """
    if not strict:
        return _lenient_window(comments, next_round)
    triggers = restore_triggers(comments)
    previous = [t for t in triggers if t.round == next_round - 1]
    if not previous:
        raise EvidenceContractError("previous round trigger is missing")
    later = [t.created_at for t in triggers if t.round >= next_round]
    return PreviousRoundWindow(
        previous_round=next_round - 1,
        reviewer=previous[0].reviewer,
        started_at=previous[0].created_at,
        ended_at=min(later) if later else None,
        exclude_ids=frozenset(trigger_comment_ids(comments)),
    )
