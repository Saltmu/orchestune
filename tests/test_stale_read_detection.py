"""Pure stale-read detection against the frozen v1 inputs."""

from dataclasses import replace
from itertools import permutations

import pytest

from orchestune.read_dependencies.contracts import (
    ChangeKind,
    ExclusionReason,
    ObservationAvailability,
    ReadContextInputs,
    ReadCoverage,
    ReadDirection,
    ReaderIdentity,
    ReaderInput,
    ReadEvidenceV1,
    ReadSource,
    ResourcePresence,
    StaleVerdict,
    UnknownReason,
    ValidationCheckpoint,
    WriterChanges,
    seal_evidence,
    seal_report,
)
from orchestune.read_dependencies.detection import detect_stale_reads
from tests.read_dependency_test_support import (
    BLOB_V1,
    BLOB_V2,
    CLAIM_B,
    EMPTY_DECLARATION,
    MODIFY,
    READER,
    REPOSITORY,
    SHA_BASE,
    SHA_HEAD,
    SHA_OTHER,
    TWO_VERSIONS,
    UNAVAILABLE_EVIDENCE,
    evidence,
    observation,
)

PATH = MODIFY.path
WRITER = WriterChanges(issue_number=11, claim_id=CLAIM_B, changes=(MODIFY,))


def reader(value: ReadEvidenceV1 = TWO_VERSIONS) -> ReaderInput:
    return ReaderInput(identity=READER, evidence=value)


def context(
    readers: tuple[ReaderInput, ...],
    writers: tuple[WriterChanges, ...] = (WRITER,),
) -> ReadContextInputs:
    return ReadContextInputs(
        repository=REPOSITORY,
        parent_issue_number=1230,
        parent_sha=SHA_OTHER,
        candidate_head_sha=SHA_HEAD,
        readers=readers,
        writers=writers,
    )


@pytest.fixture
def forward() -> ReadContextInputs:
    """An incoming reader read v1 before the parent's writer changed it."""
    return context((reader(),))


@pytest.fixture
def same_attempt() -> ReadContextInputs:
    """Both children are incoming readers in the same frozen attempt."""
    writer_evidence = evidence(
        declared=(), issue_number=11, claim_id=CLAIM_B, head_sha=SHA_OTHER
    )
    writer_reader = ReaderInput(
        identity=ReaderIdentity(
            repository=REPOSITORY,
            issue_number=11,
            claim_id=CLAIM_B,
            head_sha=SHA_OTHER,
        ),
        evidence=writer_evidence,
    )
    return context((reader(), writer_reader))


@pytest.fixture
def reverse() -> ReadContextInputs:
    """A retained integrated reader has a validation checkpoint at P."""
    integrated = replace(
        reader(),
        validations=(
            ValidationCheckpoint(candidate_head_sha=SHA_OTHER, report_digest="f" * 64),
        ),
    )
    return context((integrated,))


@pytest.mark.parametrize(
    ("fixture", "direction"),
    [
        ("forward", ReadDirection.FORWARD),
        ("same_attempt", ReadDirection.SAME_ATTEMPT),
        ("reverse", ReadDirection.REVERSE),
    ],
)
def test_three_directions(
    request: pytest.FixtureRequest, fixture: str, direction: ReadDirection
) -> None:
    report = detect_stale_reads(request.getfixturevalue(fixture))
    assert report.verdict is StaleVerdict.CANDIDATE_STALE
    assert {item.identity.direction for item in report.candidates} == {direction}
    assert report.digest == seal_report(report).digest


def test_multiple_versions_and_declared_base_are_not_rewritten(
    forward: ReadContextInputs,
) -> None:
    report = detect_stale_reads(forward)
    # v1 remains even though the same file was later read at v2.
    assert {item.identity.observation.blob_oid for item in report.candidates} == {
        BLOB_V1
    }
    assert len(report.exclusions) == 1
    assert report.exclusions[0].reason is ExclusionReason.CONTENT_UNCHANGED
    submitted = forward.readers[0].evidence
    assert submitted is not None
    assert TWO_VERSIONS.observations == submitted.observations


