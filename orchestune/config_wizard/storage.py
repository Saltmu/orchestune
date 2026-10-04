"""Safe atomic configuration storage with file locking, backups, and conflict detection."""

from __future__ import annotations

import datetime
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from orchestune.config_wizard.document import (
    ConfigSnapshot,
    verify_snapshot_consistency,
)
from orchestune.infra.process_utils import FileLock


class ConfigConflictError(Exception):
    """Raised when concurrent modification is detected (exit code 3)."""


class ConfigStorageError(Exception):
    """Raised when an I/O or storage operation fails (exit code 1)."""


@dataclass(frozen=True)
class SaveReceipt:
    committed: bool
    backup_path: Path | None
    fsync_confirmed: bool
    target_path: Path
    error_message: str | None = None


def _create_backup(project_dir: Path, snapshot: ConfigSnapshot, unique_id: str) -> Path:
    now_utc = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = project_dir / f"orchestune.toml.bak.{now_utc}.{unique_id}"
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(backup_path, flags, 0o600)
        assert snapshot.raw_bytes is not None
        with os.fdopen(fd, "wb") as f:
            f.write(snapshot.raw_bytes)
            f.flush()
            os.fsync(f.fileno())
        return backup_path
    except OSError as exc:
        raise ConfigStorageError(
            f"Failed to create configuration backup at '{backup_path}': {exc}"
        ) from exc


def _write_temporary_candidate(
    temp_path: Path, candidate_bytes: bytes, temp_mode: int
) -> None:
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(temp_path, flags, temp_mode)
        with os.fdopen(fd, "wb") as f:
            f.write(candidate_bytes)
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        raise ConfigStorageError(
            f"Failed to write candidate configuration to '{temp_path}': {exc}"
        ) from exc


def _commit_candidate(
    temp_path: Path, target_path: Path, mode: Literal["init", "edit"]
) -> bool:
    if mode == "init":
        try:
            os.link(temp_path, target_path)
            return True
        except FileExistsError as exc:
            raise ConfigConflictError(
                f"File '{target_path}' was created by another process just before commit"
            ) from exc
        except OSError:
            if target_path.exists():
                raise ConfigConflictError(
                    f"File '{target_path}' already exists; init cannot overwrite"
                ) from None
            os.replace(temp_path, target_path)
            return True
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
    else:
        os.replace(temp_path, target_path)
        return True


def _fsync_directory(project_dir: Path) -> bool:
    try:
        dir_fd = os.open(project_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return True
    except OSError:
        return False


def _acquire_config_lock(project_dir: Path) -> FileLock:
    lock_dir = project_dir / ".orchestune"
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigStorageError(
            f"Failed to create lock directory '{lock_dir}': {exc}"
        ) from exc

    lock_file = lock_dir / "config-write.lock"
    try:
        return FileLock(lock_file, timeout=10.0)
    except Exception as exc:
        raise ConfigStorageError(
            f"Failed to initialize lock for '{lock_file}': {exc}"
        ) from exc


def _persist_under_lock(
    target_path: Path,
    project_dir: Path,
    snapshot: ConfigSnapshot,
    candidate_bytes: bytes,
    mode: Literal["init", "edit"],
) -> SaveReceipt:
    consistent, reason = verify_snapshot_consistency(snapshot)
    if not consistent:
        raise ConfigConflictError(reason)

    unique_id = uuid.uuid4().hex[:8]
    backup_path: Path | None = None
    if mode == "edit" and snapshot.exists and snapshot.raw_bytes is not None:
        backup_path = _create_backup(project_dir, snapshot, unique_id)

    temp_path = project_dir / f"orchestune.toml.tmp.{unique_id}"
    temp_mode = (
        (snapshot.mode & 0o777)
        if (snapshot.exists and snapshot.mode is not None)
        else 0o600
    )
    _write_temporary_candidate(temp_path, candidate_bytes, temp_mode)

    try:
        consistent_before, reason_before = verify_snapshot_consistency(snapshot)
        if not consistent_before:
            raise ConfigConflictError(reason_before)

        committed = _commit_candidate(temp_path, target_path, mode)
        fsync_confirmed = _fsync_directory(project_dir)

        return SaveReceipt(
            committed=committed,
            backup_path=backup_path,
            fsync_confirmed=fsync_confirmed,
            target_path=target_path,
        )
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def save_config_document(
    target_path: Path,
    snapshot: ConfigSnapshot,
    candidate_bytes: bytes,
    mode: Literal["init", "edit"],
) -> SaveReceipt:
    """Safely persist configuration bytes with locking, backup, and conflict detection."""
    project_dir = target_path.parent
    lock = _acquire_config_lock(project_dir)
    with lock:
        return _persist_under_lock(
            target_path, project_dir, snapshot, candidate_bytes, mode
        )
