"""Frozen stale-read contract (schema v1).

Later issues implement codecs, Git observation, detection, and gates against
these types. Digest inputs, candidate ids, and the verdict and gate rules
below are the decisions those issues must not reopen.

Path rule: a read path is a concrete repository-relative POSIX file. Backslashes
are normalized to slashes. Absolute paths, drive prefixes, ``..``, empty
segments, globs, and control characters are rejected. ``normalize_footprint_path``
is intentionally not used.

Digest rule: SHA-256 over UTF-8 JSON with ``sort_keys=True``, separators
``,`` and ``:``, and ``ensure_ascii=False``. The digest field, timestamps, and
local paths are not part of the payload. ``None`` stays JSON null, so an
unspecified declaration is not an empty list.

Candidate id: ``sr1-`` plus that digest of ``candidate_digest_input``. The same
path keeps one id per distinct observation and change. ``confirmed_stale`` is a
review result and is never a detection verdict.

``declared_reads is None`` means unspecified. ``()`` means an explicit empty
set. A declared path with no observation is
``declared_dependency_observation``: presence ``unknown`` at ``input_base_sha``,
which cannot be proved content-unchanged.

Report coverage other than ``complete`` cannot authorize a required parent
update. ``off`` and ``advisory`` never block. Required fresh coverage allows
the fast path. A required candidate blocks the fast path; parent update is
allowed only when a validation block binds the same head and report digest and
judges every candidate ``compatible`` or ``repaired``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

READ_CONTRACT_VERSION = 1
READ_CHECKPOINT_MARKER = "orchestune-read-checkpoint"
READ_VALIDATION_MARKER = "orchestune-read-validation"
READ_REPORT_MARKER = "orchestune-read-report"
READ_REPORT_CHUNK_BODY_BYTES = 48000
CANDIDATE_ID_PREFIX = "sr1-"
GIT_FILE_MODES = frozenset({"100644", "100755", "120000", "040000", "160000"})

_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_REPOSITORY = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
_CLAIM_ID = re.compile(r"[A-Za-z0-9._:-]+")
_GLOB = re.compile(r"[*?\[\]]")


class ReadSource(StrEnum):
    """How a read was obtained. Self-report is not an adapter observation."""

    ADAPTER_OBSERVED = "adapter_observed"
    AGENT_REPORTED = "agent_reported"
    DECLARED_ONLY = "declared_only"
    UNAVAILABLE = "unavailable"


class ReadCoverage(StrEnum):
    """How much of the read set was captured. Partial is not fresh."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class ResourcePresence(StrEnum):
    """Whether the blob at a snapshot is known. Absent is not unknown."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class ChangeKind(StrEnum):
    """Git change kept for intersection. Mode-only is not a content modify."""

    ADD = "add"
    DELETE = "delete"
    MODIFY = "modify"
    RENAME = "rename"
    MODE = "mode"


class ObservationAvailability(StrEnum):
    """Whether a Git fact was resolved. Unknown is not an empty change."""

    RESOLVED = "resolved"
    UNKNOWN = "unknown"


class ReadDirection(StrEnum):
    """Which comparison produced a candidate."""

    FORWARD = "forward"
    SAME_ATTEMPT = "same_attempt"
    REVERSE = "reverse"


class StaleVerdict(StrEnum):
    """Detection or review outcome. Detection never emits confirmed_stale."""

    FRESH = "fresh"
    CANDIDATE_STALE = "candidate_stale"
    CONFIRMED_STALE = "confirmed_stale"
    UNKNOWN = "unknown"


class ExclusionReason(StrEnum):
    """Why an intersecting change is not a candidate."""

    CONTENT_UNCHANGED = "content_unchanged"
    READER_OWN_WRITE = "reader_own_write"


class UnknownReason(StrEnum):
    """Why a comparison cannot be decided. These are not empty reads."""

    WRITER_UNKNOWN = "writer_unknown"
    OBJECT_MISSING = "object_missing"
    SHALLOW_HISTORY = "shallow_history"
    FETCH_FAILED = "fetch_failed"
    EVIDENCE_MISMATCH = "evidence_mismatch"
    COVERAGE_PARTIAL = "coverage_partial"
    COVERAGE_UNAVAILABLE = "coverage_unavailable"
    PARENT_SNAPSHOT_MISMATCH = "parent_snapshot_mismatch"


class CandidateConclusion(StrEnum):
    """Reviewer judgment for one candidate id. Unknown is not a pass."""

    COMPATIBLE = "compatible"
    REPAIRED = "repaired"
    UNKNOWN = "unknown"


class StaleReadGate(StrEnum):
    """Gate mode. Default advisory and config wiring belong to issue #1767.

    ``required`` is valid only when semantic review mode is required and review
    is enabled. This module does not read that configuration.
    """

    ADVISORY = "advisory"
    REQUIRED = "required"
    OFF = "off"


class CheckpointAvailability(StrEnum):
    """Whether the claim start sha was stored. Missing is not guessed."""

    RECORDED = "recorded"
    UNAVAILABLE = "unavailable"


class GateReason(StrEnum):
    """Why a gate allows or blocks. Advisory reasons do not block."""

    GATE_OFF = "gate_off"
    ADVISORY = "advisory"
    FRESH = "fresh"
    CANDIDATE_PENDING = "candidate_pending"
    VALIDATION_INCOMPLETE = "validation_incomplete"
    VALIDATION_ACCEPTED = "validation_accepted"
    COVERAGE_GAP = "coverage_gap"
    UNKNOWN = "unknown"
    CONFIRMED_STALE = "confirmed_stale"
    INPUT_CHANGED = "input_changed"


def normalize_read_path(path: object) -> str:
    """Return the canonical relative POSIX path, or reject it.

    Windows separators are converted before the concrete-file checks. This is
    stricter than footprint normalization and does not collapse ``..``.
    """
    if not isinstance(path, str):
        raise ValueError("read path must be a string")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError("read path contains a control character")
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized.endswith("/"):
        raise ValueError("read path must be a non-empty file path")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError("read path must be repository-relative")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("read path must not escape the repository")
    if any(_GLOB.search(part) for part in parts):
        raise ValueError("read path must be a concrete file path")
    return normalized


def canonical_json_sha256(payload: object) -> str:
    """SHA-256 hex of the canonical digest JSON for ``payload``."""
    encoded = json.dumps(
        _json_ready(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parent_read_snapshot_ref(parent_issue_number: int) -> str:
    """Git ref that stores one parent issue's reader snapshot."""
    _require_issue_number(parent_issue_number)
    return f"refs/orchestune/read-snapshots/parent-issue-{parent_issue_number}"