def test_declared_and_observed_paths_form_a_union() -> None:
    value = evidence(
        declared=("declared.py",), observations=(observation(PATH, BLOB_V1),)
    )
    writer = replace(WRITER, changes=(MODIFY, replace(MODIFY, path="declared.py")))
    report = detect_stale_reads(context((reader(value),), (writer,)))
    assert {item.identity.observation.path for item in report.candidates} == {
        PATH,
        "declared.py",
    }
    declared = next(
        item.identity.observation
        for item in report.candidates
        if item.identity.observation.path == "declared.py"
    )
    assert declared.snapshot_commit == SHA_BASE
    assert declared.source is ReadSource.DECLARED_ONLY


def test_same_path_declaration_preserves_unobserved_start_version() -> None:
    value = evidence(declared=(PATH,), observations=(observation(PATH, BLOB_V2),))
    report = detect_stale_reads(context((reader(value),)))
    # An observation at the base resolves that declaration; a later one cannot.
    assert report.verdict is StaleVerdict.FRESH
    later = replace(
        value, observations=(observation(PATH, BLOB_V2, snapshot=SHA_OTHER),)
    )
    report = detect_stale_reads(context((reader(seal_evidence(later)),)))
    assert report.verdict is StaleVerdict.CANDIDATE_STALE
    assert report.candidates[0].identity.observation.snapshot_commit == SHA_BASE


def test_self_writes_do_not_hide_other_tasks_on_the_same_path() -> None:
    own = replace(WRITER, issue_number=READER.issue_number, claim_id=READER.claim_id)
    value = evidence(declared=(), observations=(observation(PATH, BLOB_V1),))
    own_report = detect_stale_reads(context((reader(value),), (own,)))
    assert own_report.verdict is StaleVerdict.FRESH
    assert own_report.exclusions[0].reason is ExclusionReason.READER_OWN_WRITE
    report = detect_stale_reads(context((reader(value),), (own, WRITER)))
    assert len(report.candidates) == 1
    assert report.candidates[0].identity.writer_issue_number == 11


def test_same_issue_different_claim_is_another_writer() -> None:
    writer = replace(WRITER, issue_number=READER.issue_number)
    report = detect_stale_reads(context((reader(),), (writer,)))
    assert report.candidates


def test_unrelated_changes_and_explicit_empty_reads_are_fresh() -> None:
    unrelated = replace(WRITER, changes=(replace(MODIFY, path="other.py"),))
    assert (
        detect_stale_reads(context((reader(),), (unrelated,))).verdict
        is StaleVerdict.FRESH
    )
    assert (
        detect_stale_reads(context((reader(EMPTY_DECLARATION),))).verdict
        is StaleVerdict.FRESH
    )
    assert detect_stale_reads(context(())).verdict is StaleVerdict.FRESH


@pytest.mark.parametrize("kind", list(ChangeKind))
def test_all_change_kinds(kind: ChangeKind) -> None:
    change = MODIFY
    obs = observation(PATH, BLOB_V1)
    if kind is ChangeKind.ADD:
        change = replace(change, kind=kind, old_blob_oid=None, old_file_mode=None)
        obs = observation(PATH, None, presence=ResourcePresence.ABSENT)
    elif kind is ChangeKind.DELETE:
        change = replace(change, kind=kind, new_blob_oid=None, new_file_mode=None)
    elif kind is ChangeKind.RENAME:
        change = replace(change, kind=kind, old_path="old.py")
        obs = observation("old.py", BLOB_V2)
    elif kind is ChangeKind.MODE:
        change = replace(
            change, kind=kind, new_blob_oid=BLOB_V1, new_file_mode="100755"
        )
    value = evidence(declared=(), observations=(obs,))
    report = detect_stale_reads(
        context((reader(value),), (replace(WRITER, changes=(change,)),))
    )
    assert len(report.candidates) == 1


def test_rename_destination_and_symlink_mode_change() -> None:
    rename = replace(MODIFY, kind=ChangeKind.RENAME, old_path="old.py")
    mode = replace(
        MODIFY, kind=ChangeKind.MODE, new_blob_oid=BLOB_V1, new_file_mode="120000"
    )
    value = evidence(declared=(), observations=(observation(PATH, BLOB_V1),))
    for change in (rename, mode):
        report = detect_stale_reads(
            context((reader(value),), (replace(WRITER, changes=(change,)),))
        )
        assert report.candidates


