"""Codec tests for ReadEvidenceV1 parse, serialize, and digest."""

from __future__ import annotations

import pytest
from read_dependency_test_support import (
    BLOB_V1,
    BLOB_V2,
    EMPTY_DECLARATION,
    SHA_OTHER,
    TWO_VERSIONS,
    UNAVAILABLE_EVIDENCE,
    UNSPECIFIED_DECLARATION,
    observation,
)

from orchestune.read_dependencies.codec import (
    canonical_payload_digest,
    parse_read_evidence,
    read_evidence_to_dict,
)
from orchestune.read_dependencies.contracts import seal_evidence

_PATH = "src/registry.py"


def test_round_trip_preserves_fixed_fixtures() -> None:
    for value in (
        UNSPECIFIED_DECLARATION,
        EMPTY_DECLARATION,
        TWO_VERSIONS,
        UNAVAILABLE_EVIDENCE,
    ):
        parsed = parse_read_evidence(read_evidence_to_dict(value))
        assert parsed == value
        assert parsed.digest == value.digest


def test_order_and_duplicate_observations_keep_the_digest() -> None:
    canonical = read_evidence_to_dict(TWO_VERSIONS)
    swapped = dict(canonical)
    swapped["observations"] = list(reversed(canonical["observations"]))
    duplicated = dict(canonical)
    duplicated["observations"] = [
        *canonical["observations"],
        canonical["observations"][0],
    ]
    assert parse_read_evidence(swapped) == TWO_VERSIONS
    assert parse_read_evidence(duplicated).digest == TWO_VERSIONS.digest
    assert canonical_payload_digest(swapped) == TWO_VERSIONS.digest
    assert canonical_payload_digest(duplicated) == TWO_VERSIONS.digest


def test_content_and_version_change_the_digest() -> None:
    payload = read_evidence_to_dict(TWO_VERSIONS)
    other = dict(payload)
    other["observations"] = [
        dict(payload["observations"][0]),
        payload["observations"][1],
    ]
    other["observations"][0]["blob_oid"] = (
        BLOB_V1 if other["observations"][0]["blob_oid"] == BLOB_V2 else BLOB_V2
    )
    revised = dict(payload)
    revised["version"] = 2
    assert canonical_payload_digest(other) != TWO_VERSIONS.digest
    assert canonical_payload_digest(revised) != TWO_VERSIONS.digest
    assert canonical_payload_digest(payload) == TWO_VERSIONS.digest


def test_unspecified_declaration_is_not_an_empty_set() -> None:
    payload = read_evidence_to_dict(UNSPECIFIED_DECLARATION)
    assert payload["declared_reads"] is None
    empty = read_evidence_to_dict(EMPTY_DECLARATION)
    assert empty["declared_reads"] == []
    assert parse_read_evidence(payload).declared_reads is None
    assert parse_read_evidence(empty).declared_reads == ()


def test_unavailable_is_not_rewritten_as_an_empty_declaration() -> None:
    payload = read_evidence_to_dict(UNAVAILABLE_EVIDENCE)
    assert payload["declared_reads"] is None
    assert payload["coverage"] == "unavailable"
    parsed = parse_read_evidence(payload)
    assert parsed.declared_reads is None
    assert parsed.observations == ()
    assert parsed.source.value == "unavailable"
    forged = dict(payload)
    forged["declared_reads"] = []
    forged["digest"] = None
    with pytest.raises(ValueError):
        parse_read_evidence(forged)


def test_rejects_bad_types_paths_and_digest_mismatch() -> None:
    payload = read_evidence_to_dict(EMPTY_DECLARATION)
    mismatched = dict(payload)
    mismatched["digest"] = "f" * 64
    windows = dict(payload)
    windows["declared_reads"] = ["src\\registry.py"]
    windows["digest"] = None
    cases = (
        {**payload, "issue_number": True},
        {**payload, "declared_reads": [1]},
        {**payload, "declared_reads": ["C:/src/registry.py"]},
        {**payload, "declared_reads": ["/src/registry.py"]},
        {**payload, "declared_reads": ["src/../registry.py"]},
        {**payload, "declared_reads": ["src/*.py"]},
        {**payload, "declared_reads": ["a\nb"]},
        {**payload, "source": "not-a-source"},
        {**payload, "coverage": True},
        {**payload, "head_sha": "abc"},
        mismatched,
        {"repository": payload["repository"]},
    )
    for raw in cases:
        with pytest.raises((TypeError, ValueError)):
            parse_read_evidence(raw)
    parsed = parse_read_evidence(windows)
    assert parsed.declared_reads == (_PATH,)
    assert parsed.digest == seal_evidence(parsed).digest


def test_same_path_with_different_commits_both_remain() -> None:
    payload = read_evidence_to_dict(TWO_VERSIONS)
    parsed = parse_read_evidence(payload)
    identities = {(item.snapshot_commit, item.blob_oid) for item in parsed.observations}
    assert identities == {
        (SHA_OTHER, BLOB_V2),
        (observation(_PATH, BLOB_V1).snapshot_commit, BLOB_V1),
    }


def test_digest_ignores_digest_timestamp_and_local_path() -> None:
    payload = read_evidence_to_dict(EMPTY_DECLARATION)
    decorated = dict(payload)
    decorated["created_at"] = "2026-11-08T00:00:00Z"
    decorated["local_path"] = "/tmp/worktree"
    decorated["digest"] = "0" * 64
    assert canonical_payload_digest(decorated) == EMPTY_DECLARATION.digest
