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

from orchestune.review.markers import (
    REPLACEMENT_LINE,
    SUPERSEDED_LINE,
    build_trigger_body,
    review_replaces_marker,
)
from orchestune.review.snapshot import (
    EvidenceContractError,
    InsufficientEvidenceError,
    RoundLimitError,
    normalize_timestamp,
    parse_utc,
)

#: The fixed review-round limit; `--max-rounds` may only lower it.
MAX_REVIEW_ROUNDS = 5


@dataclass(frozen=True)
class _FenceState:
    char: str
    length: int
    container: int


_FENCE_OPENER = re.compile(r"^(`{3,}|~{3,})(.*)$")
_FENCE_CLOSER = re.compile(r"^(`{3,}|~{3,})\s*$")
_LIST_MARKER = re.compile(r"^([-*+]|\d{1,9}[.)])(\s{1,4}|\s*$)(.*)$")
_TRIGGER_LINE = re.compile(
    r"<!--\s*orchestune:review-trigger bot=(claude|codex)\s*-->", re.I
)
_ROUND_LINE = re.compile(r"<!--\s*orchestune:review-round\s+(\d+)\s*-->", re.I)
_HEAD_LINE = re.compile(r"<!--\s*orchestune:review-head\s+([0-9a-f]{40})\s*-->", re.I)
_BARE_MENTIONS = frozenset({"@claude review", "@codex review"})


def _match_fence_opener(s: str) -> tuple[str, int] | None:
    m = _FENCE_OPENER.match(s)
    if not m:
        return None
    token, rest = m.group(1), m.group(2)
    if token[0] == "`" and "`" in rest:
        return None
    return token[0], len(token)


def _is_fence_closer(stripped: str, fence: _FenceState) -> bool:
    m = _FENCE_CLOSER.match(stripped)
    if not m:
        return False
    token = m.group(1)
    return token[0] == fence.char and len(token) >= fence.length


def _is_thematic_break(s: str) -> bool:
    cleaned = s.replace(" ", "")
    return (
        len(cleaned) >= 3
        and all(c == cleaned[0] for c in cleaned)
        and cleaned[0] in "-_*"
    )


def _match_list_marker(s: str) -> tuple[int, str] | None:
    if _is_thematic_break(s):
        return None
    m = _LIST_MARKER.match(s)
    if not m:
        return None
    marker, spaces, rest = m.group(1), m.group(2), m.group(3)
    width = len(marker) + (len(spaces) if spaces else 1)
    return width, rest


def _is_block_start(s: str) -> bool:
    return (
        s.startswith(">")
        or s.startswith("<!--")
        or _match_fence_opener(s) is not None
        or _match_list_marker(s) is not None
    )


def _step_line(
    expanded: str,
    stripped: str,
    i: int,
    items: list[int],
    paragraph: bool,
    lines: list[str],
) -> tuple[_FenceState | None, bool]:
    s = expanded[i:]
    if paragraph and not _is_block_start(s):
        pass  # lazy continuation: preserve open items
    else:
        while items and items[-1] > i:
            items.pop()

    P = items[-1] if items else 0
    rel_i = i - P

    if 0 <= rel_i <= 3:
        lm = _match_list_marker(s)
        if lm is not None:
            width, rest = lm
            content_col = i + width
            items.append(content_col)
            rest_s = rest.lstrip(" ")
            if len(rest) - len(rest_s) <= 3:
                opener = _match_fence_opener(rest_s)
                if opener is not None:
                    return _FenceState(opener[0], opener[1], content_col), False
            return None, bool(rest.strip())

        opener = _match_fence_opener(s)
        if opener is not None:
            return _FenceState(opener[0], opener[1], P), False
    elif rel_i >= 4:
        return None, False

    if i == 0 and not stripped.startswith(">"):
        lines.append(stripped)

    is_para = not (stripped.startswith(">") or stripped.startswith("<!--"))
    return None, is_para


