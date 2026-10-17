from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, TypeGuard

from orchestune.infra.process_utils import FileLock
from orchestune.installer.contracts import (
    BundlePayload,
    ConflictError,
    ManifestError,
    TransactionError,
)
from orchestune.installer.payload import calculate_file_sha256
from orchestune.installer.state import (
    LOCK_FILENAME,
    TRANSACTIONS_DIR,
    BundleManifestEntry,
    InstallerManifest,
    get_installer_dir,
    load_manifest,
    save_manifest,
)

_UNINSTALL_JOURNAL_SCHEMA_VERSION = 1
_UNINSTALL_JOURNAL_STATUSES = {
    "UNINSTALL_PREPARED",
    "UNINSTALL_STAGED",
    "MANIFEST_COMMITTED",
}


@dataclass(frozen=True)
class UninstallPlan:
    tx_id: str
    bundle_name: str
    manifest_generation_before: int
    entry_before: BundleManifestEntry
    remaining_consumers: list[str]
    skills: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _UNINSTALL_JOURNAL_SCHEMA_VERSION,
            "operation": "uninstall",
            "tx_id": self.tx_id,
            "bundle_name": self.bundle_name,
            "manifest_generation_before": self.manifest_generation_before,
            "entry_before": asdict(self.entry_before),
            "remaining_consumers": list(self.remaining_consumers),
            "skills": list(self.skills),
        }


@dataclass(frozen=True)
class UninstallJournal:
    plan: UninstallPlan
    status: str
    moved_to_backup: list[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UninstallJournal:
        if data.get("operation") != "uninstall":
            raise TransactionError("Invalid uninstall journal operation")
        schema_version = data.get("schema_version")
        if (
            not _is_plain_int(schema_version)
            or schema_version != _UNINSTALL_JOURNAL_SCHEMA_VERSION
        ):
            raise TransactionError("unsupported uninstall journal schema version")
        status = data.get("status")
        if not isinstance(status, str) or status not in _UNINSTALL_JOURNAL_STATUSES:
            raise TransactionError(f"Invalid uninstall journal status: {status}")

        tx_id = data.get("tx_id")
        bundle_name = data.get("bundle_name")
        generation = data.get("manifest_generation_before")
        if not isinstance(tx_id, str) or not tx_id:
            raise TransactionError("Invalid uninstall journal transaction id")
        if not isinstance(bundle_name, str) or not bundle_name:
            raise TransactionError("Invalid uninstall journal bundle name")
        if not _is_plain_int(generation) or generation < 1:
            raise TransactionError("Invalid uninstall journal manifest generation")

        snapshot = _journal_entry_snapshot(bundle_name, data.get("entry_before"))
        remaining = _journal_string_list(
            data.get("remaining_consumers"), "remaining_consumers"
        )
        skills = _journal_string_list(data.get("skills"), "skills")
        moved = _journal_string_list(data.get("moved_to_backup"), "moved_to_backup")
        _validate_uninstall_plan(snapshot, remaining, skills, moved)

        return cls(
            plan=UninstallPlan(
                tx_id=tx_id,
                bundle_name=bundle_name,
                manifest_generation_before=generation,
                entry_before=snapshot,
                remaining_consumers=remaining,
                skills=skills,
            ),
            status=status,
            moved_to_backup=moved,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.plan.to_dict(),
            "status": self.status,
            "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "moved_to_backup": list(self.moved_to_backup),
        }


def _is_plain_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _journal_entry_snapshot(bundle_name: str, entry_data: Any) -> BundleManifestEntry:
    if not isinstance(entry_data, dict):
        raise TransactionError("Invalid uninstall journal entry snapshot")
    try:
        entry = InstallerManifest.from_dict(
            {
                "schema_version": 1,
                "owner": "orchestune",
                "bundles": {bundle_name: entry_data},
            }
        ).bundles[bundle_name]
    except Exception as err:
        raise TransactionError(
            f"Invalid uninstall journal entry snapshot: {err}"
        ) from err
    if not isinstance(entry.consumers, list) or any(
        not isinstance(item, str) for item in entry.consumers
    ):
        raise TransactionError("Invalid uninstall journal entry consumers")
    return entry


def _validate_uninstall_plan(
    entry: BundleManifestEntry,
    remaining: list[str],
    skills: list[str],
    moved: list[str],
) -> None:
    for skill_name, skill_info in entry.skills.items():
        _validate_skill_name(skill_name)
        _validate_skill_manifest(skill_info)
    for skill_name in skills:
        _validate_skill_name(skill_name)
    if set(remaining).difference(entry.consumers):
        raise TransactionError("Uninstall journal adds an unknown consumer")
    if set(remaining) == set(entry.consumers):
        raise TransactionError("Uninstall journal does not remove a consumer")
    expected_skills = set(entry.skills) if not remaining else set()
    if set(skills) != expected_skills:
        raise TransactionError(
            "Uninstall journal skills do not match its entry snapshot"
        )
    if not set(moved).issubset(skills):
        raise TransactionError("Uninstall journal records an unknown backup target")


def _journal_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TransactionError(f"Invalid uninstall journal {field}")
    if len(set(value)) != len(value):
        raise TransactionError(f"Duplicate values in uninstall journal {field}")
    return list(value)


def _validate_skill_name(name: str) -> None:
    windows_path = PureWindowsPath(name)
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or ":" in name
        or Path(name).name != name
        or windows_path.drive
        or windows_path.is_absolute()
    ):
        raise TransactionError(f"Invalid uninstall skill path: {name!r}")


