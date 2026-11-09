"""Canonical JSON codec for stale-read reports."""

from __future__ import annotations

import json
from dataclasses import asdict, fields, is_dataclass, replace
from enum import StrEnum
from typing import Any, TypeVar, cast

from .contracts import (
    CandidateIdentity,
    ChangedResource,
    ChangeKind,
    ExclusionReason,
    ObservationAvailability,
    ReadCoverage,
    ReadDirection,
    ReaderEvidenceRef,
    ReaderIdentity,
    ReadObservationV1,
    ReadSource,
    ResourcePresence,
    StaleCandidate,
    StaleExclusion,
    StaleReadReportV1,
    StaleUnknown,
    StaleVerdict,
    UnknownReason,
    canonical_json_sha256,
    read_report_digest_input,
    seal_report,
)

_SCHEMA = "stale-read-report-v1"
_ENUM_FIELDS: dict[type[Any], dict[str, type[StrEnum]]] = {
    CandidateIdentity: {"direction": ReadDirection},
    ChangedResource: {
        "kind": ChangeKind,
        "availability": ObservationAvailability,
        "unknown_reason": UnknownReason,
    },
    ReadObservationV1: {"presence": ResourcePresence, "source": ReadSource},
    ReaderEvidenceRef: {},
    ReaderIdentity: {},
    StaleExclusion: {"reason": ExclusionReason},
    StaleReadReportV1: {"coverage": ReadCoverage, "verdict": StaleVerdict},
    StaleUnknown: {"reason": UnknownReason},
}
_T = TypeVar("_T")


def _json_ready(value: object) -> object:
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _json_ready(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_ready(item) for item in value]
    return value


def report_digest(report: StaleReadReportV1) -> str:
    """Return the frozen v1 digest for ``report``, ignoring its digest field."""
    return canonical_json_sha256(read_report_digest_input(report))


def normalize_report(report: StaleReadReportV1) -> StaleReadReportV1:
    """Canonicalize contract fields and attach the valid report digest."""
    expected = report_digest(report)
    if report.digest is not None and report.digest != expected:
        raise ValueError("stale-read report digest does not match its payload")
    return seal_report(replace(report, digest=None))


def _report_payload(report: StaleReadReportV1) -> dict[str, object]:
    normalized = normalize_report(report)
    payload = _json_ready(normalized)
    if not isinstance(payload, dict):
        raise ValueError("stale-read report must serialize as an object")
    return {"schema": _SCHEMA, **payload}


def serialize_report(report: StaleReadReportV1) -> str:
    """Serialize a normalized report as canonical UTF-8-compatible JSON."""
    return json.dumps(
        _report_payload(report),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _keys(value: dict[str, Any], expected: set[str], name: str) -> dict[str, Any]:
    missing = expected - value.keys()
    extra = value.keys() - expected
    if missing or extra:
        raise ValueError(
            f"{name} has invalid fields (missing={sorted(missing)}, extra={sorted(extra)})"
        )
    return value


def _construct(cls: type[_T], value: object, name: str, **nested: Any) -> _T:
    data = _object(value, name)
    allowed = {field.name for field in fields(cast(Any, cls))}
    extra = data.keys() - allowed
    missing = allowed - data.keys() - nested.keys()
    if missing or extra:
        raise ValueError(
            f"{name} has invalid fields (missing={sorted(missing)}, extra={sorted(extra)})"
        )
    for nested_name in nested:
        data.pop(nested_name, None)
    for field_name, enum_type in _ENUM_FIELDS.get(cls, {}).items():
        if data.get(field_name) is None:
            continue
        try:
            data[field_name] = enum_type(data[field_name])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid {name} {field_name}: {exc}") from exc
    data.update(nested)
    try:
        return cls(**data)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {name}: {exc}") from exc


def _list(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def _reader(value: object) -> ReaderIdentity:
    return _construct(ReaderIdentity, value, "reader")


def _observation(value: object) -> ReadObservationV1:
    return _construct(ReadObservationV1, value, "observation")


def _change(value: object) -> ChangedResource:
    return _construct(ChangedResource, value, "change")


def _identity(value: object) -> CandidateIdentity:
    data = _object(value, "candidate identity")
    _keys(
        data,
        {
            "repository",
            "parent_issue_number",
            "direction",
            "reader",
            "evidence_digest",
            "observation",
            "change",
            "writer_issue_number",
            "writer_claim_id",
        },
        "candidate identity",
    )
    return _construct(
        CandidateIdentity,
        data,
        "candidate identity",
        reader=_reader(data["reader"]),
        observation=_observation(data["observation"]),
        change=_change(data["change"]),
    )


def _candidate(value: object) -> StaleCandidate:
    data = _object(value, "candidate")
    _keys(data, {"identity", "candidate_id"}, "candidate")
    return _construct(
        StaleCandidate,
        data,
        "candidate",
        identity=_identity(data["identity"]),
    )


def _exclusion(value: object) -> StaleExclusion:
    data = _object(value, "exclusion")
    _keys(data, {"identity", "reason", "detail"}, "exclusion")
    return _construct(
        StaleExclusion,
        data,
        "exclusion",
        identity=_identity(data["identity"]),
    )


def _unknown(value: object) -> StaleUnknown:
    return _construct(StaleUnknown, value, "unknown")


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def parse_report(payload: str | bytes) -> StaleReadReportV1:
    """Strictly parse and verify a serialized stale-read report."""
    try:
        raw = json.loads(
            payload,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid stale-read report JSON: {exc}") from exc
    data = _object(raw, "stale-read report")
    if data.get("schema") != _SCHEMA:
        raise ValueError("unsupported stale-read report schema")
    report_keys = {field.name for field in fields(StaleReadReportV1)}
    _keys(data, report_keys | {"schema"}, "stale-read report")
    if not isinstance(data["readers"], list):
        raise ValueError("readers must be an array")
    nested = {
        "readers": [
            _construct(ReaderEvidenceRef, item, "reader evidence reference")
            for item in data["readers"]
        ],
        "candidates": [
            _candidate(item) for item in _list(data["candidates"], "candidates")
        ],
        "exclusions": [
            _exclusion(item) for item in _list(data["exclusions"], "exclusions")
        ],
        "unknowns": [_unknown(item) for item in _list(data["unknowns"], "unknowns")],
    }
    top_level = {
        key: value for key, value in data.items() if key not in {"schema", *nested}
    }
    report = _construct(StaleReadReportV1, top_level, "stale-read report", **nested)
    if not isinstance(report.digest, str) or report.digest != report_digest(report):
        raise ValueError("stale-read report digest is missing or invalid")
    return normalize_report(report)
