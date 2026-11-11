"""Classify known blocked-human-review comment markers. No I/O."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

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
