"""Strict wire format for durable parent reader snapshots.

The L0 contract owns canonical ordering and digest computation. A stored
snapshot must carry its digest; missing data cannot become an empty index.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orchestune.read_dependencies.contracts import (
    ParentReadSnapshotV1,
    ReaderSnapshotEntry,
    ValidationCheckpoint,
    read_snapshot_digest_input,
    seal_snapshot,
)

_SNAPSHOT_FIELDS = frozenset(
    {
        "schema",
        "version",
        "repository",
        "parent_issue_number",
        "parent_head_sha",
        "readers",
        "digest",
    }
)
_READER_FIELDS = frozenset(
    {
        "issue_number",
        "claim_id",
        "completion_id",
        "source_sha",
        "evidence_digest",
        "validations",
    }
)
_CHECKPOINT_FIELDS = frozenset({"candidate_head_sha", "report_digest"})


def parent_read_snapshot_to_dict(value: ParentReadSnapshotV1) -> dict[str, Any]:
    """Serialize with the canonical digest, rejecting a stale supplied digest."""
    if not isinstance(value, ParentReadSnapshotV1):
        raise TypeError("value must be ParentReadSnapshotV1")
    if isinstance(value.version, bool) or not isinstance(value.version, int):
        raise TypeError("version must be an integer")
    sealed = seal_snapshot(value)
    if value.digest is not None and value.digest != sealed.digest:
        raise ValueError("snapshot digest does not match the payload")
    payload = read_snapshot_digest_input(sealed)
    payload["digest"] = sealed.digest
    return payload


def parse_parent_read_snapshot(mapping: object) -> ParentReadSnapshotV1:
    """Parse a complete v1 snapshot and verify its canonical digest."""
    raw = _record(mapping, _SNAPSHOT_FIELDS)
    if raw["schema"] != "parent-read-snapshot-v1":
        raise ValueError("schema must be parent-read-snapshot-v1")
    if isinstance(raw["version"], bool) or not isinstance(raw["version"], int):
        raise TypeError("version must be an integer")
    snapshot = seal_snapshot(
        ParentReadSnapshotV1(
            version=raw["version"],
            repository=raw["repository"],
            parent_issue_number=raw["parent_issue_number"],
            parent_head_sha=raw["parent_head_sha"],
            readers=tuple(_reader(item) for item in _array(raw["readers"])),
        )
    )
    if raw["digest"] != snapshot.digest:
        raise ValueError("snapshot digest does not match the payload")
    return snapshot


def _record(value: object, fields: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("snapshot record must be an object")
    if set(value) != fields:
        raise ValueError("snapshot record has missing or unknown fields")
    return value


def _array(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError("snapshot collection must be a list")
    return value


def _reader(value: object) -> ReaderSnapshotEntry:
    raw = _record(value, _READER_FIELDS)
    return ReaderSnapshotEntry(
        issue_number=raw["issue_number"],
        claim_id=raw["claim_id"],
        completion_id=raw["completion_id"],
        source_sha=raw["source_sha"],
        evidence_digest=raw["evidence_digest"],
        validations=tuple(_checkpoint(item) for item in _array(raw["validations"])),
    )


def _checkpoint(value: object) -> ValidationCheckpoint:
    raw = _record(value, _CHECKPOINT_FIELDS)
    return ValidationCheckpoint(
        candidate_head_sha=raw["candidate_head_sha"],
        report_digest=raw["report_digest"],
    )