def declared_dependency_observation(
    path: str, input_base_sha: str
) -> ReadObservationV1:
    """Declared path pinned to the claim base without a blob."""
    return ReadObservationV1(
        path=path,
        snapshot_commit=input_base_sha,
        presence=ResourcePresence.UNKNOWN,
        blob_oid=None,
        file_mode=None,
        source=ReadSource.DECLARED_ONLY,
    )


def change_paths(change: ChangedResource) -> frozenset[str]:
    """Paths a change can intersect, including a rename's old path."""
    if change.old_path is None:
        return frozenset({change.path})
    return frozenset({change.path, change.old_path})


def content_unchanged(observation: ReadObservationV1, change: ChangedResource) -> bool:
    """True only when the recorded blob and mode equal the change's new side.

    A rename away from the observed path is not unchanged: the read path is
    gone even if the destination blob matches. Deletes and unresolved objects
    are not unchanged.
    """
    if change.kind is ChangeKind.RENAME and observation.path == change.old_path:
        return False
    if observation.presence is not ResourcePresence.PRESENT:
        return False
    if change.availability is not ObservationAvailability.RESOLVED:
        return False
    if change.kind is ChangeKind.DELETE:
        return False
    return (
        observation.blob_oid == change.new_blob_oid
        and observation.file_mode == change.new_file_mode
    )


def is_reader_own_write(
    reader: ReaderIdentity, writer_issue_number: int | None, writer_claim_id: str | None
) -> bool:
    """True when the change is from this reader. An unknown writer is not."""
    return (
        writer_issue_number == reader.issue_number
        and writer_claim_id == reader.claim_id
    )


def aggregate_stale_verdict(
    *, candidate_count: int, unknown_count: int, coverage: ReadCoverage
) -> StaleVerdict:
    """Detection verdict. Never returns ``confirmed_stale``.

    Any candidate is ``candidate_stale`` and the unknown items stay on the
    report. With no candidate, partial or unavailable coverage or any unknown
    item is ``unknown``. Fresh requires complete coverage and zero unknowns.
    """
    _require_count(candidate_count, "candidate_count")
    _require_count(unknown_count, "unknown_count")
    if candidate_count:
        return StaleVerdict.CANDIDATE_STALE
    if unknown_count or coverage is not ReadCoverage.COMPLETE:
        return StaleVerdict.UNKNOWN
    return StaleVerdict.FRESH


def candidate_digest_input(identity: CandidateIdentity) -> dict[str, Any]:
    """Canonical payload whose digest, prefixed with ``sr1-``, is the id."""
    return {
        "schema": "stale-read-candidate-v1",
        "repository": identity.repository,
        "parent_issue_number": identity.parent_issue_number,
        "direction": identity.direction.value,
        "reader": _reader_payload(identity.reader),
        "evidence_digest": identity.evidence_digest,
        "observation": _observation_payload(identity.observation),
        "change": _change_payload(identity.change),
        "writer_issue_number": identity.writer_issue_number,
        "writer_claim_id": identity.writer_claim_id,
    }


def candidate_id(identity: CandidateIdentity) -> str:
    """Deterministic id for one observation and one intersecting change."""
    return CANDIDATE_ID_PREFIX + canonical_json_sha256(candidate_digest_input(identity))


def read_evidence_digest_input(evidence: ReadEvidenceV1) -> dict[str, Any]:
    """Payload hashed into ``ReadEvidenceV1.digest``. The digest is omitted."""
    declared: list[str] | None
    if evidence.declared_reads is None:
        declared = None
    else:
        declared = list(evidence.declared_reads)
    return {
        "schema": "read-evidence-v1",
        "version": evidence.version,
        "repository": evidence.repository,
        "issue_number": evidence.issue_number,
        "claim_id": evidence.claim_id,
        "input_base_sha": evidence.input_base_sha,
        "head_sha": evidence.head_sha,
        "declared_reads": declared,
        "observations": [_observation_payload(item) for item in evidence.observations],
        "source": evidence.source.value,
        "coverage": evidence.coverage.value,
    }


def seal_evidence(evidence: ReadEvidenceV1) -> ReadEvidenceV1:
    """Return ``evidence`` with the digest of its canonical payload."""
    digest = canonical_json_sha256(read_evidence_digest_input(evidence))
    return replace(evidence, digest=digest)


