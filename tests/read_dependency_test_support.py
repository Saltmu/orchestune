"""Fixed stale-read inputs and expected values for later issues.

Later issues should treat these objects as read-only fixtures. The digests and
candidate id are produced by the v1 contract, not by a codec stub.
"""

from __future__ import annotations

from orchestune.read_dependencies.contracts import (
    ChangedResource,
    ChangeKind,
    ObservationAvailability,
    ReadCoverage,
    ReaderIdentity,
    ReadEvidenceV1,
    ReadObservationV1,
    ReadSource,
    ResourcePresence,
    StaleVerdict,
    aggregate_stale_verdict,
    candidate_id,
    seal_evidence,
)

REPOSITORY = "example/repo"
CLAIM_A = "claim-reader"
CLAIM_B = "claim-writer"
SHA_BASE = "a" * 40
SHA_HEAD = "b" * 40
SHA_OTHER = "c" * 40
BLOB_V1 = "1" * 40
BLOB_V2 = "2" * 40
COMMIT_1 = "d" * 40


def observation(
    path: str,
    blob: str | None,
    *,
    snapshot: str = SHA_BASE,
    presence: ResourcePresence = ResourcePresence.PRESENT,
    source: ReadSource = ReadSource.ADAPTER_OBSERVED,
    mode: str | None = "100644",
) -> ReadObservationV1:
    if presence is not ResourcePresence.PRESENT:
        blob = None
        mode = None
    return ReadObservationV1(
        path=path,
        snapshot_commit=snapshot,
        presence=presence,
        blob_oid=blob,
        file_mode=mode,
        source=source,
    )


def evidence(
    *,
    declared: tuple[str, ...] | None,
    observations: tuple[ReadObservationV1, ...] = (),
    source: ReadSource = ReadSource.ADAPTER_OBSERVED,
    coverage: ReadCoverage = ReadCoverage.COMPLETE,
    claim_id: str = CLAIM_A,
    head_sha: str = SHA_HEAD,
    issue_number: int = 10,
) -> ReadEvidenceV1:
    return seal_evidence(
        ReadEvidenceV1(
            repository=REPOSITORY,
            issue_number=issue_number,
            claim_id=claim_id,
            input_base_sha=SHA_BASE,
            head_sha=head_sha,
            declared_reads=declared,
            observations=observations,
            source=source,
            coverage=coverage,
        )
    )


UNSPECIFIED_DECLARATION = evidence(declared=None)
EMPTY_DECLARATION = evidence(declared=())
TWO_VERSIONS = evidence(
    declared=("src/registry.py",),
    observations=(
        observation("src/registry.py", BLOB_V2, snapshot=SHA_OTHER),
        observation("src/registry.py", BLOB_V1),
    ),
)
ABSENT_READ = evidence(
    declared=(),
    observations=(observation("src/gone.py", None, presence=ResourcePresence.ABSENT),),
)
UNKNOWN_PRESENCE = evidence(
    declared=(),
    observations=(
        observation("src/missing-blob.py", None, presence=ResourcePresence.UNKNOWN),
    ),
)
PARTIAL_COVERAGE = evidence(
    declared=None,
    observations=(observation("src/registry.py", BLOB_V1),),
    coverage=ReadCoverage.PARTIAL,
)
UNAVAILABLE_EVIDENCE = seal_evidence(
    ReadEvidenceV1(
        repository=REPOSITORY,
        issue_number=10,
        claim_id=CLAIM_A,
        input_base_sha=SHA_BASE,
        head_sha=SHA_HEAD,
        declared_reads=None,
        observations=(),
        source=ReadSource.UNAVAILABLE,
        coverage=ReadCoverage.UNAVAILABLE,
    )
)
READER = ReaderIdentity(
    repository=REPOSITORY,
    issue_number=10,
    claim_id=CLAIM_A,
    head_sha=SHA_HEAD,
)
OTHER_HEAD_READER = ReaderIdentity(
    repository=REPOSITORY,
    issue_number=10,
    claim_id=CLAIM_A,
    head_sha=SHA_OTHER,
)
MODIFY = ChangedResource(
    path="src/registry.py",
    old_path=None,
    kind=ChangeKind.MODIFY,
    old_blob_oid=BLOB_V1,
    new_blob_oid=BLOB_V2,
    old_file_mode="100644",
    new_file_mode="100644",
    evidence_commits=(COMMIT_1,),
    availability=ObservationAvailability.RESOLVED,
)
FORWARD_CANDIDATE_COUNT = 1
FORWARD_UNKNOWN_COUNT = 0
FORWARD_VERDICT = aggregate_stale_verdict(
    candidate_count=FORWARD_CANDIDATE_COUNT,
    unknown_count=FORWARD_UNKNOWN_COUNT,
    coverage=ReadCoverage.COMPLETE,
)
assert FORWARD_VERDICT is StaleVerdict.CANDIDATE_STALE


def forward_candidate_id() -> str:
    """Id for the v1 reader observation intersected with ``MODIFY``.

    Imported lazily so this module can be read before the identity helper is
    composed by the contract tests.
    """
    from orchestune.read_dependencies.contracts import (  # noqa: PLC0415
        CandidateIdentity,
        ReadDirection,
    )

    identity = CandidateIdentity(
        repository=REPOSITORY,
        parent_issue_number=1230,
        direction=ReadDirection.FORWARD,
        reader=READER,
        evidence_digest=TWO_VERSIONS.digest or "",
        observation=observation("src/registry.py", BLOB_V1),
        change=MODIFY,
        writer_issue_number=11,
        writer_claim_id=CLAIM_B,
    )
    return candidate_id(identity)


FORWARD_CANDIDATE_ID = forward_candidate_id()
