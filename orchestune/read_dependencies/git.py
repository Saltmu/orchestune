"""Read Git snapshots and report resource changes for stale-read checks.

All commands use the shared Git adapter. Git output is NUL-delimited so spaces
and other allowed path characters remain distinct; undecodable or incomplete
output is reported as unknown instead of being treated as no change.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

from orchestune.infra.git_cli import run_git
from orchestune.read_dependencies.contracts import (
    ChangedResource,
    ChangeKind,
    ObservationAvailability,
    ReadObservationV1,
    ReadSource,
    ResourcePresence,
    UnknownReason,
    normalize_read_path,
)

_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_REPLACEMENT_CHARACTER = "\ufffd"


def lookup_resource(
    snapshot_sha: str,
    path: str,
    *,
    cwd: str | Path,
) -> ReadObservationV1:
    """Look up one concrete file at ``snapshot_sha``.

    A missing path is known absent only after its commit tree was read
    successfully. Directories, ambiguous output, missing Git objects, and
    command failures are unknown; malformed path syntax is rejected.
    """
    normalized_path = normalize_read_path(path)
    sha = _normalize_sha(snapshot_sha)
    if sha is None or _REPLACEMENT_CHARACTER in normalized_path:
        return _unknown_observation(normalized_path)

    if not _commit_exists(sha, cwd):
        return _unknown_observation(normalized_path)
    records = _lookup_tree_records(sha, normalized_path, cwd)
    if records is None:
        return _unknown_observation(normalized_path)
    if not records:
        return ReadObservationV1(
            path=normalized_path,
            snapshot_commit=sha,
            presence=ResourcePresence.ABSENT,
            blob_oid=None,
            file_mode=None,
            source=ReadSource.ADAPTER_OBSERVED,
        )
    if len(records) != 1:
        return _unknown_observation(normalized_path)

    mode, object_type, oid, observed_path = records[0]
    if (
        observed_path != normalized_path
        or _REPLACEMENT_CHARACTER in observed_path
        or object_type not in {"blob", "commit"}
    ):
        return _unknown_observation(normalized_path)
    return ReadObservationV1(
        path=normalized_path,
        snapshot_commit=sha,
        presence=ResourcePresence.PRESENT,
        blob_oid=oid,
        file_mode=mode,
        source=ReadSource.ADAPTER_OBSERVED,
    )


def observe_changed_resources(
    old_sha: str,
    new_sha: str,
    paths: Iterable[str],
    *,
    cwd: str | Path,
) -> tuple[ChangedResource, ...]:
    """Return Git changes between two recorded snapshots for concrete paths.

    ``paths`` is a file allowlist, not a glob or directory walk. A failed,
    incomplete, or ambiguous comparison returns an unknown resource for every
    requested path so callers cannot mistake missing evidence for a clean diff.
    """
    old = _normalize_sha(old_sha)
    new = _normalize_sha(new_sha)
    normalized_paths = _normalize_paths(paths)
    if not normalized_paths:
        return ()
    if old is None or new is None:
        return _unknown_changes(
            normalized_paths, old, new, UnknownReason.OBJECT_MISSING
        )
    if any(_REPLACEMENT_CHARACTER in path for path in normalized_paths):
        return _unknown_changes(
            normalized_paths, old, new, UnknownReason.EVIDENCE_MISMATCH
        )

    shallow = _is_shallow(cwd)
    if shallow is None:
        return _unknown_changes(normalized_paths, old, new, UnknownReason.FETCH_FAILED)
    diff_output = _run_diff(old, new, normalized_paths, cwd)
    if diff_output is None:
        reason = (
            UnknownReason.SHALLOW_HISTORY if shallow else _missing_object_reason(cwd)
        )
        return _unknown_changes(
            normalized_paths,
            old,
            new,
            reason,
        )
    evidence_commits = tuple(sorted({old, new}))
    parsed = _parse_raw_diff(diff_output, evidence_commits)
    if parsed is None:
        return _unknown_changes(
            normalized_paths, old, new, UnknownReason.EVIDENCE_MISMATCH
        )
    selected = set(normalized_paths)
    changes = tuple(
        change
        for change in parsed
        if change.path in selected or change.old_path in selected
    )
    return tuple(
        sorted(changes, key=lambda change: (change.path, change.old_path or ""))
    )


def _normalize_sha(value: str) -> str | None:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        return None
    return value.lower()


def _normalize_paths(paths: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({normalize_read_path(path) for path in paths}))


def _commit_exists(sha: str, cwd: str | Path) -> bool:
    try:
        result = run_git(
            ["rev-parse", "--verify", f"{sha}^{{commit}}"], cwd=cwd, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip().lower() == sha


def _lookup_tree_records(
    sha: str, path: str, cwd: str | Path
) -> list[tuple[str, str, str, str]] | None:
    try:
        result = run_git(
            ["ls-tree", "-z", "--full-tree", sha, "--", f":(literal){path}"],
            cwd=cwd,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    return _parse_tree_records(result.stdout)


def _is_shallow(cwd: str | Path) -> bool | None:
    try:
        result = run_git(["rev-parse", "--is-shallow-repository"], cwd=cwd, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    return result.stdout.strip().lower() == "true"


def _run_diff(
    old_sha: str,
    new_sha: str,
    paths: tuple[str, ...],
    cwd: str | Path,
) -> str | None:
    try:
        result = run_git(
            [
                "diff",
                "--raw",
                "--no-abbrev",
                "--find-renames",
                "-z",
                old_sha,
                new_sha,
                "--",
                *(f":(literal){path}" for path in paths),
            ],
            cwd=cwd,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _unknown_observation(path: str) -> ReadObservationV1:
    return ReadObservationV1(
        path=path,
        snapshot_commit=None,
        presence=ResourcePresence.UNKNOWN,
        blob_oid=None,
        file_mode=None,
        source=ReadSource.ADAPTER_OBSERVED,
    )


def _unknown_changes(
    paths: tuple[str, ...],
    old_sha: str | None,
    new_sha: str | None,
    reason: UnknownReason,
) -> tuple[ChangedResource, ...]:
    commits = tuple(sha for sha in (old_sha, new_sha) if sha is not None)
    return tuple(
        ChangedResource(
            path=path,
            old_path=None,
            kind=ChangeKind.MODIFY,
            old_blob_oid=None,
            new_blob_oid=None,
            old_file_mode=None,
            new_file_mode=None,
            evidence_commits=commits,
            availability=ObservationAvailability.UNKNOWN,
            unknown_reason=reason,
        )
        for path in paths
    )


def _parse_tree_records(output: str) -> list[tuple[str, str, str, str]] | None:
    if not output:
        return []
    if not output.endswith("\0"):
        return None
    records: list[tuple[str, str, str, str]] = []
    for record in output[:-1].split("\0"):
        if not record:
            return None
        try:
            metadata, path = record.split("\t", 1)
            mode, object_type, oid = metadata.split(" ")
        except ValueError:
            return None
        if not re.fullmatch(r"[0-7]{6}", mode) or not _SHA.fullmatch(oid):
            return None
        records.append((mode, object_type, oid.lower(), path))
    return records


def _parse_raw_diff(
    output: str, evidence_commits: tuple[str, ...]
) -> tuple[ChangedResource, ...] | None:
    if not output:
        return ()
    if not output.endswith("\0"):
        return None
    fields = output[:-1].split("\0")
    changes: list[ChangedResource] = []
    index = 0
    while index < len(fields):
        parsed = _parse_raw_record(fields, index, evidence_commits)
        if parsed is None:
            return None
        change, index = parsed
        changes.append(change)
    return tuple(changes)


def _parse_raw_record(
    fields: list[str], index: int, evidence_commits: tuple[str, ...]
) -> tuple[ChangedResource, int] | None:
    header = fields[index]
    if not header.startswith(":"):
        return None
    try:
        old_mode, new_mode, old_oid, new_oid, status = header[1:].split()
    except ValueError:
        return None
    index += 1
    if index >= len(fields) or _REPLACEMENT_CHARACTER in fields[index]:
        return None
    old_path: str | None = None
    new_path = fields[index]
    index += 1
    if status.startswith(("R", "C")):
        if index >= len(fields) or _REPLACEMENT_CHARACTER in fields[index]:
            return None
        old_path, new_path = new_path, fields[index]
        index += 1
    kind = _change_kind(status, old_oid, new_oid, old_mode, new_mode)
    if kind is None:
        return None
    try:
        change = ChangedResource(
            path=new_path,
            old_path=old_path if kind is ChangeKind.RENAME else None,
            kind=kind,
            old_blob_oid=None if old_oid == "0" * 40 else old_oid.lower(),
            new_blob_oid=None if new_oid == "0" * 40 else new_oid.lower(),
            old_file_mode=None if old_mode == "000000" else old_mode,
            new_file_mode=None if new_mode == "000000" else new_mode,
            evidence_commits=evidence_commits,
            availability=ObservationAvailability.RESOLVED,
        )
    except ValueError:
        return None
    return change, index


def _change_kind(
    status: str,
    old_oid: str,
    new_oid: str,
    old_mode: str,
    new_mode: str,
) -> ChangeKind | None:
    if status == "A":
        return ChangeKind.ADD
    if status == "D":
        return ChangeKind.DELETE
    if status.startswith("R"):
        return ChangeKind.RENAME
    if status != "M" and not status.startswith("T"):
        return None
    if old_oid == new_oid and old_mode != new_mode:
        return ChangeKind.MODE
    if old_oid != new_oid:
        return ChangeKind.MODIFY
    return None


def _missing_object_reason(cwd: str | Path) -> UnknownReason:
    shallow = _is_shallow(cwd)
    if shallow is None:
        return UnknownReason.FETCH_FAILED
    if shallow:
        return UnknownReason.SHALLOW_HISTORY
    return UnknownReason.OBJECT_MISSING