def read_report_digest_input(report: StaleReadReportV1) -> dict[str, Any]:
    """Payload hashed into ``StaleReadReportV1.digest``."""
    return {
        "schema": "stale-read-report-v1",
        "version": report.version,
        "repository": report.repository,
        "parent_issue_number": report.parent_issue_number,
        "parent_sha": report.parent_sha,
        "candidate_head_sha": report.candidate_head_sha,
        "readers": [_reader_ref_payload(item) for item in report.readers],
        "candidates": [_candidate_payload(item) for item in report.candidates],
        "exclusions": [_exclusion_payload(item) for item in report.exclusions],
        "unknowns": [_unknown_payload(item) for item in report.unknowns],
        "coverage": report.coverage.value,
        "verdict": report.verdict.value,
    }


def seal_report(report: StaleReadReportV1) -> StaleReadReportV1:
    """Return ``report`` with the digest of its canonical payload."""
    return replace(
        report, digest=canonical_json_sha256(read_report_digest_input(report))
    )


def read_snapshot_digest_input(snapshot: ParentReadSnapshotV1) -> dict[str, Any]:
    """Payload hashed into ``ParentReadSnapshotV1.digest``."""
    return {
        "schema": "parent-read-snapshot-v1",
        "version": snapshot.version,
        "repository": snapshot.repository,
        "parent_issue_number": snapshot.parent_issue_number,
        "parent_head_sha": snapshot.parent_head_sha,
        "readers": [_snapshot_reader_payload(item) for item in snapshot.readers],
    }


def seal_snapshot(snapshot: ParentReadSnapshotV1) -> ParentReadSnapshotV1:
    """Return ``snapshot`` with the digest of its canonical payload."""
    digest = canonical_json_sha256(read_snapshot_digest_input(snapshot))
    return replace(snapshot, digest=digest)


