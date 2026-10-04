from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from orchestune.infra.process_utils import FileLock
from orchestune.installer.contracts import BundlePayload, TransactionError
from orchestune.installer.payload import calculate_file_sha256
from orchestune.installer.state import (
    LOCK_FILENAME,
    TRANSACTIONS_DIR,
    BundleManifestEntry,
    InstallerManifest,
    get_installer_dir,
    save_manifest,
)


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

    def __enter__(self) -> SkillTransaction:
        self.installer_dir.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(self.lock_path, timeout=10.0)
        self.lock.acquire()
        recover_pending_transactions(self.skills_root, skip_tx_id=self.tx_id)
        self.tx_dir.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            if exc_type is not None:
                self.rollback()
            elif self.status == "MANIFEST_COMMITTED":
                self.cleanup()
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

        for skill_name in payload.skills.keys():
            stage_src = self.stage_dir / skill_name
            publish_dest = self.skills_root / skill_name
            os.replace(stage_src, publish_dest)
            self.published_skills.append(skill_name)
            self._write_journal(
                "PUBLISHING",
                {
                    "bundle_name": payload.bundle_name,
                    "skills": list(payload.skills.keys()),
                    "moved_to_backup": list(self.moved_to_backup),
                    "published_skills": list(self.published_skills),
                },
            )

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
        self._write_journal("MANIFEST_COMMITTED", {"bundle_name": payload.bundle_name})
        self.cleanup()

    def rollback(self) -> None:
        if self.status == "MANIFEST_COMMITTED":
            return

        published = self.journal_data.get(
            "published_skills", list(self.published_skills)
        )
        _remove_published_skills(self.skills_root, published)

        moved = self.journal_data.get("moved_to_backup", list(self.moved_to_backup))
        if _restore_backups(self.backup_dir, self.skills_root, moved):
            self.cleanup()

    def cleanup(self) -> None:
        if self.tx_dir.exists():
            shutil.rmtree(self.tx_dir, ignore_errors=True)


def _remove_published_skills(skills_root: Path, published: list[str]) -> None:
    for s_name in published:
        p_path = skills_root / s_name
        if p_path.is_dir() and not p_path.is_symlink():
            shutil.rmtree(p_path)
        elif p_path.exists() or p_path.is_symlink():
            p_path.unlink()


def _restore_backups(backup_dir: Path, skills_root: Path, moved: list[str]) -> bool:
    if backup_dir.is_dir():
        for b_entry in backup_dir.iterdir():
            if b_entry.name not in moved:
                moved.append(b_entry.name)
    restore_failed = False
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
            except Exception:
                restore_failed = True
    return not restore_failed


def _rollback_sub_transaction(
    sub: Path, skills_root: Path, journal_data: dict[str, Any]
) -> None:
    status = journal_data.get("status")
    backup_dir = sub / "backup"

    if status == "MANIFEST_COMMITTED":
        shutil.rmtree(sub, ignore_errors=True)
        return

    published = journal_data.get("published_skills", [])
    _remove_published_skills(skills_root, published)

    moved = journal_data.get("moved_to_backup", [])
    if _restore_backups(backup_dir, skills_root, moved):
        shutil.rmtree(sub, ignore_errors=True)
    else:
        raise TransactionError(f"Could not cleanly restore backups in {sub}")


def recover_pending_transactions(
    skills_root: Path, skip_tx_id: str | None = None
) -> None:
    tx_root = get_installer_dir(skills_root) / TRANSACTIONS_DIR
    if not tx_root.is_dir():
        return

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
            _rollback_sub_transaction(sub, skills_root, journal_data)
        except Exception as e:
            raise TransactionError(
                f"Failed to recover transaction at {sub}: {e}"
            ) from e
