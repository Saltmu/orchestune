"""Issue and parent scope observation readers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from orchestune.consistency.models import (
    ConsistencyScope,
    FactValue,
    Observation,
    ScopedObservations,
)
from orchestune.consistency.observation_readings import (
    _UNKNOWN,
    FIELD_LABELS,
    FIELD_PARENT,
    FIELD_STATE,
    SOURCE_COLLECTOR,
    SOURCE_FORGE,
    _emitter,
    _ForgeReading,
    _Index,
    _issue_labels,
    _issue_state,
    _issue_status_labels,
    _IssueView,
    _parent_number,
    _Reading,
    _scope,
    _unknown_forge_reading,
    forge_reading,
)
from orchestune.consistency.vocabulary import (
    FACT_CHILD_ISSUE_NUMBERS,
    FACT_ISSUE_LABELS,
    FACT_ISSUE_NUMBER,
    FACT_ISSUE_STATE,
    FACT_ISSUE_STATUS_LABELS,
    FACT_PARENT_ISSUE_NUMBER,
    FACT_PARENT_STATE,
)
from orchestune.models import IssueRecord

_FILTERED_CHILDREN = (
    "the reused Issue snapshot is filtered, so these are the children observed "
    "in it, not necessarily every child"
)


def _missing_from_snapshot(issue_number: int) -> str:
    return f"issue #{issue_number} is not present in the reused Forge snapshot"


def _conflicting_versions(issue_number: int, view: _IssueView) -> str:
    return (
        f"the reused Forge snapshot holds {len(view.records)} versions of issue "
        f"#{issue_number} that disagree on {', '.join(view.conflicts)}"
    )


# ---------------------------------------------------------------------------
# Parent scope readings
# ---------------------------------------------------------------------------


def _children_reading(parent: int, reading: _ForgeReading, index: _Index) -> _Reading:
    """Report the observed children, and whether they are all of them.

    The subset is kept as the value even when uncertain — it is the
    evidence a diagnosis needs — but a filtered snapshot must not let a
    parent invariant conclude that the children it cannot see are gone.
    """
    children = index.children_by_parent[parent]
    if reading.snapshot is not None and not reading.snapshot.issues_complete:
        return _unknown_forge_reading(
            reading,
            (_FILTERED_CHILDREN,),
            children,
        )
    return forge_reading(reading, children)


def _parent_state_reading(
    parent: int, reading: _ForgeReading, index: _Index
) -> _Reading:
    """Prefer the parent's own record; fall back to what a child declares."""
    view = index.issues.get(parent)
    if view is not None:
        if not view.agrees_on(FIELD_STATE):
            return _unknown_forge_reading(
                reading,
                (_conflicting_versions(parent, view),),
            )
        return forge_reading(reading, view.records[0].state)
    declared = index.declared_parent_states.get(parent, ())
    if len(declared) > 1:
        candidates = ", ".join(repr(state) for state in declared)
        return _unknown_forge_reading(
            reading,
            (
                f"ambiguous parent state for issue {parent}: "
                f"children declare {candidates}",
            ),
        )
    if declared:
        return forge_reading(reading, declared[0])
    return _unknown_forge_reading(
        reading,
        (_missing_from_snapshot(parent),),
    )


def parent_scopes(
    reading: _ForgeReading, index: _Index, now: datetime
) -> list[ScopedObservations]:
    identity = _emitter(SOURCE_COLLECTOR, now)
    forge = _emitter(SOURCE_FORGE, reading.observed_at)
    return [
        _scope(
            ConsistencyScope.PARENT,
            str(parent),
            [
                identity(FACT_PARENT_ISSUE_NUMBER, _Reading(parent)),
                forge(
                    FACT_CHILD_ISSUE_NUMBERS,
                    _children_reading(parent, reading, index),
                ),
                forge(
                    FACT_PARENT_STATE,
                    _parent_state_reading(parent, reading, index),
                ),
            ],
        )
        for parent in sorted(index.children_by_parent)
    ]


# ---------------------------------------------------------------------------
# Issue readings
# ---------------------------------------------------------------------------


def _issue_reading(
    issue_number: int,
    view: _IssueView | None,
    reading: _ForgeReading,
    field: str,
    value_of: Callable[[IssueRecord], FactValue],
) -> _Reading:
    """Read one Issue field, per field, so a conflict clouds only itself."""
    if reading.snapshot is None:
        return _Reading(None, _UNKNOWN, reading.diagnostics)
    if view is None:
        if reading.snapshot.issues_complete:
            return forge_reading(reading, None)
        return _unknown_forge_reading(
            reading,
            (_missing_from_snapshot(issue_number),),
        )
    if not view.agrees_on(field):
        return _unknown_forge_reading(
            reading,
            (_conflicting_versions(issue_number, view),),
        )
    return _Reading(value_of(view.records[0]), reading.certainty, reading.diagnostics)


def issue_observations(
    issue_number: int, reading: _ForgeReading, index: _Index, now: datetime
) -> list[Observation]:
    view = index.issues.get(issue_number)
    identity = _emitter(SOURCE_COLLECTOR, now)
    forge = _emitter(SOURCE_FORGE, reading.observed_at)
    return [
        identity(FACT_ISSUE_NUMBER, _Reading(issue_number)),
        *(
            forge(
                name,
                _issue_reading(issue_number, view, reading, field, value_of),
            )
            for name, field, value_of in (
                (FACT_ISSUE_STATE, FIELD_STATE, _issue_state),
                (FACT_ISSUE_LABELS, FIELD_LABELS, _issue_labels),
                (FACT_ISSUE_STATUS_LABELS, FIELD_LABELS, _issue_status_labels),
                (FACT_PARENT_ISSUE_NUMBER, FIELD_PARENT, _parent_number),
            )
        ),
    ]
