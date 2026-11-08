"""Parse, serialize, and digest ``ReadEvidenceV1``.

The digest is the contract's canonical payload. It omits the digest field,
timestamps, and local paths. This module does not read Git.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from orchestune.read_dependencies.contracts import (
    ReadCoverage,
    ReadEvidenceV1,
    ReadObservationV1,
    ReadSource,
    ResourcePresence,
    canonical_json_sha256,
    normalize_read_path,
    read_evidence_digest_input,
    seal_evidence,
)

_EXCLUDED = frozenset(
    {"digest", "created_at", "timestamp", "recorded_at", "local_path"}
)
_SCHEMA = "read-evidence-v1"
_EVIDENCE = frozenset(
    {
        "schema",
        "version",
        "repository",
        "issue_number",
        "claim_id",
        "input_base_sha",
        "head_sha",
        "declared_reads",
        "observations",
        "source",
        "coverage",
        "digest",
    }
)
_REQUIRED = _EVIDENCE - {"digest"}
_OBSERVATION = frozenset(
    {"path", "snapshot_commit", "presence", "blob_oid", "file_mode", "source"}
)


def canonical_payload_digest(payload: object) -> str:
    """SHA-256 of a read-evidence payload, independent of collection order."""
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    body = {str(key): value for key, value in payload.items() if key not in _EXCLUDED}
    if "declared_reads" in body:
        body["declared_reads"] = _sorted_declared(body["declared_reads"])
    if "observations" in body:
        body["observations"] = _sorted_observations(body["observations"])
    return canonical_json_sha256(body)


def read_evidence_to_dict(value: ReadEvidenceV1) -> dict[str, Any]:
    """Serialize evidence, including a null digest when it is not sealed."""
    if not isinstance(value, ReadEvidenceV1):
        raise TypeError("value must be ReadEvidenceV1")
    payload = read_evidence_digest_input(value)
    payload["digest"] = value.digest
    return payload


def parse_read_evidence(mapping: object) -> ReadEvidenceV1:
    """Parse a mapping and reject a digest that does not match the payload."""
    raw = _evidence_mapping(mapping)
    if raw["schema"] != _SCHEMA:
        raise ValueError("schema must be read-evidence-v1")
    evidence = ReadEvidenceV1(
        version=raw["version"],
        repository=raw["repository"],
        issue_number=raw["issue_number"],
        claim_id=raw["claim_id"],
        input_base_sha=raw["input_base_sha"],
        head_sha=raw["head_sha"],
        declared_reads=_declared(raw["declared_reads"]),
        observations=_observations(raw["observations"]),
        source=_enum(ReadSource, raw["source"], "source"),
        coverage=_enum(ReadCoverage, raw["coverage"], "coverage"),
    )
    sealed = seal_evidence(evidence)
    supplied = raw.get("digest")
    if supplied is not None and supplied != sealed.digest:
        raise ValueError("evidence digest does not match the payload")
    return sealed


def _evidence_mapping(mapping: object) -> Mapping[str, Any]:
    if not isinstance(mapping, Mapping):
        raise TypeError("evidence must be a mapping")
    unknown = set(mapping) - _EVIDENCE
    if unknown:
        raise ValueError(f"unknown evidence fields: {sorted(unknown)}")
    missing = _REQUIRED - set(mapping)
    if missing:
        raise ValueError(f"missing evidence fields: {sorted(missing)}")
    return mapping


def _enum(kind: type[StrEnum], value: object, field: str) -> Any:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    try:
        return kind(value)
    except ValueError as error:
        raise ValueError(f"{field} is not a known value") from error


def _declared(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str) or not isinstance(value, list):
        raise TypeError("declared_reads must be a list or null")
    return tuple(value)


def _observations(value: object) -> tuple[ReadObservationV1, ...]:
    if not isinstance(value, list):
        raise TypeError("observations must be a list")
    return tuple(_one_observation(item) for item in value)


def _one_observation(raw: object) -> ReadObservationV1:
    if not isinstance(raw, Mapping):
        raise TypeError("observation must be an object")
    unknown = set(raw) - _OBSERVATION
    if unknown:
        raise ValueError(f"unknown observation fields: {sorted(unknown)}")
    missing = _OBSERVATION - set(raw)
    if missing:
        raise ValueError(f"missing observation fields: {sorted(missing)}")
    return ReadObservationV1(
        path=raw["path"],
        snapshot_commit=raw["snapshot_commit"],
        presence=_enum(ResourcePresence, raw["presence"], "presence"),
        blob_oid=raw["blob_oid"],
        file_mode=raw["file_mode"],
        source=_enum(ReadSource, raw["source"], "source"),
    )


def _sorted_declared(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, str) or not isinstance(value, list):
        return value
    return sorted({normalize_read_path(item) for item in value})


def _sorted_observations(value: object) -> object:
    if not isinstance(value, list):
        return value
    ranked: dict[tuple[object, ...], dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            return value
        copy = _normalized_observation(item)
        ranked[_observation_key(copy)] = copy
    return [ranked[key] for key in sorted(ranked)]


def _normalized_observation(item: Mapping[str, Any]) -> dict[str, Any]:
    copy = dict(item)
    if isinstance(copy.get("path"), str):
        copy["path"] = normalize_read_path(copy["path"])
    return copy


def _observation_key(item: Mapping[str, Any]) -> tuple[object, ...]:
    return (
        item.get("path"),
        item.get("snapshot_commit") or "",
        item.get("presence"),
        item.get("blob_oid") or "",
        item.get("file_mode") or "",
        item.get("source"),
    )
