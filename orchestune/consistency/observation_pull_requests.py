"""Pull request observation readers."""

from __future__ import annotations

from orchestune.consistency.models import Observation
from orchestune.consistency.observation_readings import (
    _UNKNOWN,
    SOURCE_FORGE,
    _emitter,
    _ForgeReading,
    _Identifier,
    _Index,
    _Reading,
    _unknown_forge_reading,
    forge_reading,
)
from orchestune.consistency.vocabulary import (
    FACT_PULL_REQUEST_BASE_REF,
    FACT_PULL_REQUEST_HEAD_REF,
    FACT_PULL_REQUEST_NUMBER,
    FACT_PULL_REQUEST_STATE,
)

_FILTERED_PULL_REQUESTS = (
    "the reused pull request snapshot is filtered, so finding no match does "
    "not prove that no pull request exists"
)


def _pull_request_readings(
    branch: _Identifier, reading: _ForgeReading, index: _Index
) -> tuple[_Reading, _Reading, _Reading, _Reading]:
    """Link a task to its pull request, or record why the link is unclear."""
    if reading.snapshot is None:
        unknown = _Reading(None, _UNKNOWN, reading.diagnostics)
        return unknown, unknown, unknown, unknown
    if branch.value is None:
        unknown = _unknown_forge_reading(reading, branch.diagnostics)
        return unknown, unknown, unknown, unknown
    candidates = index.pull_requests_by_branch.get(branch.value, ())
    if len(candidates) > 1:
        numbers = ", ".join(str(record.number) for record in candidates)
        unknown = _unknown_forge_reading(
            reading,
            (
                "ambiguous pull request correspondence for branch "
                f"{branch.value!r}: candidates {numbers}",
            ),
        )
        return unknown, unknown, unknown, unknown
    if not candidates:
        if not reading.snapshot.pull_requests_complete:
            unknown = _unknown_forge_reading(
                reading,
                (_FILTERED_PULL_REQUESTS,),
            )
            return unknown, unknown, unknown, unknown
        return (
            forge_reading(reading, None),
            forge_reading(reading, None),
            forge_reading(reading, None),
            forge_reading(reading, None),
        )
    return (
        forge_reading(reading, candidates[0].number),
        forge_reading(reading, candidates[0].state),
        forge_reading(reading, candidates[0].head_ref),
        forge_reading(reading, candidates[0].base_ref),
    )


def pull_request_observations(
    branch: _Identifier, reading: _ForgeReading, index: _Index
) -> list[Observation]:
    number, state, head_ref, base_ref = _pull_request_readings(branch, reading, index)
    forge = _emitter(SOURCE_FORGE, reading.observed_at)
    return [
        forge(FACT_PULL_REQUEST_NUMBER, number),
        forge(FACT_PULL_REQUEST_STATE, state),
        forge(FACT_PULL_REQUEST_HEAD_REF, head_ref),
        forge(FACT_PULL_REQUEST_BASE_REF, base_ref),
    ]