def _validate_relative_file_path(name: str) -> None:
    posix_path = PurePosixPath(name)
    windows_path = PureWindowsPath(name)
    if (
        not name
        or "\\" in name
        or posix_path.is_absolute()
        or windows_path.drive
        or windows_path.is_absolute()
        or any(part in {".", ".."} for part in posix_path.parts)
    ):
        raise TransactionError(f"Invalid uninstall file path: {name!r}")


def _validate_skill_manifest(skill_info: Any) -> None:
    if not isinstance(skill_info, dict):
        raise TransactionError("Invalid uninstall skill manifest")
    files = skill_info.get("files")
    if not isinstance(files, dict):
        raise TransactionError("Invalid uninstall skill files")
    directories = skill_info.get("directories")
    if not isinstance(directories, list) or any(
        not isinstance(directory, str) for directory in directories
    ):
        raise TransactionError("Invalid uninstall skill directories")
    for directory in directories:
        _validate_relative_file_path(directory)
    for relative_path, file_info in files.items():
        if not isinstance(relative_path, str) or not isinstance(file_info, dict):
            raise TransactionError("Invalid uninstall file entry")
        _validate_relative_file_path(relative_path)
        if not isinstance(file_info.get("sha256"), str):
            raise TransactionError("Invalid uninstall file digest")


def _skill_matches_manifest(
    skill_dir: Path, skill_info: dict[str, Any], *, allow_missing: bool = False
) -> bool:
    if not skill_dir.is_dir() or skill_dir.is_symlink():
        return False
    files = skill_info.get("files")
    expected_directories = skill_info.get("directories")
    if not isinstance(files, dict) or not isinstance(expected_directories, list):
        return False
    expected = set(files)
    expected_directory_set = set(expected_directories)
    found: set[str] = set()
    found_directories: set[str] = set()
    try:
        for root, child_directories, filenames in os.walk(skill_dir, followlinks=False):
            current = Path(root)
            for name in child_directories:
                directory_path = current / name
                if directory_path.is_symlink():
                    return False
                relative_directory = directory_path.relative_to(skill_dir).as_posix()
                if relative_directory not in expected_directory_set:
                    return False
                found_directories.add(relative_directory)
            for filename in filenames:
                file_path = current / filename
                if file_path.is_symlink() or not file_path.is_file():
                    return False
                relative_path = file_path.relative_to(skill_dir).as_posix()
                if relative_path not in expected:
                    return False
                found.add(relative_path)
                file_info = files[relative_path]
                if not isinstance(file_info, dict):
                    return False
                if calculate_file_sha256(file_path) != file_info.get("sha256"):
                    return False
    except OSError:
        return False
    if allow_missing:
        return True
    if found != expected or found_directories != expected_directory_set:
        return False
    return True