def decide_read_gate(inputs: ReadGateInputs) -> ReadGateDecision:
    """Map a report and optional validation block to fast-path and publish flags."""
    if inputs.mode is StaleReadGate.OFF:
        return _gate(
            inputs, GateReason.GATE_OFF, allow_fast_path=True, allow_parent=True
        )
    if inputs.mode is StaleReadGate.ADVISORY:
        return _gate(
            inputs, GateReason.ADVISORY, allow_fast_path=True, allow_parent=True
        )
    return _decide_required(inputs)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadObservationV1:
    """One file version read at one snapshot commit."""

    path: str
    snapshot_commit: str | None
    presence: ResourcePresence
    blob_oid: str | None
    file_mode: str | None
    source: ReadSource

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_read_path(self.path))
        _check_observation(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadEvidenceV1:
    """Read evidence bound to one repository, claim, and reviewed head.

    ``declared_reads is None`` is unspecified. ``()`` is explicit and empty.
    Observations stay unique by their full identity, so two versions of one
    path both remain. ``digest`` is omitted until ``seal_evidence``.
    """

    version: int = READ_CONTRACT_VERSION
    repository: str
    issue_number: int
    claim_id: str
    input_base_sha: str
    head_sha: str
    declared_reads: tuple[str, ...] | None
    observations: tuple[ReadObservationV1, ...] = ()
    source: ReadSource
    coverage: ReadCoverage
    digest: str | None = None

    def __post_init__(self) -> None:
        _require_version(self.version)
        _require_repository(self.repository)
        _require_issue_number(self.issue_number)
        _require_claim_id(self.claim_id)
        _require_sha(self.input_base_sha, "input_base_sha")
        _require_sha(self.head_sha, "head_sha")
        object.__setattr__(self, "declared_reads", _canon_paths(self.declared_reads))
        object.__setattr__(self, "observations", _canon_observations(self.observations))
        _check_evidence_source(self)
        _require_optional_digest(self.digest)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReaderIdentity:
    """One reader generation. ``claim_id`` is not ``head_sha``."""

    repository: str
    issue_number: int
    claim_id: str
    head_sha: str

    def __post_init__(self) -> None:
        _require_repository(self.repository)
        _require_issue_number(self.issue_number)
        _require_claim_id(self.claim_id)
        _require_sha(self.head_sha, "head_sha")


@dataclass(frozen=True, slots=True, kw_only=True)
class ChangedResource:
    """One Git change. Unresolved facts keep ``availability=unknown``."""

    path: str
    old_path: str | None
    kind: ChangeKind
    old_blob_oid: str | None
    new_blob_oid: str | None
    old_file_mode: str | None
    new_file_mode: str | None
    evidence_commits: tuple[str, ...]
    availability: ObservationAvailability
    unknown_reason: UnknownReason | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_read_path(self.path))
        old = None if self.old_path is None else normalize_read_path(self.old_path)
        object.__setattr__(self, "old_path", old)
        commits = tuple(sorted(set(self.evidence_commits)))
        object.__setattr__(self, "evidence_commits", commits)
        _check_change(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class WriterChanges:
    """Changes attributed to one writer. Both ids are null when unknown."""

    issue_number: int | None
    claim_id: str | None
    changes: tuple[ChangedResource, ...]

    def __post_init__(self) -> None:
        _check_writer(self.issue_number, self.claim_id)
        if not self.changes:
            raise ValueError("writer changes must not be empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidationCheckpoint:
    """A later review of one head and report. It does not rewrite reads."""

    candidate_head_sha: str
    report_digest: str

    def __post_init__(self) -> None:
        _require_sha(self.candidate_head_sha, "candidate_head_sha")
        _require_digest(self.report_digest, "report_digest")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReaderInput:
    """One reader passed to detection. Missing evidence stays ``None``."""

    identity: ReaderIdentity
    evidence: ReadEvidenceV1 | None
    validations: tuple[ValidationCheckpoint, ...] = ()
    integrated: bool = False

    def __post_init__(self) -> None:
        if self.evidence is None:
            return
        _require_same_reader(self.identity, self.evidence)
        checkpoints = tuple(
            sorted(
                self.validations,
                key=lambda item: (item.candidate_head_sha, item.report_digest),
            )
        )
        object.__setattr__(self, "validations", checkpoints)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadContextInputs:
    """Narrow detection input. It is not an IntegrationContext."""

    repository: str
    parent_issue_number: int
    parent_sha: str
    candidate_head_sha: str
    readers: tuple[ReaderInput, ...]
    writers: tuple[WriterChanges, ...] = ()

    def __post_init__(self) -> None:
        _require_repository(self.repository)
        _require_issue_number(self.parent_issue_number)
        _require_sha(self.parent_sha, "parent_sha")
        _require_sha(self.candidate_head_sha, "candidate_head_sha")
        for reader in self.readers:
            if reader.identity.repository != self.repository:
                raise ValueError("reader repository must match the context")


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateIdentity:
    """The fields that distinguish one stale-read candidate."""

    repository: str
    parent_issue_number: int
    direction: ReadDirection
    reader: ReaderIdentity
    evidence_digest: str
    observation: ReadObservationV1
    change: ChangedResource
    writer_issue_number: int | None
    writer_claim_id: str | None

    def __post_init__(self) -> None:
        _require_repository(self.repository)
        _require_issue_number(self.parent_issue_number)
        _require_digest(self.evidence_digest, "evidence_digest")
        _check_writer(self.writer_issue_number, self.writer_claim_id)
        if self.reader.repository != self.repository:
            raise ValueError("candidate reader repository must match")
        if self.observation.path not in change_paths(self.change):
            raise ValueError("candidate observation must intersect the change")


@dataclass(frozen=True, slots=True, kw_only=True)
class StaleCandidate:
    """One reported candidate. ``candidate_id`` is derived from ``identity``."""

    identity: CandidateIdentity
    candidate_id: str

    def __post_init__(self) -> None:
        if self.candidate_id != candidate_id(self.identity):
            raise ValueError("candidate_id does not match the identity")


@dataclass(frozen=True, slots=True, kw_only=True)
class StaleExclusion:
    """An intersection that the frozen rules exclude from review."""

    reason: ExclusionReason
    identity: CandidateIdentity
    detail: str

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError("exclusion detail must be nonempty")


@dataclass(frozen=True, slots=True, kw_only=True)
class StaleUnknown:
    """An intersection or gap that cannot be classified as fresh."""

    reason: UnknownReason
    path: str
    reader_issue_number: int | None
    detail: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_read_path(self.path))
        if self.reader_issue_number is not None:
            _require_issue_number(self.reader_issue_number)
        if not self.detail.strip():
            raise ValueError("unknown detail must be nonempty")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReaderEvidenceRef:
    """Reader identity stored on a report without copying file contents."""

    issue_number: int
    claim_id: str
    head_sha: str
    evidence_digest: str | None

    def __post_init__(self) -> None:
        _require_issue_number(self.issue_number)
        _require_claim_id(self.claim_id)
        _require_sha(self.head_sha, "head_sha")
        _require_optional_digest(self.evidence_digest)


@dataclass(frozen=True, slots=True, kw_only=True)
class StaleReadReportV1:
    """Detection report for parent ``P`` and candidate head ``H``.

    ``verdict`` must match ``aggregate_stale_verdict``. ``confirmed_stale`` is
    not a detection verdict. Original reads are not rewritten here.
    """

    version: int = READ_CONTRACT_VERSION
    repository: str
    parent_issue_number: int
    parent_sha: str
    candidate_head_sha: str
    readers: tuple[ReaderEvidenceRef, ...]
    candidates: tuple[StaleCandidate, ...] = ()
    exclusions: tuple[StaleExclusion, ...] = ()
    unknowns: tuple[StaleUnknown, ...] = ()
    coverage: ReadCoverage
    verdict: StaleVerdict
    digest: str | None = None

    def __post_init__(self) -> None:
        _require_version(self.version)
        _require_repository(self.repository)
        _require_issue_number(self.parent_issue_number)
        _require_sha(self.parent_sha, "parent_sha")
        _require_sha(self.candidate_head_sha, "candidate_head_sha")
        object.__setattr__(self, "readers", _canon_reader_refs(self.readers))
        object.__setattr__(self, "candidates", _canon_candidates(self.candidates))
        object.__setattr__(self, "exclusions", _canon_exclusions(self.exclusions))
        object.__setattr__(self, "unknowns", _canon_unknowns(self.unknowns))
        expected = aggregate_stale_verdict(
            candidate_count=len(self.candidates),
            unknown_count=len(self.unknowns),
            coverage=self.coverage,
        )
        if self.verdict is not expected:
            raise ValueError("report verdict does not match the aggregate rule")
        _require_optional_digest(self.digest)


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateJudgment:
    """One candidate conclusion inside ``orchestune-read-validation``."""

    candidate_id: str
    conclusion: CandidateConclusion
    rationale: str

    def __post_init__(self) -> None:
        if not self.candidate_id.startswith(CANDIDATE_ID_PREFIX):
            raise ValueError("candidate_id must use the sr1 prefix")
        _require_digest(
            self.candidate_id.removeprefix(CANDIDATE_ID_PREFIX), "candidate_id"
        )
        if not self.rationale.strip():
            raise ValueError("judgment rationale must be nonempty")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadValidationV1:
    """Reviewer block named ``orchestune-read-validation``.

    Every candidate id is listed once. A missing, duplicate, or unknown
    conclusion is not a pass; ``decide_read_gate`` enforces that.
    """

    version: int = READ_CONTRACT_VERSION
    round_number: int
    head_sha: str
    report_digest: str
    decisions: tuple[CandidateJudgment, ...]

    def __post_init__(self) -> None:
        _require_version(self.version)
        _require_issue_number(self.round_number)
        _require_sha(self.head_sha, "head_sha")
        _require_digest(self.report_digest, "report_digest")
        ids = [item.candidate_id for item in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("validation decisions must be unique")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadGateInputs:
    """Inputs for ``decide_read_gate``. Candidate ids are the report's ids."""

    mode: StaleReadGate
    verdict: StaleVerdict
    coverage: ReadCoverage
    candidate_head_sha: str
    report_digest: str | None
    candidate_ids: tuple[str, ...] = ()
    validation: ReadValidationV1 | None = None

    def __post_init__(self) -> None:
        _require_sha(self.candidate_head_sha, "candidate_head_sha")
        _require_optional_digest(self.report_digest)
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("candidate ids must be unique")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadGateDecision:
    """Fast-path and parent-update flags. Required coverage gaps never publish."""

    mode: StaleReadGate
    verdict: StaleVerdict
    coverage: ReadCoverage
    report_digest: str | None
    allow_fast_path: bool
    allow_parent_update: bool
    reason: GateReason

    def __post_init__(self) -> None:
        _require_optional_digest(self.report_digest)
        _check_gate_decision(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReaderSnapshotEntry:
    """One integrated reader. Validations are checkpoints, not new reads."""

    issue_number: int
    claim_id: str
    completion_id: str
    source_sha: str
    evidence_digest: str
    validations: tuple[ValidationCheckpoint, ...] = ()

    def __post_init__(self) -> None:
        _require_issue_number(self.issue_number)
        _require_claim_id(self.claim_id)
        _require_claim_id(self.completion_id)
        _require_sha(self.source_sha, "source_sha")
        _require_digest(self.evidence_digest, "evidence_digest")
        ordered = tuple(
            sorted(
                self.validations,
                key=lambda item: (item.candidate_head_sha, item.report_digest),
            )
        )
        object.__setattr__(self, "validations", ordered)


@dataclass(frozen=True, slots=True, kw_only=True)
class ParentReadSnapshotV1:
    """Snapshot committed at ``refs/orchestune/read-snapshots/parent-issue-N``."""

    version: int = READ_CONTRACT_VERSION
    repository: str
    parent_issue_number: int
    parent_head_sha: str
    readers: tuple[ReaderSnapshotEntry, ...] = ()
    digest: str | None = None

    def __post_init__(self) -> None:
        _require_version(self.version)
        _require_repository(self.repository)
        _require_issue_number(self.parent_issue_number)
        _require_sha(self.parent_head_sha, "parent_head_sha")
        object.__setattr__(self, "readers", _canon_snapshot_readers(self.readers))
        _require_optional_digest(self.digest)


@dataclass(frozen=True, slots=True, kw_only=True)
class SnapshotWrite:
    """Prepared snapshot update. ``expected_oid is None`` means the ref is absent.

    This value does not push. Issue #1780 passes ``ref`` and the two oids to
    the existing atomic push.
    """

    parent_issue_number: int
    expected_oid: str | None
    new_commit_oid: str
    parent_head_sha: str
    snapshot_digest: str

    def __post_init__(self) -> None:
        _require_issue_number(self.parent_issue_number)
        if self.expected_oid is not None:
            _require_sha(self.expected_oid, "expected_oid")
        _require_sha(self.new_commit_oid, "new_commit_oid")
        _require_sha(self.parent_head_sha, "parent_head_sha")
        _require_digest(self.snapshot_digest, "snapshot_digest")

    @property
    def ref(self) -> str:
        return parent_read_snapshot_ref(self.parent_issue_number)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadCheckpointV1:
    """Claim-start marker ``orchestune-read-checkpoint``.

    The same claim keeps one ``input_base_sha``. Unavailable means the start
    was not stored; it is not a guessed sha. No token or local path is stored.
    """

    version: int = READ_CONTRACT_VERSION
    repository: str
    issue_number: int
    claim_id: str
    availability: CheckpointAvailability
    input_base_sha: str | None

    def __post_init__(self) -> None:
        _require_version(self.version)
        _require_repository(self.repository)
        _require_issue_number(self.issue_number)
        _require_claim_id(self.claim_id)
        if self.availability is CheckpointAvailability.RECORDED:
            _require_sha(self.input_base_sha, "input_base_sha")
            return
        if self.input_base_sha is not None:
            raise ValueError("unavailable checkpoint has no input_base_sha")


def _json_ready(value: object) -> object:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError("digest payload cannot contain bool or float")
    if isinstance(value, int):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple | list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    raise TypeError(f"digest payload cannot contain {type(value).__name__}")


def _require_version(version: object) -> None:
    if version != READ_CONTRACT_VERSION:
        raise ValueError("schema version must be 1")


def _require_repository(repository: object) -> None:
    if not isinstance(repository, str) or _REPOSITORY.fullmatch(repository) is None:
        raise ValueError("repository must be owner/name")


def _require_issue_number(number: object) -> None:
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError("issue number must be a positive integer")


def _require_count(number: object, field: str) -> None:
    if isinstance(number, bool) or not isinstance(number, int) or number < 0:
        raise ValueError(f"{field} must be a non-negative integer")


def _require_claim_id(claim_id: object) -> None:
    if not isinstance(claim_id, str) or _CLAIM_ID.fullmatch(claim_id) is None:
        raise ValueError("claim id must be a token without whitespace")


def _require_sha(value: object, field: str) -> None:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError(f"{field} must be 40 lowercase hex characters")


def _require_digest(value: object, field: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} must be 64 lowercase hex characters")


def _require_optional_digest(value: object) -> None:
    if value is not None:
        _require_digest(value, "digest")


def _require_mode(value: object) -> None:
    if value not in GIT_FILE_MODES:
        raise ValueError("file mode must be a git mode")


def _optional_sha(value: str | None, field: str) -> None:
    if value is not None:
        _require_sha(value, field)


def _optional_mode(value: str | None) -> None:
    if value is not None:
        _require_mode(value)


def _check_observation(obs: ReadObservationV1) -> None:
    if obs.source is ReadSource.UNAVAILABLE:
        raise ValueError("an observation is not an unavailable evidence source")
    if obs.presence is ResourcePresence.PRESENT:
        _require_sha(obs.snapshot_commit, "snapshot_commit")
        _require_sha(obs.blob_oid, "blob_oid")
        _require_mode(obs.file_mode)
        return
    if obs.blob_oid is not None or obs.file_mode is not None:
        raise ValueError("absent and unknown observations have no blob or mode")
    if obs.presence is ResourcePresence.ABSENT:
        _require_sha(obs.snapshot_commit, "snapshot_commit")
        return
    if obs.snapshot_commit is not None:
        _require_sha(obs.snapshot_commit, "snapshot_commit")


def _canon_paths(paths: tuple[str, ...] | None) -> tuple[str, ...] | None:
    if paths is None:
        return None
    return tuple(sorted({normalize_read_path(path) for path in paths}))


def _observation_key(obs: ReadObservationV1) -> tuple[object, ...]:
    return (
        obs.path,
        obs.snapshot_commit or "",
        obs.presence.value,
        obs.blob_oid or "",
        obs.file_mode or "",
        obs.source.value,
    )


def _canon_observations(
    items: tuple[ReadObservationV1, ...],
) -> tuple[ReadObservationV1, ...]:
    return tuple(sorted(set(items), key=_observation_key))


def _check_evidence_source(evidence: ReadEvidenceV1) -> None:
    if evidence.source is not ReadSource.UNAVAILABLE:
        return
    if evidence.coverage is not ReadCoverage.UNAVAILABLE:
        raise ValueError("unavailable source requires unavailable coverage")
    if evidence.declared_reads is not None or evidence.observations:
        raise ValueError("unavailable evidence has no declaration or observations")


def _check_change(change: ChangedResource) -> None:
    _check_rename_path(change)
    for commit in change.evidence_commits:
        _require_sha(commit, "evidence_commits")
    _optional_sha(change.old_blob_oid, "old_blob_oid")
    _optional_sha(change.new_blob_oid, "new_blob_oid")
    _optional_mode(change.old_file_mode)
    _optional_mode(change.new_file_mode)
    if change.availability is ObservationAvailability.UNKNOWN:
        if change.unknown_reason is None:
            raise ValueError("unknown change requires unknown_reason")
        return
    if change.unknown_reason is not None:
        raise ValueError("resolved change has no unknown_reason")
    if not change.evidence_commits:
        raise ValueError("resolved change requires an evidence commit")
    _check_resolved_change(change)


def _check_rename_path(change: ChangedResource) -> None:
    if change.kind is ChangeKind.RENAME:
        if change.old_path is None or change.old_path == change.path:
            raise ValueError("rename requires a different old_path")
        return
    if change.old_path is not None:
        raise ValueError("only a rename records old_path")


def _check_resolved_change(change: ChangedResource) -> None:
    if change.kind is ChangeKind.ADD:
        _require_absent_side(change.old_blob_oid, change.old_file_mode, "add old")
        _require_present_side(change.new_blob_oid, change.new_file_mode, "add new")
        return
    if change.kind is ChangeKind.DELETE:
        _require_present_side(change.old_blob_oid, change.old_file_mode, "delete old")
        _require_absent_side(change.new_blob_oid, change.new_file_mode, "delete new")
        return
    _require_present_side(change.old_blob_oid, change.old_file_mode, "old")
    _require_present_side(change.new_blob_oid, change.new_file_mode, "new")
    if change.kind is ChangeKind.MODIFY and change.old_blob_oid == change.new_blob_oid:
        raise ValueError("modify must change the blob")
    if change.kind is ChangeKind.MODE:
        if change.old_blob_oid != change.new_blob_oid:
            raise ValueError("mode change keeps the blob")
        if change.old_file_mode == change.new_file_mode:
            raise ValueError("mode change must change the mode")


def _require_present_side(blob: str | None, mode: str | None, field: str) -> None:
    if blob is None or mode is None:
        raise ValueError(f"{field} side requires blob and mode")


def _require_absent_side(blob: str | None, mode: str | None, field: str) -> None:
    if blob is not None or mode is not None:
        raise ValueError(f"{field} side must be empty")


def _check_writer(issue_number: int | None, claim_id: str | None) -> None:
    if issue_number is None and claim_id is None:
        return
    _require_issue_number(issue_number)
    _require_claim_id(claim_id)


def _require_same_reader(identity: ReaderIdentity, evidence: ReadEvidenceV1) -> None:
    if (
        evidence.repository != identity.repository
        or evidence.issue_number != identity.issue_number
        or evidence.claim_id != identity.claim_id
        or evidence.head_sha != identity.head_sha
    ):
        raise ValueError("evidence must match the reader identity")


def _canon_reader_refs(
    items: tuple[ReaderEvidenceRef, ...],
) -> tuple[ReaderEvidenceRef, ...]:
    return tuple(sorted(set(items), key=_reader_ref_key))


def _reader_ref_key(item: ReaderEvidenceRef) -> tuple[object, ...]:
    return (item.issue_number, item.claim_id, item.head_sha, item.evidence_digest or "")


def _canon_candidates(items: tuple[StaleCandidate, ...]) -> tuple[StaleCandidate, ...]:
    return tuple(sorted(set(items), key=lambda item: item.candidate_id))


def _canon_exclusions(items: tuple[StaleExclusion, ...]) -> tuple[StaleExclusion, ...]:
    return tuple(sorted(set(items), key=_exclusion_key))


def _exclusion_key(item: StaleExclusion) -> tuple[object, ...]:
    return (item.reason.value, candidate_id(item.identity), item.detail)


def _canon_unknowns(items: tuple[StaleUnknown, ...]) -> tuple[StaleUnknown, ...]:
    return tuple(sorted(set(items), key=_unknown_key))


def _unknown_key(item: StaleUnknown) -> tuple[object, ...]:
    return (item.reason.value, item.path, item.reader_issue_number or 0, item.detail)


def _snapshot_reader_key(item: ReaderSnapshotEntry) -> tuple[object, ...]:
    checkpoints = tuple(
        (point.candidate_head_sha, point.report_digest) for point in item.validations
    )
    return (
        item.issue_number,
        item.claim_id,
        item.completion_id,
        item.source_sha,
        item.evidence_digest,
        checkpoints,
    )


def _canon_snapshot_readers(
    items: tuple[ReaderSnapshotEntry, ...],
) -> tuple[ReaderSnapshotEntry, ...]:
    return tuple(sorted(set(items), key=_snapshot_reader_key))


def _observation_payload(obs: ReadObservationV1) -> dict[str, Any]:
    return {
        "path": obs.path,
        "snapshot_commit": obs.snapshot_commit,
        "presence": obs.presence.value,
        "blob_oid": obs.blob_oid,
        "file_mode": obs.file_mode,
        "source": obs.source.value,
    }


def _change_payload(change: ChangedResource) -> dict[str, Any]:
    return {
        "path": change.path,
        "old_path": change.old_path,
        "kind": change.kind.value,
        "old_blob_oid": change.old_blob_oid,
        "new_blob_oid": change.new_blob_oid,
        "old_file_mode": change.old_file_mode,
        "new_file_mode": change.new_file_mode,
        "evidence_commits": list(change.evidence_commits),
        "availability": change.availability.value,
        "unknown_reason": None
        if change.unknown_reason is None
        else change.unknown_reason.value,
    }


def _reader_payload(reader: ReaderIdentity) -> dict[str, Any]:
    return {
        "repository": reader.repository,
        "issue_number": reader.issue_number,
        "claim_id": reader.claim_id,
        "head_sha": reader.head_sha,
    }


def _reader_ref_payload(item: ReaderEvidenceRef) -> dict[str, Any]:
    return {
        "issue_number": item.issue_number,
        "claim_id": item.claim_id,
        "head_sha": item.head_sha,
        "evidence_digest": item.evidence_digest,
    }


def _candidate_payload(item: StaleCandidate) -> dict[str, Any]:
    payload = candidate_digest_input(item.identity)
    payload["candidate_id"] = item.candidate_id
    return payload


def _exclusion_payload(item: StaleExclusion) -> dict[str, Any]:
    return {
        "reason": item.reason.value,
        "identity": candidate_digest_input(item.identity),
        "detail": item.detail,
    }


def _unknown_payload(item: StaleUnknown) -> dict[str, Any]:
    return {
        "reason": item.reason.value,
        "path": item.path,
        "reader_issue_number": item.reader_issue_number,
        "detail": item.detail,
    }


def _snapshot_reader_payload(item: ReaderSnapshotEntry) -> dict[str, Any]:
    return {
        "issue_number": item.issue_number,
        "claim_id": item.claim_id,
        "completion_id": item.completion_id,
        "source_sha": item.source_sha,
        "evidence_digest": item.evidence_digest,
        "validations": [
            {
                "candidate_head_sha": point.candidate_head_sha,
                "report_digest": point.report_digest,
            }
            for point in item.validations
        ],
    }


def _gate(
    inputs: ReadGateInputs,
    reason: GateReason,
    *,
    allow_fast_path: bool,
    allow_parent: bool,
) -> ReadGateDecision:
    return ReadGateDecision(
        mode=inputs.mode,
        verdict=inputs.verdict,
        coverage=inputs.coverage,
        report_digest=inputs.report_digest,
        allow_fast_path=allow_fast_path,
        allow_parent_update=allow_parent,
        reason=reason,
    )


def _decide_required(inputs: ReadGateInputs) -> ReadGateDecision:
    if inputs.coverage is not ReadCoverage.COMPLETE:
        return _gate(
            inputs, GateReason.COVERAGE_GAP, allow_fast_path=False, allow_parent=False
        )
    if inputs.verdict is StaleVerdict.UNKNOWN:
        return _gate(
            inputs, GateReason.UNKNOWN, allow_fast_path=False, allow_parent=False
        )
    if inputs.verdict is StaleVerdict.CONFIRMED_STALE:
        return _gate(
            inputs,
            GateReason.CONFIRMED_STALE,
            allow_fast_path=False,
            allow_parent=False,
        )
    if inputs.verdict is StaleVerdict.FRESH:
        return _decide_fresh(inputs)
    return _decide_candidate(inputs)


def _decide_fresh(inputs: ReadGateInputs) -> ReadGateDecision:
    if inputs.validation is not None and not _validation_binds(inputs):
        return _gate(
            inputs, GateReason.INPUT_CHANGED, allow_fast_path=False, allow_parent=False
        )
    return _gate(inputs, GateReason.FRESH, allow_fast_path=True, allow_parent=True)


def _decide_candidate(inputs: ReadGateInputs) -> ReadGateDecision:
    if inputs.validation is None:
        return _gate(
            inputs,
            GateReason.CANDIDATE_PENDING,
            allow_fast_path=False,
            allow_parent=False,
        )
    if not _validation_binds(inputs):
        return _gate(
            inputs, GateReason.INPUT_CHANGED, allow_fast_path=False, allow_parent=False
        )
    if not _validation_covers(inputs):
        return _gate(
            inputs,
            GateReason.VALIDATION_INCOMPLETE,
            allow_fast_path=False,
            allow_parent=False,
        )
    return _gate(
        inputs, GateReason.VALIDATION_ACCEPTED, allow_fast_path=False, allow_parent=True
    )


def _validation_binds(inputs: ReadGateInputs) -> bool:
    validation = inputs.validation
    if validation is None or inputs.report_digest is None:
        return False
    return (
        validation.head_sha == inputs.candidate_head_sha
        and validation.report_digest == inputs.report_digest
    )


def _validation_covers(inputs: ReadGateInputs) -> bool:
    validation = inputs.validation
    if validation is None:
        return False
    judged = {item.candidate_id: item.conclusion for item in validation.decisions}
    if set(judged) != set(inputs.candidate_ids):
        return False
    return all(
        conclusion is not CandidateConclusion.UNKNOWN for conclusion in judged.values()
    )


_GATE_FLAGS: dict[GateReason, tuple[bool, bool]] = {
    GateReason.GATE_OFF: (True, True),
    GateReason.ADVISORY: (True, True),
    GateReason.FRESH: (True, True),
    GateReason.VALIDATION_ACCEPTED: (False, True),
    GateReason.CANDIDATE_PENDING: (False, False),
    GateReason.VALIDATION_INCOMPLETE: (False, False),
    GateReason.COVERAGE_GAP: (False, False),
    GateReason.UNKNOWN: (False, False),
    GateReason.CONFIRMED_STALE: (False, False),
    GateReason.INPUT_CHANGED: (False, False),
}


def _check_gate_decision(decision: ReadGateDecision) -> None:
    flags = _GATE_FLAGS[decision.reason]
    if (decision.allow_fast_path, decision.allow_parent_update) != flags:
        raise ValueError("gate flags do not match the reason")
    _check_gate_mode(decision)
    _check_gate_verdict(decision)


def _check_gate_mode(decision: ReadGateDecision) -> None:
    if (
        decision.reason is GateReason.GATE_OFF
        and decision.mode is not StaleReadGate.OFF
    ):
        raise ValueError("gate_off requires mode off")
    if (
        decision.reason is GateReason.ADVISORY
        and decision.mode is not StaleReadGate.ADVISORY
    ):
        raise ValueError("advisory reason requires advisory mode")
    if decision.reason not in {GateReason.GATE_OFF, GateReason.ADVISORY}:
        if decision.mode is not StaleReadGate.REQUIRED:
            raise ValueError("blocking reasons require required mode")


def _check_gate_verdict(decision: ReadGateDecision) -> None:
    if decision.reason is GateReason.FRESH:
        if (
            decision.verdict is not StaleVerdict.FRESH
            or decision.coverage is not ReadCoverage.COMPLETE
        ):
            raise ValueError("fresh gate requires a fresh complete report")
    if (
        decision.reason is GateReason.COVERAGE_GAP
        and decision.coverage is ReadCoverage.COMPLETE
    ):
        raise ValueError("coverage_gap requires incomplete coverage")
    if (
        decision.reason is GateReason.CONFIRMED_STALE
        and decision.verdict is not StaleVerdict.CONFIRMED_STALE
    ):
        raise ValueError("confirmed_stale reason requires that verdict")
    if (
        decision.reason is GateReason.UNKNOWN
        and decision.verdict is not StaleVerdict.UNKNOWN
    ):
        raise ValueError("unknown reason requires an unknown verdict")
