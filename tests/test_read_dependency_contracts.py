"""Contract tests for the stale-read v1 types."""

from __future__ import annotations

import ast
import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from read_dependency_test_support import (
    ABSENT_READ,
    BLOB_V1,
    BLOB_V2,
    EMPTY_DECLARATION,
    FORWARD_CANDIDATE_ID,
    FORWARD_VERDICT,
    MODIFY,
    PARTIAL_COVERAGE,
    READER,
    REPOSITORY,
    SHA_BASE,
    SHA_HEAD,
    SHA_OTHER,
    TWO_VERSIONS,
    UNAVAILABLE_EVIDENCE,
    UNKNOWN_PRESENCE,
    UNSPECIFIED_DECLARATION,
    evidence,
    observation,
)

from orchestune.read_dependencies.contracts import (
    CandidateConclusion,
    CandidateIdentity,
    CandidateJudgment,
    ChangedResource,
    ChangeKind,
    CheckpointAvailability,
    GateReason,
    ObservationAvailability,
    ReadCoverage,
    ReadDirection,
    ReadGateInputs,
    ReadSource,
    ReadValidationV1,
    ResourcePresence,
    StaleReadGate,
    StaleVerdict,
    aggregate_stale_verdict,
    candidate_id,
    content_unchanged,
    decide_read_gate,
    declared_dependency_observation,
    normalize_read_path,
    read_evidence_digest_input,
    seal_evidence,
)

_SCAFFOLDS = (
    "orchestune/read_dependencies/__init__.py",
    "orchestune/complete/read_evidence.py",
    "orchestune/integrator/read_gate.py",
    "orchestune/integrator/read_publish.py",
    "orchestune/dispatch/semantic_review/read_context.py",
)


def test_unspecified_declaration_is_distinct_from_empty() -> None:
    assert UNSPECIFIED_DECLARATION.declared_reads is None
    assert EMPTY_DECLARATION.declared_reads == ()
    assert UNSPECIFIED_DECLARATION != EMPTY_DECLARATION
    assert UNSPECIFIED_DECLARATION.digest != EMPTY_DECLARATION.digest
    assert read_evidence_digest_input(UNSPECIFIED_DECLARATION)["declared_reads"] is None
    assert read_evidence_digest_input(EMPTY_DECLARATION)["declared_reads"] == []


def test_same_path_keeps_distinct_versions_and_ignores_order() -> None:
    assert [item.blob_oid for item in TWO_VERSIONS.observations] == [BLOB_V1, BLOB_V2]
    reversed_obs = evidence(
        declared=("src/registry.py",),
        observations=(
            observation("src/registry.py", BLOB_V1),
            observation("src/registry.py", BLOB_V2, snapshot=SHA_OTHER),
        ),
    )
    assert reversed_obs == TWO_VERSIONS
    assert reversed_obs.digest == TWO_VERSIONS.digest


def test_absent_is_distinct_from_unknown_presence() -> None:
    assert ABSENT_READ.observations[0].presence is ResourcePresence.ABSENT
    assert UNKNOWN_PRESENCE.observations[0].presence is ResourcePresence.UNKNOWN
    assert ABSENT_READ.observations[0].blob_oid is None
    assert UNKNOWN_PRESENCE.observations[0].blob_oid is None
    assert ABSENT_READ != UNKNOWN_PRESENCE


def test_partial_and_unavailable_coverage_are_distinct() -> None:
    assert PARTIAL_COVERAGE.coverage is ReadCoverage.PARTIAL
    assert UNAVAILABLE_EVIDENCE.coverage is ReadCoverage.UNAVAILABLE
    assert UNAVAILABLE_EVIDENCE.source is ReadSource.UNAVAILABLE
    assert UNAVAILABLE_EVIDENCE.declared_reads is None
    assert PARTIAL_COVERAGE != UNAVAILABLE_EVIDENCE


def test_reader_claim_and_target_head_are_independent() -> None:
    other_head = evidence(declared=(), head_sha=SHA_OTHER)
    other_claim = evidence(declared=(), claim_id="claim-next")
    assert other_head.claim_id == EMPTY_DECLARATION.claim_id
    assert other_head.head_sha != EMPTY_DECLARATION.head_sha
    assert other_claim.claim_id != EMPTY_DECLARATION.claim_id
    assert other_head.digest != EMPTY_DECLARATION.digest
    assert other_claim.digest != EMPTY_DECLARATION.digest
    assert READER.claim_id != READER.head_sha


def test_digest_excludes_itself_and_is_stable() -> None:
    sealed = seal_evidence(EMPTY_DECLARATION)
    assert sealed.digest == EMPTY_DECLARATION.digest
    assert "digest" not in read_evidence_digest_input(sealed)
    assert sealed.digest is not None
    assert len(sealed.digest) == 64


