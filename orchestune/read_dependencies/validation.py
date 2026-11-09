"""Strict read-validation blocks and exact round/head/report/candidate binding.

This checks coverage and identity, never the reviewer's semantic reasoning.
"""

from __future__ import annotations

import json
import re
from typing import Any

from orchestune.read_dependencies.contracts import (
    READ_VALIDATION_MARKER,
    CandidateConclusion,
    CandidateJudgment,
    ReadValidationV1,
)

_FIELDS = frozenset(
    {"version", "round_number", "head_sha", "report_digest", "decisions"}
)
_DECISION_FIELDS = frozenset({"candidate_id", "conclusion", "rationale"})


def parse_read_validation(body: str) -> ReadValidationV1:
    """Require exactly one fenced JSON block; reject duplicate keys and IDs."""
    blocks = re.findall(
        rf"^```{READ_VALIDATION_MARKER}[^\S\n]*\n(.*?)^```[^\S\n]*$",
        body,
        re.M | re.S,
    )
    openers = re.findall(rf"^```{READ_VALIDATION_MARKER}\b", body, re.M)
    if len(blocks) != 1 or len(openers) != 1:
        raise ValueError("exactly one orchestune-read-validation block is required")
    try:
        raw = json.loads(
            blocks[0],
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except json.JSONDecodeError as error:
        raise ValueError("invalid read-validation JSON") from error
    data = _object(raw, _FIELDS)
    version = data["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ValueError("version must be integer 1")
    if not isinstance(data["decisions"], list):
        raise ValueError("decisions must be a list")
    return ReadValidationV1(
        version=data["version"],
        round_number=data["round_number"],
        head_sha=data["head_sha"],
        report_digest=data["report_digest"],
        decisions=tuple(_decision(item) for item in data["decisions"]),
    )


def validate_read_validation(
    validation: ReadValidationV1,
    *,
    round_number: int,
    head_sha: str,
    report_digest: str,
    candidate_ids: tuple[str, ...],
) -> None:
    """Reject stale bindings, inexact coverage, and unknown conclusions."""
    if (
        validation.round_number != round_number
        or validation.head_sha != head_sha
        or validation.report_digest != report_digest
    ):
        raise ValueError("round/head/report digest mismatch")
    ids = [item.candidate_id for item in validation.decisions]
    if (
        len(ids) != len(set(ids))
        or len(candidate_ids) != len(set(candidate_ids))
        or set(ids) != set(candidate_ids)
    ):
        raise ValueError("candidate decisions must cover all IDs exactly once")
    if any(
        item.conclusion
        not in (CandidateConclusion.COMPATIBLE, CandidateConclusion.REPAIRED)
        for item in validation.decisions
    ):
        raise ValueError("unknown candidate conclusion cannot pass")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(f"duplicate JSON key: {key}")
        data[key] = value
    return data


def _invalid_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant: {value}")


def _object(value: object, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("read-validation object has missing or unexpected fields")
    return value


def _decision(value: object) -> CandidateJudgment:
    data = _object(value, _DECISION_FIELDS)
    if any(not isinstance(data[key], str) or not data[key].strip() for key in data):
        raise ValueError("candidate decision fields must be nonempty strings")
    return CandidateJudgment(
        candidate_id=data["candidate_id"],
        conclusion=CandidateConclusion(data["conclusion"]),
        rationale=data["rationale"],
    )
