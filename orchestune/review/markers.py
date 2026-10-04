"""Machine-readable PR review markers (no network or filesystem access)."""

from __future__ import annotations

import re
from typing import Any


def _sha(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise ValueError("review head must be a 40-character SHA")
    return value.lower()


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
    """Prefer current-round review commits; verify comment-only trigger heads."""
    current_items = [item for item in items if item.get("provenance") == "current"]
    commits = {
        item["commit_id"]
        for item in current_items
        if item.get("kind") == "review" and item.get("commit_id")
    }
    if len(commits) == 1:
        return next(iter(commits)), "review_commit"
    if (
        not commits
        and current_items
        and all(item.get("kind") == "issue_comment" for item in current_items)
        and requested
        and requested == current
    ):
        return requested, "trigger_head_verified"
    return None, "unknown"