def test_read_paths_are_concrete_repository_files() -> None:
    assert normalize_read_path("src\\registry.py") == "src/registry.py"
    assert normalize_read_path("./src/registry.py") == "src/registry.py"
    for path in (
        "/src/registry.py",
        "C:/src/registry.py",
        "src/../registry.py",
        "src/*.py",
        "a\nb",
    ):
        with pytest.raises(ValueError):
            normalize_read_path(path)
    with pytest.raises(ValueError):
        normalize_read_path(1)  # type: ignore[arg-type]


def test_candidate_id_changes_with_the_read_version() -> None:
    first = _identity(BLOB_V1)
    second = _identity(BLOB_V2)
    assert candidate_id(first) == FORWARD_CANDIDATE_ID
    assert candidate_id(first) == candidate_id(first)
    assert candidate_id(second) != candidate_id(first)


def test_rename_away_is_not_content_unchanged() -> None:
    renamed = ChangedResource(
        path="src/new.py",
        old_path="src/registry.py",
        kind=ChangeKind.RENAME,
        old_blob_oid=BLOB_V1,
        new_blob_oid=BLOB_V1,
        old_file_mode="100644",
        new_file_mode="100644",
        evidence_commits=(SHA_OTHER,),
        availability=ObservationAvailability.RESOLVED,
    )
    seen = observation("src/registry.py", BLOB_V1)
    assert content_unchanged(seen, renamed) is False
    assert content_unchanged(seen, MODIFY) is False
    same = ChangedResource(
        path="src/registry.py",
        old_path=None,
        kind=ChangeKind.MODIFY,
        old_blob_oid=BLOB_V2,
        new_blob_oid=BLOB_V1,
        old_file_mode="100644",
        new_file_mode="100644",
        evidence_commits=(SHA_OTHER,),
        availability=ObservationAvailability.RESOLVED,
    )
    assert content_unchanged(seen, same) is True


def test_declared_dependency_does_not_pin_a_blob() -> None:
    declared = declared_dependency_observation("src/registry.py", SHA_BASE)
    assert declared.presence is ResourcePresence.UNKNOWN
    assert declared.snapshot_commit == SHA_BASE
    assert content_unchanged(declared, MODIFY) is False


def test_aggregate_verdict_rules() -> None:
    assert FORWARD_VERDICT is StaleVerdict.CANDIDATE_STALE
    assert (
        aggregate_stale_verdict(
            candidate_count=0, unknown_count=0, coverage=ReadCoverage.COMPLETE
        )
        is StaleVerdict.FRESH
    )
    assert (
        aggregate_stale_verdict(
            candidate_count=0, unknown_count=0, coverage=ReadCoverage.PARTIAL
        )
        is StaleVerdict.UNKNOWN
    )
    assert (
        aggregate_stale_verdict(
            candidate_count=0, unknown_count=1, coverage=ReadCoverage.COMPLETE
        )
        is StaleVerdict.UNKNOWN
    )
    assert (
        aggregate_stale_verdict(
            candidate_count=1, unknown_count=1, coverage=ReadCoverage.PARTIAL
        )
        is StaleVerdict.CANDIDATE_STALE
    )
    assert StaleVerdict.CONFIRMED_STALE not in {
        aggregate_stale_verdict(
            candidate_count=count, unknown_count=0, coverage=ReadCoverage.COMPLETE
        )
        for count in (0, 1)
    }


def test_required_gate_blocks_fast_path_until_validation_binds() -> None:
    fresh = decide_read_gate(
        ReadGateInputs(
            mode=StaleReadGate.REQUIRED,
            verdict=StaleVerdict.FRESH,
            coverage=ReadCoverage.COMPLETE,
            candidate_head_sha=SHA_HEAD,
            report_digest="e" * 64,
        )
    )
    assert fresh.allow_fast_path is True
    assert fresh.reason is GateReason.FRESH
    pending = decide_read_gate(
        ReadGateInputs(
            mode=StaleReadGate.REQUIRED,
            verdict=StaleVerdict.CANDIDATE_STALE,
            coverage=ReadCoverage.COMPLETE,
            candidate_head_sha=SHA_HEAD,
            report_digest="e" * 64,
            candidate_ids=(FORWARD_CANDIDATE_ID,),
        )
    )
    assert pending.allow_fast_path is False
    assert pending.allow_parent_update is False
    accepted = decide_read_gate(
        ReadGateInputs(
            mode=StaleReadGate.REQUIRED,
            verdict=StaleVerdict.CANDIDATE_STALE,
            coverage=ReadCoverage.COMPLETE,
            candidate_head_sha=SHA_HEAD,
            report_digest="e" * 64,
            candidate_ids=(FORWARD_CANDIDATE_ID,),
            validation=_validation(FORWARD_CANDIDATE_ID),
        )
    )
    assert accepted.allow_fast_path is False
    assert accepted.allow_parent_update is True
    assert accepted.reason is GateReason.VALIDATION_ACCEPTED


