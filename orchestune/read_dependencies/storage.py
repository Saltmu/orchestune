"""Durable Git storage for parent reader snapshots, without publishing refs.

The data tree contains snapshot.json and evidence-<digest>.json blobs. Commit
parents retain the previous data commit and every referenced input/source/head.
Only the caller's later atomic push publishes a prepared ``SnapshotWrite``.
Missing or inconsistent data raises ``SnapshotStorageError`` (unknown), never
an empty snapshot. Bootstrap is an explicit assertion by the recovery caller.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from orchestune.infra.git_cli import run_git
from orchestune.read_dependencies.codec import (
    parse_read_evidence,
    read_evidence_to_dict,
)
from orchestune.read_dependencies.contracts import (
    ParentReadSnapshotV1,
    ReaderSnapshotEntry,
    ReadEvidenceV1,
    ResourcePresence,
    SnapshotWrite,
    UnknownReason,
    parent_read_snapshot_ref,
)
from orchestune.read_dependencies.git import lookup_resource
from orchestune.read_dependencies.snapshot_codec import (
    parent_read_snapshot_to_dict,
    parse_parent_read_snapshot,
)

_SHA = re.compile(r"^[0-9a-f]{40}$")


class SnapshotStorageError(ValueError):
    """Unknown storage result, with a reason suitable for the read gate."""

    def __init__(self, reason: UnknownReason, detail: str) -> None:
        self.reason = reason
        super().__init__(f"{reason.value}: {detail}")


@dataclass(frozen=True)
class _StoredSnapshot:
    oid: str
    snapshot: ParentReadSnapshotV1
    evidence: dict[str, ReadEvidenceV1]


def read_snapshot(
    repository: str,
    parent_issue_number: int,
    parent_head_sha: str,
    *,
    cwd: str | Path,
    remote: str = "origin",
) -> ParentReadSnapshotV1:
    """Fetch and validate the remote snapshot matching this repository/parent.

    The caller supplies the observed parent ref head. Even a valid snapshot
    for a different head is unknown after manual or forced parent updates.
    No local or remote refs, index, or worktree files are changed.
    """
    expected = ParentReadSnapshotV1(
        repository=repository,
        parent_issue_number=parent_issue_number,
        parent_head_sha=parent_head_sha,
    )
    stored = _read_current(expected, cwd, remote)
    if stored is None:
        raise SnapshotStorageError(
            UnknownReason.OBJECT_MISSING, "snapshot ref is absent"
        )
    return stored.snapshot


def prepare_snapshot_write(
    snapshot: ParentReadSnapshotV1,
    *,
    parent_head_sha: str,
    cwd: str | Path,
    read_evidence: Iterable[ReadEvidenceV1] = (),
    remote: str = "origin",
    bootstrap: bool = False,
) -> SnapshotWrite:
    """Prepare data for candidate ``snapshot.parent_head_sha``, without a push.

    ``parent_head_sha`` is the old parent P, distinct from the candidate H.
    Missing refs require explicit ``bootstrap=True`` after caller recovery.
    Supply evidence for new readers; existing readers reuse stored evidence.
    The returned expected OID must be used with the later atomic parent push.
    """
    sealed = parse_parent_read_snapshot(parent_read_snapshot_to_dict(snapshot))
    previous = _read_current(
        ParentReadSnapshotV1(
            repository=sealed.repository,
            parent_issue_number=sealed.parent_issue_number,
            parent_head_sha=parent_head_sha,
        ),
        cwd,
        remote,
    )
    if previous is None and not bootstrap:
        raise SnapshotStorageError(
            UnknownReason.OBJECT_MISSING, "bootstrap recovery required"
        )
    evidence = dict(previous.evidence) if previous else {}
    for item in read_evidence:
        parsed = parse_read_evidence(read_evidence_to_dict(item))
        assert parsed.digest is not None
        evidence[parsed.digest] = parsed
    if previous:
        _check_preserved(previous.snapshot, sealed)
    selected = _select_evidence(sealed, evidence)
    commits = _retained_commits(sealed, selected.values())
    if previous:
        commits.add(previous.oid)
    for oid in sorted(commits):
        _require_commit(oid, cwd)
    _check_observations(selected.values(), cwd)
    new_oid = _write_commit(sealed, selected, commits, cwd)
    assert sealed.digest is not None
    return SnapshotWrite(
        parent_issue_number=sealed.parent_issue_number,
        expected_oid=previous.oid if previous else None,
        new_commit_oid=new_oid,
        parent_head_sha=sealed.parent_head_sha,
        snapshot_digest=sealed.digest,
    )


def _git(args: list[str], cwd: str | Path) -> str:
    try:
        return run_git(args, cwd=cwd).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise SnapshotStorageError(
            UnknownReason.FETCH_FAILED, "Git operation failed"
        ) from error


def _remote_oid(ref: str, remote: str, cwd: str | Path) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", remote):
        raise ValueError("remote must be a configured remote name")
    output = _git(["ls-remote", "--refs", remote, ref], cwd)
    if not output:
        return None
    fields = output.strip().split()
    if len(fields) != 2 or fields[1] != ref or not _SHA.fullmatch(fields[0]):
        raise SnapshotStorageError(
            UnknownReason.EVIDENCE_MISMATCH, "invalid snapshot ref"
        )
    return fields[0]


def _read_current(
    expected: ParentReadSnapshotV1, cwd: str | Path, remote: str
) -> _StoredSnapshot | None:
    oid = _remote_oid(
        parent_read_snapshot_ref(expected.parent_issue_number), remote, cwd
    )
    if oid is None:
        return None
    _git(["fetch", "--no-tags", "--no-write-fetch-head", remote, oid], cwd)
    _require_commit(oid, cwd)
    try:
        snapshot = parse_parent_read_snapshot(_read_json(oid, "snapshot.json", cwd))
        _check_expected(snapshot, expected)
        evidence = {
            reader.evidence_digest: parse_read_evidence(
                _read_json(oid, f"evidence-{reader.evidence_digest}.json", cwd)
            )
            for reader in snapshot.readers
        }
        selected = _select_evidence(snapshot, evidence)
        for commit in sorted(_retained_commits(snapshot, selected.values())):
            _require_commit(commit, cwd)
            _git(["merge-base", "--is-ancestor", commit, oid], cwd)
        _check_observations(selected.values(), cwd)
    except (TypeError, ValueError) as error:
        if isinstance(error, SnapshotStorageError):
            raise
        raise SnapshotStorageError(
            UnknownReason.EVIDENCE_MISMATCH, "invalid snapshot data"
        ) from error
    return _StoredSnapshot(oid, snapshot, selected)


def _read_json(oid: str, name: str, cwd: str | Path) -> object:
    return json.loads(_git(["cat-file", "blob", f"{oid}:{name}"], cwd))


def _check_expected(
    value: ParentReadSnapshotV1, expected: ParentReadSnapshotV1
) -> None:
    if (
        value.repository != expected.repository
        or value.parent_issue_number != expected.parent_issue_number
        or value.parent_head_sha != expected.parent_head_sha
    ):
        raise SnapshotStorageError(
            UnknownReason.PARENT_SNAPSHOT_MISMATCH,
            "snapshot repository/parent/head mismatch",
        )


def _select_evidence(
    snapshot: ParentReadSnapshotV1, evidence: dict[str, ReadEvidenceV1]
) -> dict[str, ReadEvidenceV1]:
    selected: dict[str, ReadEvidenceV1] = {}
    identities: set[tuple[int, str]] = set()
    for reader in snapshot.readers:
        item = evidence.get(reader.evidence_digest)
        identity = (reader.issue_number, reader.claim_id)
        if (
            item is None
            or identity in identities
            or item.repository != snapshot.repository
            or item.issue_number != reader.issue_number
            or item.claim_id != reader.claim_id
            or item.head_sha != reader.source_sha
            or item.digest != reader.evidence_digest
        ):
            raise SnapshotStorageError(
                UnknownReason.EVIDENCE_MISMATCH, "reader evidence mismatch"
            )
        identities.add(identity)
        selected[reader.evidence_digest] = item
    return selected


def _check_preserved(old: ParentReadSnapshotV1, new: ParentReadSnapshotV1) -> None:
    readers = {(item.issue_number, item.claim_id): item for item in new.readers}
    for item in old.readers:
        updated = readers.get((item.issue_number, item.claim_id))
        if updated is None or not _preserves_reader(item, updated):
            raise SnapshotStorageError(
                UnknownReason.EVIDENCE_MISMATCH, "original reader was changed"
            )


def _preserves_reader(old: ReaderSnapshotEntry, new: ReaderSnapshotEntry) -> bool:
    return (
        old.source_sha == new.source_sha
        and old.evidence_digest == new.evidence_digest
        and old.completion_id == new.completion_id
        and set(old.validations).issubset(new.validations)
    )


def _retained_commits(
    snapshot: ParentReadSnapshotV1, evidence: Iterable[ReadEvidenceV1]
) -> set[str]:
    commits = {snapshot.parent_head_sha}
    for reader in snapshot.readers:
        commits.add(reader.source_sha)
        commits.update(point.candidate_head_sha for point in reader.validations)
    for item in evidence:
        commits.update((item.input_base_sha, item.head_sha))
        commits.update(
            obs.snapshot_commit
            for obs in item.observations
            if obs.snapshot_commit is not None
        )
    return commits


def _require_commit(oid: str, cwd: str | Path) -> None:
    resolved = _git(["rev-parse", "--verify", f"{oid}^{{commit}}"], cwd).strip()
    if resolved != oid:
        raise SnapshotStorageError(
            UnknownReason.OBJECT_MISSING, "required commit is missing"
        )


def _check_observations(evidence: Iterable[ReadEvidenceV1], cwd: str | Path) -> None:
    for item in evidence:
        for obs in item.observations:
            if obs.presence is ResourcePresence.UNKNOWN:
                continue
            assert obs.snapshot_commit is not None
            actual = lookup_resource(obs.snapshot_commit, obs.path, cwd=cwd)
            if (actual.presence, actual.blob_oid, actual.file_mode) != (
                obs.presence,
                obs.blob_oid,
                obs.file_mode,
            ):
                raise SnapshotStorageError(
                    UnknownReason.EVIDENCE_MISMATCH, "read object mismatch"
                )


def _write_commit(
    snapshot: ParentReadSnapshotV1,
    evidence: dict[str, ReadEvidenceV1],
    parents: set[str],
    cwd: str | Path,
) -> str:
    scratch = Path(cwd).resolve() / ".orchestune" / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="snapshot-", dir=scratch) as directory:
        path = Path(directory)
        payloads = {"snapshot.json": parent_read_snapshot_to_dict(snapshot)}
        payloads.update(
            {
                f"evidence-{digest}.json": read_evidence_to_dict(item)
                for digest, item in evidence.items()
            }
        )
        tree = _write_tree(payloads, path, cwd)
        message = path / "message"
        message.write_text("Parent reader snapshot v1\n", encoding="utf-8")
        args = ["commit-tree", tree, "-F", str(message)]
        for oid in sorted(parents):
            args.extend(("-p", oid))
        return _object_oid(_git(args, cwd))


def _write_tree(
    payloads: dict[str, dict[str, Any]], path: Path, cwd: str | Path
) -> str:
    entries = bytearray()
    for name, value in sorted(payloads.items()):
        file = path / name
        file.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
            encoding="utf-8",
        )
        oid = _object_oid(_git(["hash-object", "-w", "--no-filters", str(file)], cwd))
        entries.extend(b"100644 " + name.encode("ascii") + b"\0" + bytes.fromhex(oid))
    tree = path / "tree"
    tree.write_bytes(entries)
    return _object_oid(
        _git(["hash-object", "-w", "-t", "tree", "--no-filters", str(tree)], cwd)
    )


def _object_oid(output: str) -> str:
    oid = output.strip()
    if not _SHA.fullmatch(oid):
        raise SnapshotStorageError(
            UnknownReason.OBJECT_MISSING, "invalid Git object OID"
        )
    return oid
