"""Assemble read inputs from immutable source proofs and retained reader data.

The caller supplies verified receipts and complete, parent-scoped history through
a read-only protocol. Closed Issues and deleted branches remain readers. Unknown
assembly returns no detector input: an incomplete empty set must never be fresh.
This module neither publishes refs nor updates child Issues or old Outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from orchestune.integrator.proofs import TaskIntegrationProof
from orchestune.outcome_record import OutcomeRecord
from orchestune.read_dependencies.contracts import (
    CheckpointAvailability,
    ParentReadSnapshotV1,
    ReadCheckpointV1,
    ReadContextInputs,
    ReadCoverage,
    ReaderIdentity,
    ReaderInput,
    ReaderSnapshotEntry,
    ReadEvidenceV1,
    UnknownReason,
    ValidationCheckpoint,
    WriterChanges,
    seal_evidence,
    seal_snapshot,
)


@dataclass(frozen=True, kw_only=True)
class ReaderSourceProof:
    """A receipt/source plus the exact expected claim and completion generation.

    The provider must use authenticated receipt lookup and exact-generation
    Outcome/checkpoint lookup, not the latest Outcome or included labels.
    Missing or malformed receipts are represented by ``proof=None``.
    Validations are independently verified successful checkpoints, never inferred
    from inclusion, CI success, rebasing, or a source commit's ancestry.
    """

    proof: TaskIntegrationProof | None
    claim_id: str
    completion_id: str
    outcome: OutcomeRecord | None
    checkpoint: ReadCheckpointV1 | None
    validations: tuple[ValidationCheckpoint, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ReaderHistory:
    """All integrations for P, including closed children and missing proofs.

    ``complete`` attests successful enumeration of every page and historical
    child, including integrations with unavailable evidence. ``empty_verified``
    attests absence of old integration history; an empty active task list does
    not establish either flag. Providers must verify receipts belong to this
    parent and their source commits are integrated into P.
    """

    readers: tuple[ReaderSourceProof, ...]
    complete: bool = False
    empty_verified: bool = False


class ReadContextSource(Protocol):
    """Durable read access; no branch lookup or mutation is required."""

    def list_integrated_readers(
        self,
        *,
        repository: str,
        parent_issue_number: int,
        parent_sha: str,
        include_closed: bool,
    ) -> ReaderHistory:
        """Enumerate complete receipt/Outcome history, including closed Issues."""
        ...

    def load_snapshot_evidence(
        self, entry: ReaderSnapshotEntry
    ) -> ReadEvidenceV1 | None:
        """Load retained data by digest, even after Issue close/branch deletion."""
        ...


@dataclass(frozen=True, kw_only=True)
class ReadContextRequest:
    """Frozen P/H and child inputs, independent of L3 IntegrationContext.

    ``snapshot=None`` means the ref is confirmed absent. Fetch/storage failures
    must set ``snapshot_unknown`` instead, preventing accidental rebootstrap.
    Writers contain the separately acquired/attributed Git changes, not footprints.
    """

    repository: str
    parent_issue_number: int
    parent_sha: str
    candidate_head_sha: str
    current_sources: tuple[ReaderSourceProof, ...] = ()
    writers: tuple[WriterChanges, ...] = ()
    snapshot: ParentReadSnapshotV1 | None = None
    snapshot_unknown: UnknownReason | None = None


@dataclass(frozen=True, kw_only=True)
class ReadContextResult:
    """Assembly evidence; callers must handle unknown before invoking detection.

    ``snapshot`` describes existing readers at P, excluding incoming children.
    It is a bootstrap proposal only: the caller later prepares/publishes H's
    snapshot. Original observations and validation checkpoints remain distinct.
    """

    inputs: ReadContextInputs | None
    snapshot: ParentReadSnapshotV1 | None
    bootstrap: bool
    coverage: ReadCoverage
    unknowns: tuple[UnknownReason, ...] = ()


class _UnknownContext(ValueError):
    def __init__(self, reason: UnknownReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


def build_read_context(
    request: ReadContextRequest, source: ReadContextSource
) -> ReadContextResult:
    """Restore existing readers and combine this attempt's exact child proofs."""
    try:
        inputs = ReadContextInputs(
            repository=request.repository,
            parent_issue_number=request.parent_issue_number,
            parent_sha=request.parent_sha,
            candidate_head_sha=request.candidate_head_sha,
            readers=(),
            writers=request.writers,
        )
        if request.snapshot_unknown is not None:
            raise _UnknownContext(request.snapshot_unknown)
        snapshot, readers = _existing_readers(request, source)
        entries = list(snapshot.readers)
        for item in request.current_sources:
            entry, reader = _source_reader(item, request.repository, integrated=False)
            _add_reader(entries, readers, entry, reader)
        return ReadContextResult(
            inputs=ReadContextInputs(
                repository=inputs.repository,
                parent_issue_number=inputs.parent_issue_number,
                parent_sha=inputs.parent_sha,
                candidate_head_sha=inputs.candidate_head_sha,
                readers=tuple(readers),
                writers=inputs.writers,
            ),
            snapshot=snapshot,
            bootstrap=request.snapshot is None,
            coverage=_coverage(readers),
        )
    except _UnknownContext as error:
        return _unknown(error.reason)
    except (ValueError, TypeError):
        return _unknown(UnknownReason.EVIDENCE_MISMATCH)


