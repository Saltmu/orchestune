from __future__ import annotations

import json
from dataclasses import replace

import pytest

from orchestune.read_dependencies.contracts import (
    CandidateIdentity,
    ReadCoverage,
    ReadDirection,
    ReaderEvidenceRef,
    StaleCandidate,
    StaleReadReportV1,
    StaleUnknown,
    StaleVerdict,
    UnknownReason,
    candidate_id,
    seal_report,
)
from orchestune.read_dependencies.report_codec import (
    normalize_report,
    parse_report,
    report_digest,
    serialize_report,
)
from tests.read_dependency_test_support import (
    CLAIM_A,
    MODIFY,
    READER,
    REPOSITORY,
    SHA_BASE,
    SHA_HEAD,
    TWO_VERSIONS,
    observation,
)


def sample_report() -> StaleReadReportV1:
    identity = CandidateIdentity(
        repository=REPOSITORY,
        parent_issue_number=1230,
        direction=ReadDirection.FORWARD,
        reader=READER,
        evidence_digest=TWO_VERSIONS.digest or "",
        observation=observation("src/registry.py", "1" * 40),
        change=MODIFY,
        writer_issue_number=11,
        writer_claim_id="claim-writer",
    )
    candidate = StaleCandidate(identity=identity, candidate_id=candidate_id(identity))
    return seal_report(
        StaleReadReportV1(
            repository=REPOSITORY,
            parent_issue_number=1230,
            parent_sha=SHA_BASE,
            candidate_head_sha=SHA_HEAD,
            readers=(
                ReaderEvidenceRef(
                    issue_number=10,
                    claim_id=CLAIM_A,
                    head_sha=SHA_HEAD,
                    evidence_digest=TWO_VERSIONS.digest,
                ),
            ),
            candidates=(candidate,),
            unknowns=(
                StaleUnknown(
                    reason=UnknownReason.FETCH_FAILED,
                    path="src/other.py",
                    reader_issue_number=10,
                    detail="fetch was unavailable",
                ),
            ),
            coverage=ReadCoverage.PARTIAL,
            verdict=StaleVerdict.CANDIDATE_STALE,
        )
    )


def test_report_codec_round_trips_candidates_and_reasons() -> None:
    original = sample_report()

    encoded = serialize_report(original)
    restored = parse_report(encoded)

    assert restored == normalize_report(original)
    assert restored.candidates[0].candidate_id == original.candidates[0].candidate_id
    assert restored.unknowns[0].detail == "fetch was unavailable"
    assert restored.digest == report_digest(original)


def test_report_codec_preserves_candidate_order_and_reference_shas() -> None:
    report = sample_report()
    first = report.candidates[0]
    second_path = "src/second.py"
    second_identity = replace(
        first.identity,
        observation=observation(second_path, "2" * 40),
        change=replace(first.identity.change, path=second_path),
    )
    second = StaleCandidate(
        identity=second_identity, candidate_id=candidate_id(second_identity)
    )
    later_reader = replace(report.readers[0], issue_number=12, head_sha=SHA_BASE)
    report = seal_report(
        replace(
            report,
            candidates=(second, first),
            readers=(report.readers[0], later_reader),
            digest=None,
        )
    )

    restored = parse_report(serialize_report(report))

    assert (
        restored.candidates
        == report.candidates
        == tuple(sorted((second, first), key=lambda candidate: candidate.candidate_id))
    )
    assert [reader.head_sha for reader in restored.readers] == [
        reader.head_sha for reader in report.readers
    ]
    assert [reader.evidence_digest for reader in restored.readers] == [
        TWO_VERSIONS.digest,
        TWO_VERSIONS.digest,
    ]


def test_report_codec_rejects_tampered_digest() -> None:
    payload = json.loads(serialize_report(sample_report()))
    payload["digest"] = "0" * 64

    with pytest.raises(ValueError, match="digest"):
        parse_report(json.dumps(payload))


def test_report_codec_rejects_unknown_fields_and_duplicate_json_keys() -> None:
    payload = json.loads(serialize_report(sample_report()))
    payload["unexpected"] = "ignored fields must not be dropped"

    with pytest.raises(ValueError, match="field"):
        parse_report(json.dumps(payload))
    with pytest.raises(ValueError, match="duplicate"):
        parse_report(
            '{"schema":"stale-read-report-v1","schema":"stale-read-report-v1"}'
        )


def test_report_codec_normalizes_digest_and_rejects_invalid_json_types() -> None:
    report = sample_report()
    sealed = normalize_report(report)

    assert sealed.digest == report_digest(sealed)
    with pytest.raises(ValueError):
        parse_report("[]")
