"""Shared fact inspection helpers for consistency invariants."""

from __future__ import annotations

from orchestune.consistency.models import Observation, ScopedObservations


def _fact(scope: ScopedObservations, name: str) -> Observation | None:
    """One uniquely named fact; a duplicated name is not a usable reading."""
    matches = tuple(fact for fact in scope.facts if fact.name == name)
    return matches[0] if len(matches) == 1 else None


def _observation_details(fact: Observation) -> tuple[str, ...]:
    return (
        f"fact={fact.name}",
        f"source={fact.source}",
        f"certainty={fact.certainty.value}",
        *fact.diagnostics,
    )


fact = _fact
observation_details = _observation_details

__all__ = [
    "_fact",
    "_observation_details",
    "fact",
    "observation_details",
]