@pytest.mark.parametrize("coverage", [ReadCoverage.PARTIAL, ReadCoverage.UNAVAILABLE])
def test_incomplete_coverage_cannot_be_fresh(coverage: ReadCoverage) -> None:
    value = (
        UNAVAILABLE_EVIDENCE
        if coverage is ReadCoverage.UNAVAILABLE
        else evidence(declared=(), coverage=coverage)
    )
    report = detect_stale_reads(context((reader(value),), ()))
    assert report.coverage is coverage
    assert report.verdict is StaleVerdict.UNKNOWN
    assert report.unknowns


def test_candidates_do_not_hide_unobserved_partial_interval() -> None:
    value = evidence(
        declared=(),
        observations=(observation(PATH, BLOB_V1),),
        coverage=ReadCoverage.PARTIAL,
    )
    writer = replace(WRITER, changes=(MODIFY, replace(MODIFY, path="not-observed.py")))
    report = detect_stale_reads(context((reader(value),), (writer,)))
    assert report.candidates
    assert report.coverage is ReadCoverage.PARTIAL
    assert any(item.path == "not-observed.py" for item in report.unknowns)


@pytest.mark.parametrize(
    "reason",
    [
        UnknownReason.OBJECT_MISSING,
        UnknownReason.SHALLOW_HISTORY,
        UnknownReason.FETCH_FAILED,
    ],
)
def test_unresolved_changes_are_unknown(reason: UnknownReason) -> None:
    change = replace(
        MODIFY, availability=ObservationAvailability.UNKNOWN, unknown_reason=reason
    )
    report = detect_stale_reads(
        context((reader(),), (replace(WRITER, changes=(change,)),))
    )
    assert not report.candidates
    assert reason in {item.reason for item in report.unknowns}


def test_missing_evidence_unknown_observation_and_unknown_writer() -> None:
    missing = replace(reader(), evidence=None)
    report = detect_stale_reads(context((missing,)))
    assert report.verdict is StaleVerdict.UNKNOWN
    assert report.readers[0].evidence_digest is None
    value = evidence(
        declared=(),
        observations=(observation(PATH, None, presence=ResourcePresence.UNKNOWN),),
    )
    report = detect_stale_reads(context((reader(value),)))
    assert report.verdict is StaleVerdict.UNKNOWN
    unknown_writer = replace(WRITER, issue_number=None, claim_id=None)
    report = detect_stale_reads(context((reader(),), (unknown_writer,)))
    assert not report.candidates
    assert UnknownReason.WRITER_UNKNOWN in {item.reason for item in report.unknowns}


def test_forged_digest_is_not_silently_resealed() -> None:
    report = detect_stale_reads(
        context((reader(replace(TWO_VERSIONS, digest="0" * 64)),))
    )
    assert report.verdict is StaleVerdict.UNKNOWN
    assert UnknownReason.EVIDENCE_MISMATCH in {item.reason for item in report.unknowns}


def test_checkpoint_does_not_erase_original_reads(reverse: ReadContextInputs) -> None:
    report = detect_stale_reads(reverse)
    integrated = reverse.readers[0]
    current_checkpoint = ValidationCheckpoint(
        candidate_head_sha=SHA_HEAD, report_digest=report.digest or ""
    )
    revised = replace(
        reverse,
        readers=(
            replace(
                integrated, validations=(*integrated.validations, current_checkpoint)
            ),
        ),
    )
    assert detect_stale_reads(revised) == report


def test_input_order_and_duplicates_do_not_change_report(
    same_attempt: ReadContextInputs,
) -> None:
    own = replace(WRITER, issue_number=10, claim_id=READER.claim_id)
    expected = detect_stale_reads(replace(same_attempt, writers=(WRITER, own)))
    for readers in permutations(same_attempt.readers):
        for writers in permutations((WRITER, own, WRITER)):
            assert (
                detect_stale_reads(
                    replace(same_attempt, readers=readers, writers=writers)
                )
                == expected
            )
    changed = replace(WRITER, changes=(replace(MODIFY, new_blob_oid="3" * 40),))
    other = detect_stale_reads(replace(same_attempt, writers=(changed, own)))
    assert other.digest != expected.digest
    assert other.candidates[0].candidate_id != expected.candidates[0].candidate_id
