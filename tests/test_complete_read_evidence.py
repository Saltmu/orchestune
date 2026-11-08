"""Completion request contract tests for parsed stale-read evidence."""

from __future__ import annotations

import pytest
from read_dependency_test_support import (
    BLOB_V1,
    BLOB_V2,
    SHA_OTHER,
    observation,
)
from read_dependency_test_support import (
    evidence as read_evidence,
)

from orchestune.complete import CompleteRequest, CompletionEvidence, DonePayload
from orchestune.outcome_record import RESULT_DONE


def _read_evidence(*, blob=BLOB_V1, head_sha="b" * 40, claim_id="claim-reader"):
    return read_evidence(
        declared=("src/registry.py",),
        observations=(observation("src/registry.py", blob),),
        head_sha=head_sha,
        claim_id=claim_id,
    )


def test_done_read_evidence_flows_to_outcome_record() -> None:
    evidence = _read_evidence()
    from_evidence = CompleteRequest.done(
        1663, 2001, evidence=CompletionEvidence(read_evidence=evidence)
    )
    direct = CompleteRequest.done(1663, 2001, read_evidence=evidence)

    assert isinstance(from_evidence.payload, DonePayload)
    assert isinstance(direct.payload, DonePayload)
    assert from_evidence.payload.read_evidence == evidence
    assert direct.payload.read_evidence == evidence
    assert direct.to_outcome_record().read_evidence == evidence


def test_read_evidence_rejects_invalid_values_and_non_done_use() -> None:
    invalid = object()
    with pytest.raises(TypeError, match="read_evidence must be ReadEvidenceV1"):
        CompletionEvidence(read_evidence=invalid)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="read_evidence must be ReadEvidenceV1"):
        DonePayload(pr=2001, read_evidence=invalid)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="read_evidence is only valid for done"):
        CompleteRequest.blocked(
            1663, "blocked", evidence=CompletionEvidence(read_evidence=_read_evidence())
        )

    payload = DonePayload(pr=2001)
    object.__setattr__(payload, "read_evidence", invalid)
    request = CompleteRequest(issue_number=1663, result=RESULT_DONE, payload=payload)
    with pytest.raises(
        ValueError, match="Done request read_evidence must be ReadEvidenceV1"
    ):
        request.validate()


def test_request_fingerprint_includes_canonical_read_evidence() -> None:
    fingerprints = [
        CompleteRequest.done(1663, 2001, read_evidence=value).request_fingerprint
        for value in (
            _read_evidence(),
            _read_evidence(),
            _read_evidence(blob=BLOB_V2),
            _read_evidence(head_sha=SHA_OTHER),
            _read_evidence(claim_id="claim-other"),
        )
    ]

    assert fingerprints[0] == fingerprints[1]
    assert len(set(fingerprints)) == 4
