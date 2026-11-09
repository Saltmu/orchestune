"""Prepare atomic reader publication and recover only verified parent/ref pairs."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orchestune.infra.git_cli import run_git
from orchestune.integrator.read_gate import make_default_read_context_builder
from orchestune.outcome_record import parse_from_comments
from orchestune.read_dependencies.contracts import (
    ParentReadSnapshotV1,
    ReaderSnapshotEntry,
    SnapshotWrite,
    StaleReadReportV1,
    ValidationCheckpoint,
    parent_read_snapshot_ref,
    seal_snapshot,
)
from orchestune.read_dependencies.detection import detect_stale_reads
from orchestune.read_dependencies.report_codec import normalize_report
from orchestune.read_dependencies.storage import prepare_snapshot_write, read_snapshot


@dataclass(frozen=True)
class ReadPublication:
    """A prepared CAS update, or an already published and verified pair."""

    write: SnapshotWrite | None = None
    recovered: bool = False


def _remote_pair(root: Path, parent: int) -> tuple[str | None, str | None]:
    refs = (f"refs/heads/parent/issue-{parent}", parent_read_snapshot_ref(parent))
    output = run_git(["ls-remote", "--refs", "origin", *refs], cwd=root).stdout
    observed: dict[str, str] = {}
    for line in output.splitlines():
        fields = line.split()
        if (
            len(fields) != 2
            or fields[1] not in refs
            or fields[1] in observed
            or re.fullmatch(r"[0-9a-f]{40}", fields[0]) is None
        ):
            raise ValueError("invalid parent/snapshot ref response")
        observed[fields[1]] = fields[0]
    return observed.get(refs[0]), observed.get(refs[1])


def _passed_report(ctx: Any) -> StaleReadReportV1:
    state = ctx.semantic_review_state
    if not isinstance(ctx.read_report, StaleReadReportV1):
        raise ValueError("required read report is missing")
    report = normalize_report(ctx.read_report)
    if (
        report.repository != ctx.forge.get_repository_slug()
        or report.parent_issue_number != ctx.attempt_identity.parent_issue_number
        or report.parent_sha != state.base_sha
        or report.candidate_head_sha != state.head_sha
        or report.digest != state.read_report_digest
        or report.digest != state.reviewed_read_report_digest
        or report.unknowns
    ):
        raise ValueError("snapshot publication lacks the passed P/H/report")
    sources = {(r.issue_number, r.head_sha) for r in report.readers}
    if any(
        (c.issue_number, c.source_sha) not in sources
        for c in ctx.attempt_identity.child_sources
    ):
        raise ValueError("read report omits an incoming child")
    return report


def _completed_entry(ctx: Any, reader: Any) -> ReaderSnapshotEntry:
    identity = reader.identity
    author = ctx.forge.get_authenticated_user()
    if not isinstance(author, str) or not author:
        raise ValueError("snapshot completion author is unavailable")
    records = [
        parse_from_comments([comment])
        for comment in ctx.forge.list_all_issue_comments(identity.issue_number)
        if (comment.get("user") or comment.get("author") or {})
        .get("login", "")
        .casefold()
        == author.casefold()
    ]
    matches = [
        r
        for r in records
        if r is not None
        and r.issue == identity.issue_number
        and r.claim_id == identity.claim_id
        and r.head_sha == identity.head_sha
        and r.result == "done"
    ]
    if not matches or any(r != matches[0] for r in matches):
        raise ValueError("snapshot completion generation is missing or ambiguous")
    record = matches[0]
    if (
        not record.completion_id
        or record.read_evidence != reader.evidence
        or record.review.reviewed_head_sha != identity.head_sha
    ):
        raise ValueError("snapshot completion evidence mismatch")
    return ReaderSnapshotEntry(
        issue_number=identity.issue_number,
        claim_id=identity.claim_id,
        completion_id=record.completion_id,
        source_sha=identity.head_sha,
        evidence_digest=reader.evidence.digest,
        validations=reader.validations,
    )


def _reader_key(entry: Any) -> tuple[int, str]:
    return entry.issue_number, entry.claim_id


def _candidate_snapshot(
    ctx: Any,
    built: Any,
    previous: ParentReadSnapshotV1 | None,
    report: StaleReadReportV1,
) -> ParentReadSnapshotV1:
    entries = {_reader_key(r): r for r in built.snapshot.readers}
    if previous is not None:
        for old in previous.readers:
            restored = entries.get(_reader_key(old))
            if (
                restored is None
                or replace(restored, validations=old.validations) != old
            ):
                raise ValueError(
                    "retained snapshot differs from restored reader history"
                )
            entries[_reader_key(old)] = old
    checkpoint = ValidationCheckpoint(
        candidate_head_sha=report.candidate_head_sha,
        report_digest=report.digest or "",
    )
    readers = []
    for reader in built.inputs.readers:
        key = _reader_key(reader.identity)
        entry = entries.pop(key, None) or _completed_entry(ctx, reader)
        if (
            entry.source_sha != reader.identity.head_sha
            or reader.evidence is None
            or entry.evidence_digest != reader.evidence.digest
        ):
            raise ValueError("snapshot reader differs from the reviewed report")
        validations = tuple(set(entry.validations + reader.validations + (checkpoint,)))
        readers.append(replace(entry, validations=validations))
    if entries:
        raise ValueError("snapshot contains readers absent from the reviewed report")
    return seal_snapshot(
        ParentReadSnapshotV1(
            repository=report.repository,
            parent_issue_number=report.parent_issue_number,
            parent_head_sha=report.candidate_head_sha,
            readers=tuple(readers),
        )
    )


def _verify_published(
    snapshot: ParentReadSnapshotV1, report: StaleReadReportV1
) -> None:
    expected = {
        (r.issue_number, r.claim_id, r.head_sha, r.evidence_digest)
        for r in report.readers
    }
    actual = {
        (r.issue_number, r.claim_id, r.source_sha, r.evidence_digest)
        for r in snapshot.readers
    }
    checkpoint = ValidationCheckpoint(
        candidate_head_sha=report.candidate_head_sha,
        report_digest=report.digest or "",
    )
    if actual != expected or any(
        checkpoint not in r.validations for r in snapshot.readers
    ):
        raise ValueError("published snapshot differs from the passed head/report")


def prepare_parent_read_publication(ctx: Any) -> ReadPublication:
    """Recheck frozen inputs and prepare one update, never publish or re-review.

    Missing refs bootstrap only from the existing complete history builder.
    A restarted attempt may adopt H only with its retained reader checkpoints.
    """
    report = _passed_report(ctx)
    root, parent = ctx.repository_root, report.parent_issue_number
    pair = _remote_pair(root, parent)
    if pair[0] not in {report.parent_sha, report.candidate_head_sha}:
        raise ValueError("parent advanced; stale report cannot be republished")
    previous = None
    if pair[1] is not None:
        previous = read_snapshot(report.repository, parent, pair[0], cwd=root)
    if pair[0] == report.candidate_head_sha:
        if previous is None:
            raise ValueError("published parent lacks a matching snapshot")
        _verify_published(previous, report)
        if _remote_pair(root, parent) != pair:
            raise ValueError("parent/snapshot changed during recovery")
        return ReadPublication(recovered=True)
    built = make_default_read_context_builder(ctx.forge, root)(
        ctx.semantic_review_state, report.candidate_head_sha
    )
    if (
        built.inputs is None
        or built.snapshot is None
        or built.unknowns
        or detect_stale_reads(built.inputs) != report
    ):
        raise ValueError("snapshot inputs differ from the passed read report")
    snapshot = _candidate_snapshot(ctx, built, previous, report)
    write = prepare_snapshot_write(
        snapshot,
        parent_head_sha=report.parent_sha,
        cwd=root,
        read_evidence=tuple(r.evidence for r in built.inputs.readers if r.evidence),
        bootstrap=built.bootstrap,
    )
    if write.expected_oid != pair[1] or _remote_pair(root, parent) != pair:
        raise ValueError("parent/snapshot changed during preparation")
    return ReadPublication(write=write)


def reconcile_parent_read_publication(ctx: Any, write: SnapshotWrite) -> bool:
    """Confirm both remote refs, snapshot digest and retained objects after push."""
    report = _passed_report(ctx)
    pair = (write.parent_head_sha, write.new_commit_oid)
    root, parent = ctx.repository_root, report.parent_issue_number
    if _remote_pair(root, parent) != pair:
        return False
    snapshot = read_snapshot(report.repository, parent, write.parent_head_sha, cwd=root)
    _verify_published(snapshot, report)
    return (
        snapshot.digest == write.snapshot_digest and _remote_pair(root, parent) == pair
    )