def _existing_readers(
    request: ReadContextRequest, source: ReadContextSource
) -> tuple[ParentReadSnapshotV1, list[ReaderInput]]:
    if request.snapshot is None:
        return _bootstrap(request, source)
    snapshot = request.snapshot
    if (
        snapshot.repository != request.repository
        or snapshot.parent_issue_number != request.parent_issue_number
        or snapshot.parent_head_sha != request.parent_sha
        or snapshot.digest is None
        or seal_snapshot(snapshot).digest != snapshot.digest
    ):
        raise _UnknownContext(UnknownReason.PARENT_SNAPSHOT_MISMATCH)
    entries: list[ReaderSnapshotEntry] = []
    readers: list[ReaderInput] = []
    for entry in snapshot.readers:
        try:
            evidence = source.load_snapshot_evidence(entry)
        except Exception as error:  # noqa: BLE001 - boundary: retained data lookup cannot turn failure into empty reads
            raise _UnknownContext(UnknownReason.OBJECT_MISSING) from error
        _add_reader(
            entries,
            readers,
            entry,
            _reader(entry, evidence, request.repository, integrated=True),
        )
    return snapshot, readers


def _bootstrap(
    request: ReadContextRequest, source: ReadContextSource
) -> tuple[ParentReadSnapshotV1, list[ReaderInput]]:
    try:
        history = source.list_integrated_readers(
            repository=request.repository,
            parent_issue_number=request.parent_issue_number,
            parent_sha=request.parent_sha,
            include_closed=True,
        )
    except Exception as error:  # noqa: BLE001 - boundary: any missing page leaves history unknown
        raise _UnknownContext(UnknownReason.FETCH_FAILED) from error
    if not history.complete or (not history.readers and not history.empty_verified):
        raise _UnknownContext(UnknownReason.COVERAGE_UNAVAILABLE)
    entries: list[ReaderSnapshotEntry] = []
    readers: list[ReaderInput] = []
    for item in history.readers:
        entry, reader = _source_reader(item, request.repository, integrated=True)
        _add_reader(entries, readers, entry, reader)
    return (
        seal_snapshot(
            ParentReadSnapshotV1(
                repository=request.repository,
                parent_issue_number=request.parent_issue_number,
                parent_head_sha=request.parent_sha,
                readers=tuple(entries),
            )
        ),
        readers,
    )


def _source_reader(
    item: ReaderSourceProof, repository: str, *, integrated: bool = False
) -> tuple[ReaderSnapshotEntry, ReaderInput]:
    proof, outcome, checkpoint = item.proof, item.outcome, item.checkpoint
    if (
        proof is None
        or outcome is None
        or checkpoint is None
        or outcome.result != "done"
        or outcome.issue != proof.issue_number
        or outcome.claim_id != item.claim_id
        or outcome.completion_id != item.completion_id
        or outcome.head_sha != proof.source_sha
        or outcome.review.reviewed_head_sha != proof.source_sha
        or outcome.read_evidence is None
        or checkpoint.availability is not CheckpointAvailability.RECORDED
        or checkpoint.repository != repository
        or checkpoint.issue_number != proof.issue_number
        or checkpoint.claim_id != item.claim_id
        or checkpoint.input_base_sha != outcome.read_evidence.input_base_sha
    ):
        raise _UnknownContext(UnknownReason.EVIDENCE_MISMATCH)
    entry = ReaderSnapshotEntry(
        issue_number=proof.issue_number,
        claim_id=item.claim_id,
        completion_id=item.completion_id,
        source_sha=proof.source_sha,
        evidence_digest=outcome.read_evidence.digest or "",
        validations=item.validations,
    )
    return entry, _reader(
        entry, outcome.read_evidence, repository, integrated=integrated
    )


def _reader(
    entry: ReaderSnapshotEntry,
    evidence: ReadEvidenceV1 | None,
    repository: str,
    *,
    integrated: bool = False,
) -> ReaderInput:
    if (
        evidence is None
        or evidence.digest != entry.evidence_digest
        or seal_evidence(evidence).digest != entry.evidence_digest
    ):
        raise _UnknownContext(UnknownReason.EVIDENCE_MISMATCH)
    return ReaderInput(
        identity=ReaderIdentity(
            repository=repository,
            issue_number=entry.issue_number,
            claim_id=entry.claim_id,
            head_sha=entry.source_sha,
        ),
        evidence=evidence,
        validations=entry.validations,
        integrated=integrated,
    )


def _add_reader(
    entries: list[ReaderSnapshotEntry],
    readers: list[ReaderInput],
    entry: ReaderSnapshotEntry,
    reader: ReaderInput,
) -> None:
    for previous in entries:
        if (previous.issue_number, previous.claim_id) != (
            entry.issue_number,
            entry.claim_id,
        ):
            continue
        if previous != entry:
            raise _UnknownContext(UnknownReason.EVIDENCE_MISMATCH)
        return
    entries.append(entry)
    readers.append(reader)


def _coverage(readers: list[ReaderInput]) -> ReadCoverage:
    coverages = {r.evidence.coverage for r in readers if r.evidence is not None}
    if ReadCoverage.UNAVAILABLE in coverages:
        return ReadCoverage.UNAVAILABLE
    if ReadCoverage.PARTIAL in coverages:
        return ReadCoverage.PARTIAL
    return ReadCoverage.COMPLETE


def _unknown(reason: UnknownReason) -> ReadContextResult:
    return ReadContextResult(
        inputs=None,
        snapshot=None,
        bootstrap=False,
        coverage=ReadCoverage.UNAVAILABLE,
        unknowns=(reason,),
    )