@dataclass(frozen=True)
class ReviewTrigger:
    id: Any
    reviewer: str
    round: int
    created_at: str
    requested_head_sha: str | None
    body: str
    active: bool = True
    replaces: str | None = None
    author: str | None = None


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
    """Column-0 lines outside fenced code (also inside list items) and block quotes.

    Tracks list item indentation to determine when nested fences close.
    Remaining CommonMark approximations (out of scope): fences inside
    blockquotes in lists, lists inside blockquotes, marker-like lines inside
    raw HTML blocks, and strict column arithmetic with mixed tabs. Any
    omissions by builder output are detected prior to posting via round-trip
    verification in build_restorable_trigger_body.
    """
    lines: list[str] = []
    items: list[int] = []
    fence: _FenceState | None = None
    paragraph = False

    for raw in (body or "").splitlines():
        expanded = raw.expandtabs(4)
        stripped = expanded.strip()
        i = len(expanded) - len(expanded.lstrip(" "))

        if fence is not None:
            if fence.container > 0 and stripped and i < fence.container:
                fence = None
            else:
                rel_i = i - fence.container
                if 0 <= rel_i <= 3 and _is_fence_closer(stripped, fence):
                    fence = None
                continue

        if not stripped:
            paragraph = False
            continue

        fence, paragraph = _step_line(expanded, stripped, i, items, paragraph, lines)

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
    superseded = [
        line
        for line in lines
        if line.startswith("<!--") and "orchestune:superseded-trigger" in line.lower()
    ]
    historical = [m[1] for line in superseded if (m := SUPERSEDED_LINE.fullmatch(line))]
    replaces = _marker_values(lines, "replaces", REPLACEMENT_LINE)
    if len(historical) != len(superseded) or (bots and historical):
        raise EvidenceContractError("conflicting or malformed superseded trigger")
    if not bots and not historical:
        if replaces:
            raise EvidenceContractError("replacement metadata requires a trigger")
        return None
    active = bool(bots)
    bots = bots or historical
    rounds = _marker_values(lines, "round", _ROUND_LINE)
    heads = _marker_values(lines, "head", _HEAD_LINE)
    if len(bots) != 1 or len(rounds) != 1 or len(heads) > 1 or len(replaces) > 1:
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
        active=active,
        replaces=replaces[0] if replaces else None,
        author=str((item.get("user") or {}).get("login") or "").lower() or None,
    )


def build_restorable_trigger_body(
    reply_body: str,
    bot_name: str,
    round_num: int,
    head_sha: str,
    *,
    replaces: object = None,
) -> str:
    """Build a combined trigger comment body and verify parse_trigger can restore it.

    Raises EvidenceContractError if parse_trigger fails to recover the exact bot,
    round, and head sha (e.g. when reply_body leaves a code fence unclosed).
    """
    candidate = build_trigger_body(reply_body, bot_name, round_num, head_sha)
    if replaces is not None:
        candidate += "\n" + review_replaces_marker(replaces)
    probe_item = {
        "id": "probe",
        "body": candidate,
        "created_at": "2026-01-01T00:00:00Z",
    }
    trigger = parse_trigger(probe_item)
    if (
        trigger is None
        or trigger.reviewer != bot_name.lower()
        or trigger.round != round_num
        or trigger.requested_head_sha != head_sha.lower()
    ):
        raise EvidenceContractError(
            "trigger markers could not be restored by parse_trigger from candidate body; "
            "ensure all code fences in reply are properly closed"
        )
    return candidate


def precise_created_at(comments: list[dict[str, Any]], trigger: ReviewTrigger) -> str:
    """The trigger's creation time with fractional seconds, when the snapshot has them."""
    for item in comments:
        if str(item.get("id")) == str(trigger.id):
            return str(
                item.get("created_at_precise")
                or item.get("created_at")
                or trigger.created_at
            )
    return trigger.created_at


def activity_bounds(
    comments: list[dict[str, Any]],
    triggers: list[ReviewTrigger],
    current: ReviewTrigger,
) -> tuple[str, str]:
    """Exact [start, end) of the round for tracker activity comparisons.

    `ReviewTrigger.created_at` is rounded down to the second for string ordering,
    which would move a boundary by up to a second; the end is the next
    round's trigger, empty for the latest round.
    """
    later = [t for t in triggers if t.round > current.round]
    end = precise_created_at(comments, later[0]) if later else ""
    return precise_created_at(comments, current), end


