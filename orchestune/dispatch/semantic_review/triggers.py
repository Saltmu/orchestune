"""Round-bound review requests of a Layer-2 attempt PR (#1362, #1436).

Pure helpers over the trigger comments of one PR: which requests belong to the
state's round and head, split by whether they were posted since the round began.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from orchestune.review.rounds import ReviewTrigger, parse_trigger
from orchestune.review.semantic_gate import SemanticReviewStateV1
from orchestune.review.snapshot import EvidenceContractError, normalize_timestamp

#: Tolerated GitHub/local clock difference when a trigger is matched to a round.
_CLOCK_SKEW = timedelta(seconds=60)


def restore_round_triggers(comments: list[dict[str, Any]]) -> list[ReviewTrigger]:
    """Every well-formed trigger in creation order.

    Rounds repeat on one PR once a human resets the limits, so the strict
    PR-wide `restore_triggers` ordering cannot apply; a round's trigger is
    told apart by its head and by being posted after the round started.
    """
    triggers = []
    for comment in comments:
        try:
            trigger = parse_trigger(comment)
        except EvidenceContractError:
            continue
        if trigger is not None:
            triggers.append(trigger)
    return sorted(triggers, key=lambda t: t.created_at)


def round_trigger(
    triggers: list[ReviewTrigger], state: SemanticReviewStateV1, *, fresh: bool
) -> ReviewTrigger | None:
    """The latest trigger of this round and head, posted since the round began
    (``fresh``) or only before it (a stale request of an earlier run)."""
    matching = round_triggers(triggers, state, fresh=fresh)
    return matching[-1] if matching else None


def round_triggers(
    triggers: list[ReviewTrigger], state: SemanticReviewStateV1, *, fresh: bool
) -> list[ReviewTrigger]:
    """Every request of this round and head (a retry repeats the first one)."""
    started = parse_iso(state.round_started_at)
    floor = (
        None
        if started is None
        else normalize_timestamp((started - _CLOCK_SKEW).isoformat())
    )
    return [
        t
        for t in triggers
        if t.round == state.round
        and t.requested_head_sha == state.head_sha
        and (floor is None or (t.created_at >= floor) == fresh)
    ]


def parse_iso(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    return parsed if parsed is None or parsed.tzinfo else None
