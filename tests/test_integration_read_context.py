"""Reader assembly uses durable generations, including closed children."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from orchestune.integrator.proofs import TaskIntegrationProof
from orchestune.integrator.read_context import (
    ReadContextRequest,
    ReaderHistory,
    ReaderSourceProof,
    build_read_context,
)
from orchestune.outcome_record import OutcomeRecord, ReviewSummary
from orchestune.read_dependencies.contracts import (
    CheckpointAvailability,
    ParentReadSnapshotV1,
    ReadCheckpointV1,
    ReadCoverage,
    ReadDirection,
    ReaderSnapshotEntry,
    StaleVerdict,
    UnknownReason,
    ValidationCheckpoint,
    WriterChanges,
    seal_snapshot,
)
from orchestune.read_dependencies.detection import detect_stale_reads
from tests.read_dependency_test_support import (
    CLAIM_A,
    CLAIM_B,
    MODIFY,
    PARTIAL_COVERAGE,
    REPOSITORY,
    SHA_BASE,
    SHA_HEAD,
    SHA_OTHER,
    TWO_VERSIONS,
    UNAVAILABLE_EVIDENCE,
)

CHECKPOINT = ValidationCheckpoint(candidate_head_sha=SHA_OTHER, report_digest="f" * 64)
ENTRY = ReaderSnapshotEntry(
    issue_number=10,
    claim_id=CLAIM_A,
    completion_id="completion-reader",
    source_sha=SHA_HEAD,
    evidence_digest=TWO_VERSIONS.digest or "",
    validations=(CHECKPOINT,),
)
SNAPSHOT = seal_snapshot(
    ParentReadSnapshotV1(
        repository=REPOSITORY,
        parent_issue_number=1230,
        parent_head_sha=SHA_OTHER,
        readers=(ENTRY,),
    )
)
SOURCE = ReaderSourceProof(
    proof=TaskIntegrationProof(10, "reader", "deleted/reader", SHA_HEAD),
    claim_id=CLAIM_A,
    completion_id="completion-reader",
    checkpoint=ReadCheckpointV1(
        repository=REPOSITORY,
        issue_number=10,
        claim_id=CLAIM_A,
        availability=CheckpointAvailability.RECORDED,
        input_base_sha=SHA_BASE,
    ),
    outcome=OutcomeRecord(
        result="done",
        issue=10,
        claim_id=CLAIM_A,
        completion_id="completion-reader",
        head_sha=SHA_HEAD,
        review=ReviewSummary(reviewed_head_sha=SHA_HEAD),
        read_evidence=TWO_VERSIONS,
    ),
    validations=(CHECKPOINT,),
)
assert SOURCE.checkpoint is not None


def request(**kwargs: object) -> ReadContextRequest:
    values = dict(
        repository=REPOSITORY,
        parent_issue_number=1230,
        parent_sha=SHA_OTHER,
        candidate_head_sha="d" * 40,
        writers=(WriterChanges(issue_number=11, claim_id=CLAIM_B, changes=(MODIFY,)),),
    )
    return ReadContextRequest(**(values | kwargs))  # type: ignore[arg-type]


def backend(*sources: ReaderSourceProof) -> Mock:
    result = Mock(spec=("list_integrated_readers", "load_snapshot_evidence"))
    result.list_integrated_readers.return_value = ReaderHistory(
        readers=sources, complete=True, empty_verified=not sources
    )
    result.load_snapshot_evidence.return_value = TWO_VERSIONS
    return result


def test_closed_reader_with_deleted_branch_bootstraps_reverse_input() -> None:
    source = backend(SOURCE)
    result = build_read_context(request(), source)
    source.list_integrated_readers.assert_called_once_with(
        repository=REPOSITORY,
        parent_issue_number=1230,
        parent_sha=SHA_OTHER,
        include_closed=True,
    )
    assert result.bootstrap
    assert result.snapshot == SNAPSHOT
    assert result.coverage is ReadCoverage.COMPLETE
    assert result.inputs is not None
    report = detect_stale_reads(result.inputs)
    assert report.verdict is StaleVerdict.CANDIDATE_STALE
    assert report.candidates[0].identity.direction is ReadDirection.REVERSE
    # No source branch lookup, publishing, requeue, or Outcome mutation API exists.
    assert len(source.mock_calls) == 1
    assert result.inputs.readers[0].evidence is TWO_VERSIONS
    assert SOURCE.outcome is not None
    assert SOURCE.outcome.read_evidence is TWO_VERSIONS


def test_snapshot_uses_retained_evidence_without_issue_or_branch_lookup() -> None:
    source = backend()
    result = build_read_context(request(snapshot=SNAPSHOT), source)
    assert not result.bootstrap
    assert result.inputs is not None
    assert result.inputs.readers[0].validations == (CHECKPOINT,)
    assert result.inputs.readers[0].evidence == TWO_VERSIONS
    source.list_integrated_readers.assert_not_called()
    source.load_snapshot_evidence.assert_called_once_with(ENTRY)


def test_new_parent_requires_verified_absence_of_old_integrations() -> None:
    result = build_read_context(request(), backend())
    assert result.bootstrap and result.inputs is not None
    assert result.inputs.readers == ()
    assert result.snapshot is not None and result.snapshot.readers == ()
    source = backend()
    source.list_integrated_readers.return_value = ReaderHistory(
        readers=(), complete=True, empty_verified=False
    )
    unknown = build_read_context(request(), source)
    assert unknown.inputs is None
    assert unknown.unknowns


@pytest.mark.parametrize("failure", ["partial", "exception", "no_receipt"])
def test_history_failure_never_becomes_empty_fresh(failure: str) -> None:
    source = backend(SOURCE)
    if failure == "exception":
        source.list_integrated_readers.side_effect = OSError("page fetch failed")
    elif failure == "partial":
        source.list_integrated_readers.return_value = ReaderHistory(
            readers=(SOURCE,), complete=False
        )
    else:
        source.list_integrated_readers.return_value = ReaderHistory(
            readers=(replace(SOURCE, proof=None),), complete=True
        )
    result = build_read_context(request(), source)
    assert result.inputs is None
    assert result.snapshot is None
    assert result.coverage is ReadCoverage.UNAVAILABLE
    assert result.unknowns


@pytest.mark.parametrize(
    "change",
    [
        {"claim_id": "claim-other"},
        {"completion_id": "completion-other"},
        {"head_sha": SHA_OTHER},
        {"issue": 99},
        {"result": "blocked", "read_evidence": None},
        {"read_evidence": None},
        {"review": ReviewSummary(reviewed_head_sha=SHA_OTHER)},
        {"read_evidence": replace(TWO_VERSIONS, digest="0" * 64)},
        {"read_evidence": replace(TWO_VERSIONS, repository="other/repo")},
        {"read_evidence": replace(TWO_VERSIONS, claim_id="old-claim")},
    ],
)
def test_outcome_generation_or_evidence_mismatch_is_unknown(
    change: dict[str, object],
) -> None:
    assert SOURCE.outcome is not None
    stale = replace(SOURCE, outcome=replace(SOURCE.outcome, **change))  # type: ignore[arg-type]
    result = build_read_context(request(current_sources=(stale,)), backend())
    assert result.inputs is None
    assert UnknownReason.EVIDENCE_MISMATCH in result.unknowns


@pytest.mark.parametrize(
    "checkpoint", [None, replace(SOURCE.checkpoint, input_base_sha=SHA_OTHER)]
)
def test_missing_or_different_claim_checkpoint_is_unknown(
    checkpoint: ReadCheckpointV1 | None,
) -> None:
    result = build_read_context(
        request(current_sources=(replace(SOURCE, checkpoint=checkpoint),)), backend()
    )
    assert result.inputs is None


@pytest.mark.parametrize(
    "change",
    [
        {"parent_head_sha": SHA_HEAD},
        {"parent_issue_number": 99},
        {"repository": "other/repo"},
        {"digest": "0" * 64},
    ],
)
def test_mismatched_snapshot_is_not_silently_rebootstrapped(
    change: dict[str, object],
) -> None:
    source = backend()
    snapshot = replace(SNAPSHOT, **change)  # type: ignore[arg-type]
    result = build_read_context(request(snapshot=snapshot), source)
    assert result.inputs is None
    assert not result.bootstrap
    assert not source.mock_calls


@pytest.mark.parametrize("failure", [None, "digest", "exception"])
def test_missing_or_corrupt_retained_evidence_is_unknown(failure: str | None) -> None:
    source = backend()
    source.load_snapshot_evidence.return_value = (
        None if failure is None else replace(TWO_VERSIONS, digest="0" * 64)
    )
    if failure == "exception":
        source.load_snapshot_evidence.side_effect = OSError("missing data object")
    result = build_read_context(request(snapshot=SNAPSHOT), source)
    assert result.inputs is None and result.unknowns


def test_snapshot_fetch_failure_cannot_be_confused_with_absence() -> None:
    source = backend()
    result = build_read_context(
        request(snapshot_unknown=UnknownReason.FETCH_FAILED), source
    )
    assert result.inputs is None
    assert result.unknowns == (UnknownReason.FETCH_FAILED,)
    assert not source.mock_calls


def test_partial_read_coverage_is_preserved_for_detection() -> None:
    assert SOURCE.outcome is not None
    partial = replace(
        SOURCE,
        outcome=replace(SOURCE.outcome, read_evidence=PARTIAL_COVERAGE),
        validations=(),
    )
    result = build_read_context(
        request(current_sources=(partial,), writers=()), backend()
    )
    assert result.coverage is ReadCoverage.PARTIAL
    assert result.inputs is not None
    assert detect_stale_reads(result.inputs).verdict is StaleVerdict.UNKNOWN


def test_conflicting_duplicate_reader_is_unknown_but_equal_duplicates_collapse() -> (
    None
):
    duplicate = build_read_context(request(), backend(SOURCE, SOURCE))
    assert duplicate.inputs is not None and len(duplicate.inputs.readers) == 1
    conflict = replace(SOURCE, completion_id="different-generation")
    assert build_read_context(request(), backend(SOURCE, conflict)).inputs is None


def test_current_child_is_combined_without_fabricating_validation() -> None:
    incoming = replace(SOURCE, validations=())
    result = build_read_context(request(current_sources=(incoming,)), backend())
    assert result.inputs is not None
    assert result.inputs.readers[0].validations == ()
    assert result.inputs.readers[0].evidence is TWO_VERSIONS
    # The bootstrap snapshot describes P, not the not-yet-published child at H.
    assert result.snapshot is not None and result.snapshot.readers == ()


def test_unavailable_coverage_is_not_promoted_by_successful_validation() -> None:
    assert SOURCE.outcome is not None
    unavailable = replace(
        SOURCE, outcome=replace(SOURCE.outcome, read_evidence=UNAVAILABLE_EVIDENCE)
    )
    result = build_read_context(
        request(current_sources=(unavailable,), writers=()), backend()
    )
    assert result.coverage is ReadCoverage.UNAVAILABLE
    assert result.inputs is not None
    assert result.inputs.readers[0].validations == (CHECKPOINT,)
    assert detect_stale_reads(result.inputs).verdict is StaleVerdict.UNKNOWN


@pytest.mark.parametrize(
    "evidence_change",
    [{"head_sha": SHA_OTHER}, {"issue_number": 99}, {"claim_id": "wrong-claim"}],
)
def test_snapshot_reader_identity_is_checked_beyond_digest(
    evidence_change: dict[str, object],
) -> None:
    from orchestune.read_dependencies.contracts import seal_evidence

    evidence = seal_evidence(replace(TWO_VERSIONS, **evidence_change))  # type: ignore[arg-type]
    changed_entry = replace(ENTRY, evidence_digest=evidence.digest or "")
    snapshot = seal_snapshot(replace(SNAPSHOT, readers=(changed_entry,)))
    source = backend()
    source.load_snapshot_evidence.return_value = evidence
    result = build_read_context(request(snapshot=snapshot), source)
    assert result.inputs is None


def test_snapshot_and_current_child_combine_without_rewriting_existing_reads() -> None:
    from tests.read_dependency_test_support import evidence

    value = evidence(declared=(), issue_number=11, claim_id=CLAIM_B)
    assert SOURCE.outcome is not None and SOURCE.checkpoint is not None
    incoming = replace(
        SOURCE,
        proof=TaskIntegrationProof(11, "writer", "writer/branch", SHA_HEAD),
        claim_id=CLAIM_B,
        completion_id="completion-writer",
        checkpoint=replace(SOURCE.checkpoint, issue_number=11, claim_id=CLAIM_B),
        outcome=replace(
            SOURCE.outcome,
            issue=11,
            claim_id=CLAIM_B,
            completion_id="completion-writer",
            read_evidence=value,
        ),
        validations=(),
    )
    result = build_read_context(
        request(snapshot=SNAPSHOT, current_sources=(incoming,)), backend()
    )
    assert result.inputs is not None and len(result.inputs.readers) == 2
    assert result.snapshot == SNAPSHOT
    assert result.inputs.readers[0].evidence is TWO_VERSIONS
    assert result.inputs.readers[1].evidence is value
    assert result.inputs.writers == request().writers


def test_invalid_parent_identity_returns_unknown_before_reading_history() -> None:
    source = backend()
    result = build_read_context(request(parent_sha="invalid"), source)
    assert result.inputs is None
    assert not source.mock_calls


def test_historical_integrated_reader_produces_reverse_candidates_without_validations() -> (
    None
):
    historical_source = replace(SOURCE, validations=())
    backend_mock = backend(historical_source)
    req = request()
    result = build_read_context(req, backend_mock)
    assert result.inputs is not None
    assert len(result.inputs.readers) == 1
    reader = result.inputs.readers[0]
    assert reader.integrated is True
    assert reader.validations == ()
    report = detect_stale_reads(result.inputs)
    assert report.candidates
    assert all(c.identity.direction == ReadDirection.REVERSE for c in report.candidates)