def trigger_comment_ids(comments: Iterable[dict[str, Any]]) -> set[Any]:
    """Ids of normal comments that request a review; never review evidence.

    Deliberately broader than `parse_trigger`: a bot-authored request must not be
    read as its own review even when its markers are inconsistent.
    """
    ids: set[Any] = set()
    for item in comments:
        lines = effective_lines(item.get("body"))
        if any(
            line.startswith("<!--")
            and any(
                marker in line.lower()
                for marker in (
                    "orchestune:review-trigger",
                    "orchestune:superseded-trigger",
                    "orchestune:review-replaces",
                )
            )
            for line in lines
        ) or any(line.lower() in _BARE_MENTIONS for line in lines):
            ids.add(item.get("id"))
    ids.discard(None)
    return ids


def trigger_history(comments: Iterable[dict[str, Any]]) -> list[ReviewTrigger]:
    """Restore all records, including inactive history; validate replacement edges."""
    seen: dict[str, tuple[dict[str, Any], ReviewTrigger]] = {}
    raw_seen: dict[str, dict[str, Any]] = {}
    for item in comments:
        if item.get("id") is not None:
            key = str(item["id"])
            if key in raw_seen and raw_seen[key] != item:
                raise EvidenceContractError(f"comment id {key} has different content")
            raw_seen[key] = item
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
    records = sorted(
        (trigger for _, trigger in seen.values()),
        key=lambda t: (t.round, t.created_at),
    )
    _validate_history_order(records)
    _validate_replacements(seen)
    for record in records:
        if record.replaces is not None:
            validate_replacement_target(
                records,
                record.replaces,
                record.reviewer,
                record.requested_head_sha or "",
            )
    return records


def _validate_history_order(records: list[ReviewTrigger]) -> None:
    rounds: dict[int, list[str]] = {}
    for record in records:
        rounds.setdefault(record.round, []).append(record.created_at)
    for earlier, later in pairwise(sorted(rounds)):
        if max(rounds[earlier]) > min(rounds[later]):
            raise EvidenceContractError("round order contradicts created_at")


def _validate_replacements(
    seen: dict[str, tuple[dict[str, Any], ReviewTrigger]],
) -> None:
    children: dict[str, str] = {}
    for key, (item, new) in seen.items():
        if new.replaces is None:
            continue
        parent = seen.get(new.replaces)
        if parent is None:
            raise EvidenceContractError("replacement references a missing trigger")
        old_item, old = parent
        if new.replaces in children:
            raise EvidenceContractError("replacement chain branches")
        children[new.replaces] = key
        if old.round != new.round or not old.author or old.author != new.author:
            raise EvidenceContractError(
                "replacement must have the same round and author"
            )
        if not old.requested_head_sha or not new.requested_head_sha:
            raise EvidenceContractError("replacement requires both head markers")
        if (old.reviewer, old.requested_head_sha) == (
            new.reviewer,
            new.requested_head_sha,
        ):
            raise EvidenceContractError("same reviewer/head must resume, not replace")
        before = parse_utc(
            old_item.get("created_at_precise") or old_item["created_at"], "created_at"
        )
        after = parse_utc(
            item.get("created_at_precise") or item["created_at"], "created_at"
        )
        same_second_order = (
            str(old.id).isdigit()
            and str(new.id).isdigit()
            and int(old.id) < int(new.id)
        )
        if after < before or (after == before and not same_second_order):
            raise EvidenceContractError(
                "replacement creation order is invalid (or cyclic)"
            )
        _validate_same_pr(old_item, item)


def _validate_same_pr(old: dict[str, Any], new: dict[str, Any]) -> None:
    pattern = re.compile(
        r"github\.com/(?:repos/)?([^/]+/[^/]+)/(?:issues|pulls?)/(\d+)", re.I
    )
    identities = set()
    for item in (old, new):
        for field in ("html_url", "url", "issue_url", "pull_request_url"):
            match = pattern.search(str(item.get(field) or ""))
            if match:
                identities.add((match[1].lower(), match[2]))
    if len(identities) > 1:
        raise EvidenceContractError("replacement references another PR")


def consumed_round(comments: Iterable[dict[str, Any]]) -> int:
    return max((t.round for t in trigger_history(comments)), default=0)


def legacy_trigger_timestamp(comments: list[dict[str, Any]], bot: str) -> str:
    """Legacy acquisition only: bare mentions cannot prove a round or HEAD.

    Never use this path when structured trigger history exists. It preserves
    read-only access to old requests, without granting completion evidence.
    """
    if trigger_history(comments):
        return ""
    return max(
        (
            normalize_timestamp(item.get("created_at")) or ""
            for item in comments
            if f"@{bot.lower()} review"
            in [line.lower() for line in effective_lines(item.get("body"))]
        ),
        default="",
    )


