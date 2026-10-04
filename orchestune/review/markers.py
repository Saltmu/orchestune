"""Machine-readable PR review markers (no network or filesystem access)."""

from __future__ import annotations

import re
from typing import Any


def normalize_sha(value: object) -> str | None:
    """Lowercase 40-hex commit id, or None for anything else (never a guess)."""
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40}", value):
        return value.lower()
    return None


def _sha(value: str) -> str:
    sha = normalize_sha(value)
    if sha is None:
        raise ValueError("review head must be a 40-character SHA")
    return sha


def review_trigger_marker(bot_name: str) -> str:
    return f"<!-- orchestune:review-trigger bot={bot_name.lower()} -->"


def review_reply_marker() -> str:
    return "<!-- orchestune:review-reply -->"


def is_review_reply(body: str | None) -> bool:
    """True when the first non-blank line declares a review-reply comment.

    A declaration of comment kind, not an authenticated identity: mentions in the
    body, quotes, code spans and fences are never treated as the declaration.
    """
    for line in (body or "").splitlines():
        if line.strip():
            return line.strip() == review_reply_marker()
    return False


def review_round_marker(round_num: int) -> str:
    return f"<!-- orchestune:review-round {round_num} -->"


def parse_round_marker(body: str) -> int | None:
    match = re.search(r"<!--\s*orchestune:review-round\s+(\d+)\s*-->", body, re.I)
    return int(match[1]) if match else None


def review_head_marker(head_sha: str) -> str:
    return f"<!-- orchestune:review-head {_sha(head_sha)} -->"


def parse_head_marker(body: str) -> str | None:
    matches = re.findall(
        r"<!--\s*orchestune:review-head\s+([0-9a-f]{40})\s*-->", body, re.I
    )
    return matches[0].lower() if len(matches) == 1 else None


def parse_trigger_reviewer(body: str) -> str | None:
    match = re.search(
        r"<!--\s*orchestune:review-trigger bot=(claude|codex)\s*-->", body, re.I
    )
    return match[1].lower() if match else None


def has_review_trigger_mention(body: str, bot_name: str) -> bool:
    body_lower = body.lower()
    bot = bot_name.lower()
    if bot == "claude":
        return "@claude" in body_lower and "review" in body_lower
    pattern = re.compile(rf"@(?:{re.escape(bot_name)})[,\s:]+review\b", re.IGNORECASE)
    return pattern.search(body) is not None


def ensure_review_trigger_mention(body: str, bot_name: str) -> str:
    trimmed = body.strip()
    if not trimmed:
        return f"@{bot_name} review"
    if has_review_trigger_mention(trimmed, bot_name):
        return trimmed
    return f"@{bot_name} review\n\n{trimmed}"


def mark_review_trigger(body: str, bot_name: str, round_num: int | None = None) -> str:
    result = body
    trigger_marker = review_trigger_marker(bot_name)
    if trigger_marker not in result.lower():
        result = f"{result.rstrip()}\n\n{trigger_marker}"
    if round_num is not None:
        round_marker = review_round_marker(round_num)
        if round_marker not in result.lower():
            result = f"{result.rstrip()}\n{round_marker}"
    return result


def build_trigger_body(
    reply_body: str, bot_name: str, round_num: int, head_sha: str
) -> str:
    """One combined normal comment: reply, mention, then bot/round/head markers.

    Stale trigger markers inside the reply are dropped; the reply marker is kept.
    """
    head_marker = review_head_marker(head_sha)
    raw = re.sub(
        r"<!--\s*orchestune:review-(?:head|round|trigger)\b.*?-->",
        "",
        reply_body,
        flags=re.I,
    )
    body = ensure_review_trigger_mention(raw, bot_name)
    return f"{mark_review_trigger(body, bot_name, round_num)}\n{head_marker}"


def review_selection_marker(reviewer: str, head_sha: str) -> str:
    if reviewer not in {"claude", "codex", "skip"}:
        raise ValueError("reviewer must be claude, codex or skip")
    return f"<!-- orchestune:review-selection reviewer={reviewer} head={_sha(head_sha)} -->"


def parse_selection_marker(body: str) -> tuple[str, str] | None:
    match = re.search(
        r"<!--\s*orchestune:review-selection reviewer=(claude|codex|skip) head=([0-9a-f]{40})\s*-->",
        body,
        re.I,
    )
    return (match[1].lower(), match[2].lower()) if match else None


def derive_review_target(
    items: list[dict[str, Any]], requested: str | None, current: str | None
) -> tuple[str | None, str]:
    """Prefer current-round review commits; verify comment-only trigger heads.

    A malformed or conflicting review commit is evidence that the target cannot
    be named: it never lets the trigger head stand in for it.
    """
    current_items = [item for item in items if item.get("provenance") == "current"]
    raw = [
        item["commit_id"]
        for item in current_items
        if item.get("kind") == "review" and item.get("commit_id")
    ]
    if raw:
        commits = {normalize_sha(commit) for commit in raw}
        if len(commits) == 1 and None not in commits:
            return next(iter(commits)), "review_commit"
        return None, "unknown"
    requested_sha, current_sha = normalize_sha(requested), normalize_sha(current)
    if (
        current_items
        and all(item.get("kind") == "issue_comment" for item in current_items)
        and requested_sha
        and requested_sha == current_sha
    ):
        return requested_sha, "trigger_head_verified"
    return None, "unknown"
