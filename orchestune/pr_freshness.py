"""PR freshness: whether a PR can be evidence for the current execution (#1306).

Dispatch targets and GC both ask this question about the same candidate PRs, so
the answer lives here once. It depends on nothing but the standard library: no
Forge, no PR/ledger models, no I/O.
"""

from __future__ import annotations

import math
from datetime import datetime


def parse_github_timestamp(value: str) -> float | None:
    """Return the epoch seconds of an ISO-8601 timestamp, or None if unusable.

    A timestamp without a UTC offset is unusable: reading it would depend on the
    host's local timezone.
    """
    if not value:
        return None
    if value.endswith("Z"):
        value = value.removesuffix("Z") + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def should_exclude_pr(*, created_at: str, started_at: float | None) -> bool:
    """Whether the PR cannot serve as evidence for the execution started at ``started_at``.

    True means "drop it from the candidates" (created before the start, or its
    generation cannot be confirmed); it never decides completion by itself. The
    PR state and ``closed_at`` are deliberately not inputs, so every state is judged
    by the same creation time.

    - ``started_at`` unset (recovered entries): keep, without parsing anything.
    - ``started_at`` not finite: exclude; no generation can be proved against it.
    - ``created_at`` unusable: exclude (fail closed).
    - GitHub truncates ``created_at`` to whole seconds while ``started_at`` carries
      fractions, so the start is floored: a PR created in the start second is kept.
    """
    if started_at is None:
        return False
    if not math.isfinite(started_at):
        return True
    created = parse_github_timestamp(created_at)
    return created is None or created < math.floor(started_at)