def validate_replacement_target(
    records: Sequence[ReviewTrigger], source_id: object, bot: str, head: str
) -> None:
    """Reusing any ancestor's execution identity cannot be distinguished safely."""
    by_id = {str(t.id): t for t in records}
    key: str | None = str(source_id)
    visited: set[str] = set()
    while key is not None:
        if key in visited or key not in by_id:
            raise EvidenceContractError("replacement chain is cyclic or incomplete")
        visited.add(key)
        old = by_id[key]
        if (old.reviewer, old.requested_head_sha) == (bot, head):
            raise EvidenceContractError(
                "same reviewer/head execution must resume, not repost"
            )
        key = old.replaces


def bind_replacement_results(
    comments: list[dict[str, Any]], trigger_id: object, result: dict[str, Any]
) -> bool:
    """Replacement results require explicit commit binding, never time alone.

    A late old execution may finish after the replacement. Keep mismatched
    commits as history and unbound comments as unassociated, rather than promoting
    either to evidence for the new execution.
    """
    if trigger_id is None:
        return True
    current = next(
        (t for t in restore_triggers(comments) if str(t.id) == str(trigger_id)), None
    )
    if current is None or current.replaces is None:
        return True
    items = result.get("review_items", []) + result.get("inline_comments", [])
    uncertain = False
    for item in items:
        if item.get("provenance") != "current":
            continue
        commit = item.get("commit_id")
        if not commit:
            item.update(
                provenance="unassociated",
                provenance_reason="replacement_execution_unproven",
            )
            uncertain = True
        elif str(commit).lower() != current.requested_head_sha:
            item.update(provenance="historical", provenance_reason="superseded_head")
    bodies = [
        i.get("body", "")
        for i in result.get("review_items", [])
        if i.get("provenance") == "current"
    ]
    result["review_body"] = "\n\n---\n\n".join(bodies)
    proven = any(i.get("provenance") == "current" and i.get("body") for i in items)
    if uncertain or not proven:
        result.update(
            acquisition_status="unavailable",
            reason="replacement execution cannot be proven from timestamps alone",
        )
        return False
    return True


def restore_triggers(comments: Iterable[dict[str, Any]]) -> list[ReviewTrigger]:
    """Only unique active chain tails; unrelated duplicate rounds remain invalid."""
    records = trigger_history(comments)
    replaced = {t.replaces for t in records if t.replaces is not None}
    triggers = [t for t in records if t.active and str(t.id) not in replaced]
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
    max_rounds: int = MAX_REVIEW_ROUNDS,
) -> ReviewRoundContext:
    """The posted round to evaluate: the PR-wide latest unless one is requested."""
    max_rounds = min(max_rounds, MAX_REVIEW_ROUNDS)
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
    max_rounds: int = MAX_REVIEW_ROUNDS,
    explicit_round: int | None = None,
    consumed_round_num: int | None = None,
) -> int:
    """The round a new request would open; the limit counts the whole PR."""
    max_rounds = min(max_rounds, MAX_REVIEW_ROUNDS)
    latest = triggers[-1] if triggers else None
    next_round = max(latest.round if latest else 0, consumed_round_num or 0) + 1
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


def previous_round_window(
    comments: list[dict[str, Any]], next_round: int, *, strict: bool
) -> PreviousRoundWindow:
    """Boundaries of round `next_round - 1`, the round a reply table must judge.

    Both transports use the same reader. `strict` remains a compatibility
    argument; it no longer permits ambiguous trigger selection.
    """
    triggers = restore_triggers(comments)
    previous = [t for t in triggers if t.round == next_round - 1]
    if not previous:
        raise EvidenceContractError("previous round trigger is missing")
    later = [t.created_at for t in trigger_history(comments) if t.round >= next_round]
    return PreviousRoundWindow(
        previous_round=next_round - 1,
        reviewer=previous[0].reviewer,
        started_at=previous[0].created_at,
        ended_at=min(later) if later else None,
        exclude_ids=frozenset(trigger_comment_ids(comments)),
    )
