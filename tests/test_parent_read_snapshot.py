"""Strict parent snapshot wire format and immutable read identities."""

from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest

from orchestune.read_dependencies.contracts import (
    ParentReadSnapshotV1,
    ReaderSnapshotEntry,
    ValidationCheckpoint,
    seal_snapshot,
)
from orchestune.read_dependencies.snapshot_codec import (
    parent_read_snapshot_to_dict,
    parse_parent_read_snapshot,
)


def snapshot() -> ParentReadSnapshotV1:
    return seal_snapshot(
        ParentReadSnapshotV1(
            repository="owner/repo",
            parent_issue_number=1230,
            parent_head_sha="a" * 40,
            readers=(
                ReaderSnapshotEntry(
                    issue_number=1,
                    claim_id="claim-1",
                    completion_id="completion-1",
                    source_sha="b" * 40,
                    evidence_digest="c" * 64,
                    validations=(
                        ValidationCheckpoint(
                            candidate_head_sha="d" * 40, report_digest="e" * 64
                        ),
                    ),
                ),
            ),
        )
    )


def test_round_trip_and_original_read_identity() -> None:
    original = snapshot()
    raw = parent_read_snapshot_to_dict(original)
    assert parse_parent_read_snapshot(raw) == original
    assert raw["schema"] == "parent-read-snapshot-v1"
    reader = original.readers[0]
    changed = seal_snapshot(
        replace(original, readers=(replace(reader, validations=()),))
    )
    assert changed.digest != original.digest
    assert changed.readers[0].source_sha == reader.source_sha
    assert changed.readers[0].evidence_digest == reader.evidence_digest


def test_readers_are_canonical_and_empty_is_explicit() -> None:
    original = snapshot()
    reader = replace(original.readers[0], issue_number=2)
    first = seal_snapshot(replace(original, readers=(reader, *original.readers)))
    other = seal_snapshot(
        replace(original, readers=(*original.readers, reader, reader))
    )
    assert first == other
    empty = seal_snapshot(replace(original, readers=()))
    assert parse_parent_read_snapshot(parent_read_snapshot_to_dict(empty)) == empty


@pytest.mark.parametrize("field", list(parent_read_snapshot_to_dict(snapshot())))
def test_missing_top_level_field(field: str) -> None:
    raw = parent_read_snapshot_to_dict(snapshot())
    del raw[field]
    with pytest.raises((TypeError, ValueError)):
        parse_parent_read_snapshot(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", "other"),
        ("version", True),
        ("version", 1.0),
        ("version", 2),
        ("repository", "other/repo"),
        ("parent_issue_number", True),
        ("parent_head_sha", "main"),
        ("readers", None),
        ("readers", {}),
        ("digest", None),
        ("digest", "f" * 64),
        ("unknown", 1),
    ],
)
def test_invalid_or_tampered_snapshot(field: str, value: Any) -> None:
    raw = parent_read_snapshot_to_dict(snapshot())
    raw[field] = value
    with pytest.raises((TypeError, ValueError)):
        parse_parent_read_snapshot(raw)


@pytest.mark.parametrize("section", ["reader", "validation"])
@pytest.mark.parametrize("operation", ["missing", "extra", "wrong_type"])
def test_nested_records_are_strict(section: str, operation: str) -> None:
    raw = deepcopy(parent_read_snapshot_to_dict(snapshot()))
    target = raw["readers"][0]
    key = "claim_id"
    if section == "validation":
        target = target["validations"][0]
        key = "report_digest"
    if operation == "missing":
        del target[key]
    elif operation == "extra":
        target["surprise"] = 1
    else:
        target[key] = False
    with pytest.raises((TypeError, ValueError)):
        parse_parent_read_snapshot(raw)


@pytest.mark.parametrize("value", [None, [], "snapshot", 1])
def test_non_object(value: object) -> None:
    with pytest.raises(TypeError):
        parse_parent_read_snapshot(value)


def test_serialize_seals_but_rejects_incorrect_digest() -> None:
    original = snapshot()
    assert parent_read_snapshot_to_dict(replace(original, digest=None)) == (
        parent_read_snapshot_to_dict(original)
    )
    with pytest.raises(ValueError, match="digest"):
        parent_read_snapshot_to_dict(replace(original, digest="f" * 64))
    with pytest.raises(TypeError, match="version"):
        parent_read_snapshot_to_dict(replace(original, version=True))
    with pytest.raises(TypeError):
        parent_read_snapshot_to_dict(None)  # type: ignore[arg-type]
