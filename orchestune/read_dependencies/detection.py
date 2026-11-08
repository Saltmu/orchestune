"""Pure stale-read detection over frozen reader versions and attributed changes.

Checkpoint-bearing readers are retained (reverse) readers; writers also present
in the reader batch are same-attempt children. Remaining comparisons are forward.
Checkpoints contain no Git trees, so they cannot prove that an original read is
unchanged. Review validation remains separate from detecting intersections.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from orchestune.read_dependencies.contracts import (
    CandidateIdentity,
    ExclusionReason,
    ObservationAvailability,
    ReadContextInputs,
    ReadCoverage,
    ReadDirection,
    ReaderEvidenceRef,
    ReaderInput,
    ReadEvidenceV1,
    ReadObservationV1,
    ReadSource,
    ResourcePresence,
    StaleCandidate,
    StaleExclusion,
    StaleReadReportV1,
    StaleUnknown,
    UnknownReason,
    WriterChanges,
    aggregate_stale_verdict,
    candidate_id,
    change_paths,
    content_unchanged,
    declared_dependency_observation,
    is_reader_own_write,
    seal_evidence,
    seal_report,
)


@dataclass
class _Detection:
    readers: list[ReaderEvidenceRef] = field(default_factory=list)
    candidates: list[StaleCandidate] = field(default_factory=list)
    exclusions: list[StaleExclusion] = field(default_factory=list)
    unknowns: list[StaleUnknown] = field(default_factory=list)
    coverage: ReadCoverage = ReadCoverage.COMPLETE


def detect_stale_reads(inputs: ReadContextInputs) -> StaleReadReportV1:
    """Return a sealed, order-independent report without accessing a repository.

    Missing evidence uses ``read-evidence`` as a diagnostic path only when no
    concrete read/change path is available. Partial coverage retains uncertainty
    even if all recorded intersections are excluded. Detection never confirms a
    semantic bug and never treats a checkpoint as a new read observation.
    """
    result = _Detection()
    for reader in inputs.readers:
        _detect_reader(inputs, reader, result)
    return seal_report(
        StaleReadReportV1(
            repository=inputs.repository,
            parent_issue_number=inputs.parent_issue_number,
            parent_sha=inputs.parent_sha,
            candidate_head_sha=inputs.candidate_head_sha,
            readers=tuple(result.readers),
            candidates=tuple(result.candidates),
            exclusions=tuple(result.exclusions),
            unknowns=tuple(result.unknowns),
            coverage=result.coverage,
            verdict=aggregate_stale_verdict(
                candidate_count=len(result.candidates),
                unknown_count=len(result.unknowns),
                coverage=result.coverage,
            ),
        )
    )


def _detect_reader(
    inputs: ReadContextInputs, reader: ReaderInput, result: _Detection
) -> None:
    evidence = reader.evidence
    sealed = None if evidence is None else seal_evidence(evidence)
    result.readers.append(
        ReaderEvidenceRef(
            issue_number=reader.identity.issue_number,
            claim_id=reader.identity.claim_id,
            head_sha=reader.identity.head_sha,
            evidence_digest=None if sealed is None else sealed.digest,
        )
    )
    if evidence is None or sealed is None:
        result.coverage = ReadCoverage.UNAVAILABLE
        _gap(inputs, reader, UnknownReason.EVIDENCE_MISMATCH, result)
        return
    if evidence.digest is not None and evidence.digest != sealed.digest:
        _gap(inputs, reader, UnknownReason.EVIDENCE_MISMATCH, result)
        return
    if evidence.coverage is not ReadCoverage.COMPLETE:
        if result.coverage is not ReadCoverage.UNAVAILABLE:
            result.coverage = evidence.coverage
        reason = (
            UnknownReason.COVERAGE_PARTIAL
            if evidence.coverage is ReadCoverage.PARTIAL
            else UnknownReason.COVERAGE_UNAVAILABLE
        )
        _gap(inputs, reader, reason, result)
    observations = _read_versions(evidence)
    for obs in observations:
        if (
            obs.presence is ResourcePresence.UNKNOWN
            and obs.source is not ReadSource.DECLARED_ONLY
        ):
            _unknown(reader, obs.path, UnknownReason.OBJECT_MISSING, result)
    for writer in inputs.writers:
        _detect_writer(inputs, reader, sealed, observations, writer, result)


def _read_versions(evidence: ReadEvidenceV1) -> tuple[ReadObservationV1, ...]:
    observations = list(evidence.observations)
    # An observation at a later snapshot cannot resolve a declaration at the
    # claim base. Keep both versions rather than upgrading the declared read.
    resolved_base_paths = {
        item.path
        for item in observations
        if item.snapshot_commit == evidence.input_base_sha
    }
    for path in evidence.declared_reads or ():
        if path not in resolved_base_paths:
            observations.append(
                declared_dependency_observation(path, evidence.input_base_sha)
            )
    return tuple(observations)


def _gap(
    inputs: ReadContextInputs,
    reader: ReaderInput,
    reason: UnknownReason,
    result: _Detection,
) -> None:
    paths = {
        path
        for writer in inputs.writers
        for change in writer.changes
        for path in change_paths(change)
    }
    if reader.evidence is not None:
        paths.update(reader.evidence.declared_reads or ())
        paths.update(item.path for item in reader.evidence.observations)
    for path in sorted(paths or {"read-evidence"}):
        _unknown(reader, path, reason, result)


def _unknown(
    reader: ReaderInput, path: str, reason: UnknownReason, result: _Detection
) -> None:
    result.unknowns.append(
        StaleUnknown(
            reason=reason,
            path=path,
            reader_issue_number=reader.identity.issue_number,
            detail=f"{reason.value}: reader {reader.identity.claim_id} at {reader.identity.head_sha}",
        )
    )


def _direction(
    inputs: ReadContextInputs, reader: ReaderInput, writer: WriterChanges
) -> ReadDirection:
    if reader.validations:
        return ReadDirection.REVERSE
    if any(
        is_reader_own_write(item.identity, writer.issue_number, writer.claim_id)
        for item in inputs.readers
    ):
        return ReadDirection.SAME_ATTEMPT
    return ReadDirection.FORWARD


def _detect_writer(
    inputs: ReadContextInputs,
    reader: ReaderInput,
    evidence: ReadEvidenceV1,
    observations: tuple[ReadObservationV1, ...],
    writer: WriterChanges,
    result: _Detection,
) -> None:
    for change in writer.changes:
        # An unresolved Git range can hide reads outside the listed path.
        if change.availability is ObservationAvailability.UNKNOWN:
            _unknown(
                reader,
                change.path,
                change.unknown_reason or UnknownReason.OBJECT_MISSING,
                result,
            )
            continue
        for obs in observations:
            if obs.path not in change_paths(change):
                continue
            identity = CandidateIdentity(
                repository=inputs.repository,
                parent_issue_number=inputs.parent_issue_number,
                direction=_direction(inputs, reader, writer),
                reader=reader.identity,
                evidence_digest=evidence.digest or "",
                observation=obs,
                change=change,
                writer_issue_number=writer.issue_number,
                writer_claim_id=writer.claim_id,
            )
            _classify(reader, identity, result)


def _classify(
    reader: ReaderInput, identity: CandidateIdentity, result: _Detection
) -> None:
    if is_reader_own_write(
        identity.reader, identity.writer_issue_number, identity.writer_claim_id
    ):
        reason = ExclusionReason.READER_OWN_WRITE
    elif identity.writer_issue_number is None:
        _unknown(
            reader, identity.observation.path, UnknownReason.WRITER_UNKNOWN, result
        )
        return
    elif content_unchanged(identity.observation, identity.change):
        reason = ExclusionReason.CONTENT_UNCHANGED
    elif (
        identity.observation.presence is ResourcePresence.UNKNOWN
        and identity.observation.source is not ReadSource.DECLARED_ONLY
    ):
        return  # The unresolved observation was already recorded as unknown.
    else:
        result.candidates.append(
            StaleCandidate(identity=identity, candidate_id=candidate_id(identity))
        )
        return
    result.exclusions.append(
        StaleExclusion(
            reason=reason,
            identity=identity,
            detail=(
                "Change belongs to the same reader issue and claim"
                if reason is ExclusionReason.READER_OWN_WRITE
                else "Recorded blob and file mode equal the resolved target at this path"
            ),
        )
    )