def test_required_gate_rejects_coverage_gap_and_unknown_judgment() -> None:
    gap = decide_read_gate(
        ReadGateInputs(
            mode=StaleReadGate.REQUIRED,
            verdict=StaleVerdict.CANDIDATE_STALE,
            coverage=ReadCoverage.PARTIAL,
            candidate_head_sha=SHA_HEAD,
            report_digest="e" * 64,
            candidate_ids=(FORWARD_CANDIDATE_ID,),
            validation=_validation(FORWARD_CANDIDATE_ID),
        )
    )
    assert gap.reason is GateReason.COVERAGE_GAP
    assert gap.allow_parent_update is False
    unknown = decide_read_gate(
        ReadGateInputs(
            mode=StaleReadGate.REQUIRED,
            verdict=StaleVerdict.CANDIDATE_STALE,
            coverage=ReadCoverage.COMPLETE,
            candidate_head_sha=SHA_HEAD,
            report_digest="e" * 64,
            candidate_ids=(FORWARD_CANDIDATE_ID,),
            validation=_validation(FORWARD_CANDIDATE_ID, CandidateConclusion.UNKNOWN),
        )
    )
    assert unknown.reason is GateReason.VALIDATION_INCOMPLETE
    advisory = decide_read_gate(
        ReadGateInputs(
            mode=StaleReadGate.ADVISORY,
            verdict=StaleVerdict.UNKNOWN,
            coverage=ReadCoverage.UNAVAILABLE,
            candidate_head_sha=SHA_HEAD,
            report_digest=None,
        )
    )
    assert advisory.allow_parent_update is True
    assert advisory.reason is GateReason.ADVISORY


def test_unavailable_checkpoint_does_not_invent_a_sha() -> None:
    from orchestune.read_dependencies.contracts import ReadCheckpointV1

    missing = ReadCheckpointV1(
        repository=REPOSITORY,
        issue_number=10,
        claim_id="claim-old",
        availability=CheckpointAvailability.UNAVAILABLE,
        input_base_sha=None,
    )
    assert missing.input_base_sha is None
    with pytest.raises(ValueError):
        ReadCheckpointV1(
            repository=REPOSITORY,
            issue_number=10,
            claim_id="claim-old",
            availability=CheckpointAvailability.UNAVAILABLE,
            input_base_sha=SHA_BASE,
        )


def test_snapshot_reader_order_is_independent_of_hash_seed() -> None:
    script = """
from orchestune.read_dependencies.contracts import (
    ParentReadSnapshotV1,
    ReaderSnapshotEntry,
    ValidationCheckpoint,
    seal_snapshot,
)
head = "b" * 40
base = "a" * 40
digest = "a" * 64
report = "e" * 64
later = ReaderSnapshotEntry(
    issue_number=10,
    claim_id="claim-a",
    completion_id="completion-b",
    source_sha=head,
    evidence_digest=digest,
    validations=(
        ValidationCheckpoint(candidate_head_sha=head, report_digest=report),
    ),
)
earlier = ReaderSnapshotEntry(
    issue_number=10,
    claim_id="claim-a",
    completion_id="completion-a",
    source_sha=base,
    evidence_digest=digest,
)
snapshot = seal_snapshot(
    ParentReadSnapshotV1(
        repository="example/repo",
        parent_issue_number=1230,
        parent_head_sha=head,
        readers=(later, earlier),
    )
)
print(",".join(item.completion_id for item in snapshot.readers))
print(snapshot.digest)
"""
    observed: list[str] = []
    for seed in ("1", "2"):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        assert completed.returncode == 0, completed.stderr
        observed.append(completed.stdout.strip())
    assert observed[0] == observed[1]
    assert observed[0].splitlines()[0] == "completion-a,completion-b"


def test_scaffold_modules_are_docstrings_without_functions() -> None:
    for relative in _SCAFFOLDS:
        source = Path(relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        assert len(tree.body) == 1
        assert isinstance(tree.body[0], ast.Expr)
        module_name = relative.removesuffix(".py").replace("/", ".")
        module = importlib.import_module(module_name)
        assert module.__doc__
        assert re.search(r"#17\d\d", module.__doc__)


def _identity(blob: str) -> CandidateIdentity:
    return CandidateIdentity(
        repository=REPOSITORY,
        parent_issue_number=1230,
        direction=ReadDirection.FORWARD,
        reader=READER,
        evidence_digest=TWO_VERSIONS.digest or "",
        observation=observation("src/registry.py", blob),
        change=MODIFY,
        writer_issue_number=11,
        writer_claim_id="claim-writer",
    )


def _validation(
    candidate: str, conclusion: CandidateConclusion = CandidateConclusion.COMPATIBLE
) -> ReadValidationV1:
    return ReadValidationV1(
        round_number=1,
        head_sha=SHA_HEAD,
        report_digest="e" * 64,
        decisions=(
            CandidateJudgment(
                candidate_id=candidate,
                conclusion=conclusion,
                rationale="reviewed",
            ),
        ),
    )
