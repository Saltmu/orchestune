"""Classify known blocked-human-review comment markers. No I/O."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from orchestune.review.rounds import effective_lines

CATEGORIES: tuple[str, ...] = (
    "child-review-gate",
    "semantic-review",
    "token-limit",
    "finalization-escalation",
    "final-acceptance-review",
    "outcome",
    "unrecognized",
)

_MARKERS: dict[str, tuple[str, ...]] = {
    "child-review-gate": ("orchestune:child-review-gate",),
    "semantic-review": (
        "orchestune:semantic-review-state:v1",
        "orchestune:semantic-review-override:v1",
        "orchestune:semantic-review-retry:v1",
    ),
    "token-limit": ("orchestune:completion-token-limit",),
    "finalization-escalation": ("orchestune:child-branch-finalization-escalation:v1",),
    "final-acceptance-review": ("orchestune:final-acceptance-review:v1",),
    "outcome": ("orchestune:outcome",),
}


def _pattern(name: str) -> re.Pattern[str]:
    """A column-0 comment whose marker name ends at whitespace or ``-->``."""
    return re.compile(rf"^<!--\s*{re.escape(name)}(?:\s.*)?\s*-->$")


_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    category: tuple(_pattern(name) for name in names)
    for category, names in _MARKERS.items()
}


def classify_comment(body: str) -> frozenset[str]:
    """Categories present on independent top-level marker lines."""
    found: set[str] = set()
    for line in effective_lines(body):
        for category, patterns in _PATTERNS.items():
            if category in found:
                continue
            if any(pattern.fullmatch(line) for pattern in patterns):
                found.add(category)
    if not found:
        return frozenset({"unrecognized"})
    return frozenset(found)


def classify_comments(bodies: Sequence[str]) -> Mapping[str, int]:
    """Count comments per category. One comment increments a category once."""
    counts = {category: 0 for category in CATEGORIES}
    for body in bodies:
        for category in classify_comment(body):
            counts[category] += 1
    return counts


_VIEW = ("gh", "issue", "view")
_NOTES: dict[str, str] = {
    "child-review-gate": "コメントの child-review-gate を確認する。",
    "semantic-review": (
        "コメントから PR 番号を確認し、orchestune review status --pr へ進む。"
    ),
    "token-limit": "token-limit のため preview の recover のみ。--apply は付けない。",
    "finalization-escalation": "コメントの finalization escalation を確認する。",
    "final-acceptance-review": "コメントの final-acceptance-review を確認する。",
    "outcome": "コメントの Outcome を確認する。",
    "unrecognized": "unrecognized コメントを確認する。",
}
_NO_COMMENTS = "コメントがないため、Issue を開いて停止理由を確認する。"
_NO_MARKERS = "既知マーカーがないため、コメントを確認する。"


@dataclass(frozen=True)
class BlockedIssueTriage:
    """One blocked issue and a fully resolved next command. No comment text."""

    issue_number: int
    title: str
    counts: Mapping[str, int]
    has_comments: bool
    recommended_command: tuple[str, ...]
    recommended_note: str


def recommended_action(
    issue_number: int,
    counts: Mapping[str, int],
    *,
    has_comments: bool = True,
) -> tuple[tuple[str, ...], str]:
    """Return a resolved argv and a category note. Never include comment text."""
    if counts.get("token-limit", 0) > 0:
        preview = ("orchestune", "recover", "--issue", str(issue_number))
        return preview, _NOTES["token-limit"]
    view = (*_VIEW, str(issue_number), "--comments")
    return view, _note(counts, has_comments=has_comments)


def build_triage_entry(
    issue_number: int,
    title: str,
    counts: Mapping[str, int],
    *,
    has_comments: bool = True,
) -> BlockedIssueTriage:
    """Copy one issue row. The stored counts do not alias the caller's mapping."""
    command, note = recommended_action(issue_number, counts, has_comments=has_comments)
    return BlockedIssueTriage(
        issue_number=issue_number,
        title=title,
        counts=MappingProxyType(dict(counts)),
        has_comments=has_comments,
        recommended_command=command,
        recommended_note=note,
    )


def _note(counts: Mapping[str, int], *, has_comments: bool) -> str:
    if not has_comments:
        return _NO_COMMENTS
    for category in CATEGORIES:
        if category != "token-limit" and counts.get(category, 0) > 0:
            return _NOTES[category]
    return _NO_MARKERS