class SkillTransaction:
    def __init__(self, skills_root: Path) -> None:
        self.skills_root = skills_root.resolve()
        self.installer_dir = get_installer_dir(self.skills_root)
        self.lock_path = self.installer_dir / LOCK_FILENAME
        self.lock: FileLock | None = None
        self.tx_id = uuid.uuid4().hex
        self.tx_dir = self.installer_dir / TRANSACTIONS_DIR / self.tx_id
        self.stage_dir = self.tx_dir / "stage"
        self.backup_dir = self.tx_dir / "backup"
        self.journal_path = self.tx_dir / "journal.json"
        self.status = "INITIALIZED"
        self.journal_data: dict[str, Any] = {}
        self.moved_to_backup: list[str] = []
        self.published_skills: list[str] = []
        self.uninstall_journal: UninstallJournal | None = None
        self.recovered_uninstalls: list[str] = []

    def __enter__(self) -> SkillTransaction:
        self.installer_dir.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(self.lock_path, timeout=10.0)
        self.lock.acquire()
        try:
            self.recovered_uninstalls = recover_pending_transactions(
                self.skills_root, skip_tx_id=self.tx_id
            )
            self.tx_dir.mkdir(parents=True, exist_ok=True)
        except BaseException:
            if self.lock is not None:
                self.lock.release()
                self.lock = None
            raise
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            if exc_type is not None:
                try:
                    self.rollback()
                except Exception as rollback_err:
                    raise TransactionError(
                        f"Rollback failed after {exc_type.__name__}: {rollback_err}"
                    ) from exc_val
                # Note: If an error occurred after save_manifest (e.g. journal write error),
                # rollback() detects that the manifest was committed and leaves it intact.
                # Propagating the original exception alerts the caller that a post-commit
                # journal step failed, while keeping root state consistent.
            elif self.status == "MANIFEST_COMMITTED":
                if self.uninstall_journal is None:
                    self.cleanup()
                else:
                    _cleanup_committed_uninstall(self.tx_dir, self.uninstall_journal)
            elif not self.journal_path.exists():
                if self.tx_dir.exists():
                    try:
                        self.tx_dir.rmdir()
                    except OSError as err:
                        raise TransactionError(
                            f"Could not remove empty transaction directory {self.tx_dir}: {err}"
                        ) from err
        finally:
            if self.lock is not None:
                self.lock.release()
                self.lock = None

    def _write_journal(self, status: str, details: dict[str, Any]) -> None:
        self.status = status
        self.journal_data = {
            "status": status,
            "tx_id": self.tx_id,
            "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            **details,
        }
        content = json.dumps(self.journal_data, indent=2)
        with tempfile.NamedTemporaryFile(
            "w", dir=self.tx_dir, delete=False, encoding="utf-8"
        ) as tf:
            tf.write(content)
            tf.flush()
            os.fsync(tf.fileno())
            temp_name = tf.name
        os.replace(temp_name, self.journal_path)

    def _write_uninstall_journal(self, journal: UninstallJournal) -> None:
        self.uninstall_journal = journal
        self.status = journal.status
        self.journal_data = journal.to_dict()
        content = json.dumps(self.journal_data, indent=2)
        with tempfile.NamedTemporaryFile(
            "w",
            dir=self.tx_dir,
            prefix=".journal-",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tf:
            tf.write(content)
            tf.flush()
            os.fsync(tf.fileno())
            temp_name = tf.name
        os.replace(temp_name, self.journal_path)

    def prepare_stage(self, payload: BundlePayload) -> None:
        self.stage_dir.mkdir(parents=True, exist_ok=True)
        for skill_name, skill in payload.skills.items():
            src_skill_dir = payload.source_path / skill_name
            dest_skill_dir = self.stage_dir / skill_name
            shutil.copytree(src_skill_dir, dest_skill_dir)

            # Verify copied files
            for rel_file, file_record in skill.files.items():
                staged_file = dest_skill_dir / rel_file
                if not staged_file.is_file():
                    raise TransactionError(f"Staged file missing: {staged_file}")
                digest = calculate_file_sha256(staged_file)
                if digest != file_record.sha256:
                    raise TransactionError(
                        f"Staged file digest mismatch for {staged_file}"
                    )

        self._write_journal(
            "PREPARED",
            {
                "bundle_name": payload.bundle_name,
                "skills": list(payload.skills.keys()),
                "bundle_digest": payload.bundle_digest,
            },
        )

    def stage_publish(self, payload: BundlePayload) -> None:
        if self.status != "PREPARED":
            self.prepare_stage(payload)

        self.backup_dir.mkdir(parents=True, exist_ok=True)
        for skill_name in payload.skills.keys():
            existing = self.skills_root / skill_name
            if existing.exists() or existing.is_symlink():
                backup_dest = self.backup_dir / skill_name
                os.replace(existing, backup_dest)
                self.moved_to_backup.append(skill_name)
                self._write_journal(
                    "MOVING_BACKUP",
                    {
                        "bundle_name": payload.bundle_name,
                        "skills": list(payload.skills.keys()),
                        "moved_to_backup": list(self.moved_to_backup),
                    },
                )

        self._write_journal(
            "OLD_MOVED",
            {
                "bundle_name": payload.bundle_name,
                "skills": list(payload.skills.keys()),
                "moved_to_backup": list(self.moved_to_backup),
            },
        )

        self._write_journal(
            "PUBLISHING",
            {
                "bundle_name": payload.bundle_name,
                "skills": list(payload.skills.keys()),
                "moved_to_backup": list(self.moved_to_backup),
                "published_skills": list(payload.skills.keys()),
            },
        )

        for skill_name in payload.skills.keys():
            stage_src = self.stage_dir / skill_name
            publish_dest = self.skills_root / skill_name
            os.replace(stage_src, publish_dest)
            self.published_skills.append(skill_name)

        self._write_journal(
            "NEW_PUBLISHED",
            {
                "bundle_name": payload.bundle_name,
                "skills": list(payload.skills.keys()),
                "moved_to_backup": list(self.moved_to_backup),
                "published_skills": list(self.published_skills),
            },
        )

    def commit(
        self,
        payload: BundlePayload,
        manifest: InstallerManifest,
        consumers: list[str],
    ) -> None:
        if self.status != "NEW_PUBLISHED":
            self.stage_publish(payload)

        # Update manifest
        skills_manifest: dict[str, dict[str, Any]] = {}
        for s_name, s_payload in payload.skills.items():
            skills_manifest[s_name] = {
                "files": {
                    rel: {"sha256": f.sha256, "mode": f.mode}
                    for rel, f in s_payload.files.items()
                },
                "directories": s_payload.directories,
            }

        existing_entry = manifest.bundles.get(payload.bundle_name)
        combined_consumers = sorted(
            list(set((existing_entry.consumers if existing_entry else []) + consumers))
        )

        entry = BundleManifestEntry(
            package_version=payload.package_version,
            source_kind=payload.source_kind,
            bundle_digest=payload.bundle_digest,
            consumers=combined_consumers,
            skills=skills_manifest,
        )
        manifest.bundles[payload.bundle_name] = entry
        manifest.generation += 1
        manifest.transaction_id = self.tx_id
        manifest.updated_at = datetime.datetime.now(datetime.UTC).isoformat()

        save_manifest(self.skills_root, manifest)
        self.status = "MANIFEST_COMMITTED"
        self._write_journal("MANIFEST_COMMITTED", {"bundle_name": payload.bundle_name})
        self.cleanup()

    def commit_uninstall(
        self,
        manifest: InstallerManifest,
        bundle_name: str,
        remaining_consumers: list[str],
    ) -> None:
        plan = _build_uninstall_plan(
            self.skills_root, self.tx_id, manifest, bundle_name, remaining_consumers
        )
        journal = UninstallJournal(plan, "UNINSTALL_PREPARED", [])
        self._write_uninstall_journal(journal)
        journal = self._stage_uninstall(plan, journal)
        self._commit_uninstall_manifest(manifest, plan, journal)

    def _stage_uninstall(
        self, plan: UninstallPlan, journal: UninstallJournal
    ) -> UninstallJournal:
        if plan.skills:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            journal = self._move_uninstall_skills(plan, journal)
            _verify_uninstall_backups(self.backup_dir, plan)
        staged = UninstallJournal(plan, "UNINSTALL_STAGED", journal.moved_to_backup)
        self._write_uninstall_journal(staged)
        return staged

    def _move_uninstall_skills(
        self, plan: UninstallPlan, journal: UninstallJournal
    ) -> UninstallJournal:
        for skill_name in plan.skills:
            public_path = self.skills_root / skill_name
            backup_path = self.backup_dir / skill_name
            if backup_path.exists() or backup_path.is_symlink():
                raise TransactionError(
                    f"Uninstall backup target already exists: {skill_name}"
                )
            os.replace(public_path, backup_path)
            journal = UninstallJournal(
                plan, "UNINSTALL_PREPARED", [*journal.moved_to_backup, skill_name]
            )
            self._write_uninstall_journal(journal)
        return journal

    def _commit_uninstall_manifest(
        self,
        manifest: InstallerManifest,
        plan: UninstallPlan,
        journal: UninstallJournal,
    ) -> None:
        if plan.remaining_consumers:
            manifest.bundles[plan.bundle_name].consumers = list(
                plan.remaining_consumers
            )
        else:
            del manifest.bundles[plan.bundle_name]
        manifest.generation += 1
        manifest.transaction_id = plan.tx_id
        manifest.updated_at = datetime.datetime.now(datetime.UTC).isoformat()
        save_manifest(self.skills_root, manifest)
        committed = UninstallJournal(
            plan, "MANIFEST_COMMITTED", list(journal.moved_to_backup)
        )
        self._write_uninstall_journal(committed)

    def rollback(self) -> None:
        if (
            self.uninstall_journal is not None
            or self.journal_data.get("operation") == "uninstall"
        ):
            self._rollback_uninstall()
            return

        if self.status == "MANIFEST_COMMITTED":
            return

        try:
            manifest = load_manifest(self.skills_root)
            if manifest is not None and manifest.transaction_id == self.tx_id:
                self.status = "MANIFEST_COMMITTED"
                self.cleanup()
                return
        except ManifestError as e:
            raise TransactionError(
                f"Cannot safely perform live rollback due to corrupted manifest: {e}"
            ) from e
        except Exception:
            pass

        published = list(
            dict.fromkeys(
                self.journal_data.get("published_skills", [])
                + list(self.published_skills)
            )
        )
        _remove_published_skills(self.skills_root, published)

        moved = self.journal_data.get("moved_to_backup", list(self.moved_to_backup))
        success, errors = _restore_backups(self.backup_dir, self.skills_root, moved)
        if success:
            self.cleanup()
        else:
            raise TransactionError(
                f"Failed to cleanly restore backups during rollback: {errors}"
            )

    def _rollback_uninstall(self) -> None:
        if not self.journal_path.is_file():
            if self.uninstall_journal is not None:
                _remove_uninstall_journal_last(self.tx_dir)
                return
            raise TransactionError("Uninstall journal is missing during rollback")
        try:
            data = json.loads(self.journal_path.read_text(encoding="utf-8"))
            journal = UninstallJournal.from_dict(data)
        except Exception as err:
            raise TransactionError(
                f"Cannot read uninstall journal during rollback: {err}"
            ) from err
        if journal.plan.tx_id != self.tx_id:
            raise TransactionError("Uninstall journal transaction id does not match")
        recovered_bundle = _recover_uninstall_transaction(
            self.tx_dir, self.skills_root, journal
        )
        if recovered_bundle is not None:
            self.recovered_uninstalls.append(recovered_bundle)

    def cleanup(self) -> None:
        if self.tx_dir.exists():
            shutil.rmtree(self.tx_dir, ignore_errors=True)


def _build_uninstall_plan(
    skills_root: Path,
    tx_id: str,
    manifest: InstallerManifest,
    bundle_name: str,
    remaining_consumers: list[str],
) -> UninstallPlan:
    entry = manifest.bundles.get(bundle_name)
    if entry is None:
        raise TransactionError(f"Bundle '{bundle_name}' is not managed")
    if len(set(remaining_consumers)) != len(remaining_consumers) or not set(
        remaining_consumers
    ).issubset(entry.consumers):
        raise TransactionError("Invalid remaining uninstall consumers")
    if set(remaining_consumers) == set(entry.consumers):
        raise TransactionError("Uninstall must remove at least one consumer")

    _reject_shared_uninstall_skills(manifest, bundle_name, entry)
    for skill_name, skill_info in entry.skills.items():
        _validate_skill_name(skill_name)
        _validate_skill_manifest(skill_info)
        if not _skill_matches_manifest(skills_root / skill_name, skill_info):
            raise ConflictError(
                f"Managed skill '{skill_name}' has changed; refusing to uninstall"
            )

    snapshot = InstallerManifest.from_dict(
        {
            "schema_version": 1,
            "owner": "orchestune",
            "bundles": {bundle_name: asdict(entry)},
        }
    ).bundles[bundle_name]
    return UninstallPlan(
        tx_id=tx_id,
        bundle_name=bundle_name,
        manifest_generation_before=manifest.generation,
        entry_before=snapshot,
        remaining_consumers=list(remaining_consumers),
        skills=list(snapshot.skills) if not remaining_consumers else [],
    )


def _reject_shared_uninstall_skills(
    manifest: InstallerManifest, bundle_name: str, entry: BundleManifestEntry
) -> None:
    for other_name, other_entry in manifest.bundles.items():
        if other_name == bundle_name:
            continue
        shared = set(entry.skills).intersection(other_entry.skills)
        if shared:
            raise ConflictError(
                f"Skills {sorted(shared)} are also owned by another bundle '{other_name}'"
            )


def _verify_uninstall_backups(backup_dir: Path, plan: UninstallPlan) -> None:
    for skill_name in plan.skills:
        if not _skill_matches_manifest(
            backup_dir / skill_name, plan.entry_before.skills[skill_name]
        ):
            raise ConflictError(
                f"Managed skill '{skill_name}' changed while staging uninstall"
            )


def _remove_published_skills(skills_root: Path, published: list[str]) -> None:
    for s_name in published:
        p_path = skills_root / s_name
        if p_path.is_dir() and not p_path.is_symlink():
            shutil.rmtree(p_path)
        elif p_path.exists() or p_path.is_symlink():
            p_path.unlink()


def _restore_backups(
    backup_dir: Path, skills_root: Path, moved: list[str]
) -> tuple[bool, list[Exception]]:
    moved = list(moved)
    if backup_dir.is_dir():
        for b_entry in backup_dir.iterdir():
            if b_entry.name not in moved:
                moved.append(b_entry.name)
    restore_errors: list[Exception] = []
    for s_name in moved:
        b_path = backup_dir / s_name
        r_path = skills_root / s_name
        if b_path.exists():
            if r_path.is_dir() and not r_path.is_symlink():
                shutil.rmtree(r_path)
            elif r_path.exists() or r_path.is_symlink():
                r_path.unlink()
            try:
                os.replace(b_path, r_path)
            except OSError as err:
                restore_errors.append(err)
    return len(restore_errors) == 0, restore_errors


def _manifest_matches_uninstall_before(
    manifest: InstallerManifest | None, journal: UninstallJournal
) -> bool:
    if (
        manifest is None
        or manifest.generation != journal.plan.manifest_generation_before
    ):
        return False
    entry = manifest.bundles.get(journal.plan.bundle_name)
    return entry is not None and asdict(entry) == asdict(journal.plan.entry_before)


def _manifest_matches_uninstall_commit(
    manifest: InstallerManifest, journal: UninstallJournal
) -> bool:
    if (
        manifest.transaction_id != journal.plan.tx_id
        or manifest.generation != journal.plan.manifest_generation_before + 1
    ):
        return False
    entry = manifest.bundles.get(journal.plan.bundle_name)
    if not journal.plan.remaining_consumers:
        return entry is None
    if entry is None:
        return False
    expected_entry = asdict(journal.plan.entry_before)
    expected_entry["consumers"] = list(journal.plan.remaining_consumers)
    return asdict(entry) == expected_entry


def _restore_uninstall_backups(
    transaction_dir: Path, skills_root: Path, journal: UninstallJournal
) -> None:
    backup_dir = transaction_dir / "backup"
    if backup_dir.is_symlink():
        raise TransactionError("Uninstall backup directory is a symlink")
    if backup_dir.exists() and not backup_dir.is_dir():
        raise TransactionError("Uninstall backup path is not a directory")
    if backup_dir.is_dir():
        for backup_entry in backup_dir.iterdir():
            _validate_skill_name(backup_entry.name)
            if backup_entry.name not in journal.plan.skills:
                raise TransactionError(
                    f"Unknown uninstall backup entry: {backup_entry.name}"
                )

    for skill_name in journal.plan.skills:
        public_path = skills_root / skill_name
        backup_path = backup_dir / skill_name
        public_exists = public_path.exists() or public_path.is_symlink()
        backup_exists = backup_path.exists() or backup_path.is_symlink()
        if public_exists and backup_exists:
            raise TransactionError(
                f"Both the public path and backup exist for uninstall skill '{skill_name}'"
            )
        if backup_exists:
            os.replace(backup_path, public_path)
        elif not public_exists:
            raise TransactionError(
                f"Uninstall skill '{skill_name}' is missing from both public path and backup"
            )


def _remove_uninstall_backup(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def _cleanup_committed_uninstall(
    transaction_dir: Path, journal: UninstallJournal
) -> None:
    backup_dir = transaction_dir / "backup"
    try:
        if backup_dir.is_symlink():
            raise TransactionError("Uninstall backup directory is a symlink")
        if backup_dir.exists() and not backup_dir.is_dir():
            raise TransactionError("Uninstall backup path is not a directory")
        if backup_dir.is_dir():
            for backup_entry in backup_dir.iterdir():
                _validate_skill_name(backup_entry.name)
                if backup_entry.name not in journal.plan.skills:
                    raise TransactionError(
                        f"Unknown uninstall backup entry: {backup_entry.name}"
                    )
            for skill_name in journal.plan.skills:
                backup_path = backup_dir / skill_name
                if backup_path.exists() or backup_path.is_symlink():
                    skill_info = journal.plan.entry_before.skills[skill_name]
                    if not _skill_matches_manifest(
                        backup_path, skill_info, allow_missing=True
                    ):
                        raise TransactionError(
                            f"Committed uninstall backup changed: {skill_name}"
                        )
                    _remove_uninstall_backup(backup_path)
        _remove_uninstall_journal_last(transaction_dir)
    except TransactionError:
        raise
    except OSError as err:
        raise TransactionError(
            f"Could not finish committed uninstall cleanup in {transaction_dir}: {err}"
        ) from err


def _remove_uninstall_journal_last(transaction_dir: Path) -> None:
    if not transaction_dir.exists():
        return
    if transaction_dir.is_symlink() or not transaction_dir.is_dir():
        raise TransactionError("Uninstall transaction path is not a directory")
    journal_path = transaction_dir / "journal.json"
    for entry in transaction_dir.iterdir():
        if entry.name == "journal.json":
            if entry.is_symlink() or not entry.is_file():
                raise TransactionError("Uninstall journal is not a regular file")
            continue
        if entry.name == "backup":
            if entry.is_symlink() or not entry.is_dir() or any(entry.iterdir()):
                raise TransactionError("Uninstall backup directory is not empty")
            entry.rmdir()
            continue
        if entry.name.startswith(".journal-") and entry.name.endswith(".tmp"):
            if entry.is_symlink() or not entry.is_file():
                raise TransactionError("Uninstall journal temporary path is invalid")
            entry.unlink()
            continue
        raise TransactionError(f"Unknown uninstall transaction entry: {entry.name}")
    if journal_path.exists() or journal_path.is_symlink():
        journal_path.unlink()
    try:
        transaction_dir.rmdir()
    except OSError:
        # An empty transaction directory has no recovery data and is harmless.
        pass


def _recover_uninstall_transaction(
    transaction_dir: Path, skills_root: Path, journal: UninstallJournal
) -> str | None:
    if journal.plan.tx_id != transaction_dir.name:
        raise TransactionError(
            "Uninstall journal transaction id does not match directory"
        )
    try:
        manifest = load_manifest(skills_root)
    except ManifestError as err:
        raise TransactionError(
            f"Cannot recover uninstall with a corrupted manifest: {err}"
        ) from err

    if manifest is not None and manifest.transaction_id == journal.plan.tx_id:
        if not _manifest_matches_uninstall_commit(manifest, journal):
            raise TransactionError(
                "Committed uninstall manifest does not match its journal"
            )
        _cleanup_committed_uninstall(transaction_dir, journal)
        return journal.plan.bundle_name

    if journal.status == "MANIFEST_COMMITTED":
        raise TransactionError("Uninstall journal says committed but manifest does not")
    if not _manifest_matches_uninstall_before(manifest, journal):
        raise TransactionError("Uninstall manifest changed before commit")
    _restore_uninstall_backups(transaction_dir, skills_root, journal)
    _remove_uninstall_journal_last(transaction_dir)
    return None


def _rollback_sub_transaction(
    sub: Path, skills_root: Path, journal_data: dict[str, Any]
) -> None:
    status = journal_data.get("status")
    tx_id = journal_data.get("tx_id")
    backup_dir = sub / "backup"

    if status == "MANIFEST_COMMITTED":
        shutil.rmtree(sub, ignore_errors=True)
        return

    if tx_id and status not in ("INITIALIZED", "PREPARED"):
        try:
            manifest = load_manifest(skills_root)
            if manifest is not None and manifest.transaction_id == tx_id:
                shutil.rmtree(sub, ignore_errors=True)
                return
        except ManifestError as e:
            raise TransactionError(
                f"Cannot safely recover transaction {sub.name} due to corrupted manifest: {e}"
            ) from e
        except Exception:
            pass

    published = journal_data.get("published_skills", [])
    _remove_published_skills(skills_root, published)

    moved = journal_data.get("moved_to_backup", [])
    success, errors = _restore_backups(backup_dir, skills_root, moved)
    if success:
        shutil.rmtree(sub, ignore_errors=True)
    else:
        raise TransactionError(f"Could not cleanly restore backups in {sub}: {errors}")


def recover_pending_transactions(
    skills_root: Path, skip_tx_id: str | None = None
) -> list[str]:
    tx_root = get_installer_dir(skills_root) / TRANSACTIONS_DIR
    if not tx_root.is_dir():
        return []

    completed_uninstalls: list[str] = []
    for sub in list(tx_root.iterdir()):
        if not sub.is_dir():
            continue
        if skip_tx_id is not None and sub.name == skip_tx_id:
            continue
        journal_file = sub / "journal.json"
        if not journal_file.is_file():
            shutil.rmtree(sub, ignore_errors=True)
            continue

        try:
            journal_data = json.loads(journal_file.read_text(encoding="utf-8"))
            if not isinstance(journal_data, dict):
                raise TransactionError("Transaction journal must be an object")
            if "operation" not in journal_data:
                _rollback_sub_transaction(sub, skills_root, journal_data)
            elif journal_data["operation"] == "uninstall":
                journal = UninstallJournal.from_dict(journal_data)
                completed_bundle = _recover_uninstall_transaction(
                    sub, skills_root, journal
                )
                if completed_bundle is not None:
                    completed_uninstalls.append(completed_bundle)
            else:
                raise TransactionError(
                    f"Unknown transaction operation: {journal_data['operation']!r}"
                )
        except Exception as e:
            raise TransactionError(
                f"Failed to recover transaction at {sub}: {e}"
            ) from e
    return completed_uninstalls
