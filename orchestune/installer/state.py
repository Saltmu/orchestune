from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from orchestune.installer.contracts import BundlePayload, BundleState, ManifestError
from orchestune.installer.payload import calculate_file_sha256

INSTALLER_DIR = ".orchestune-installer"
MANIFEST_FILENAME = "manifest.json"
LOCK_FILENAME = "write.lock"
TRANSACTIONS_DIR = "transactions"


@dataclass
class BundleManifestEntry:
    package_version: str
    source_kind: str
    bundle_digest: str
    consumers: list[str]
    skills: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class InstallerManifest:
    schema_version: int = 1
    owner: str = "orchestune"
    generation: int = 1
    updated_at: str = ""
    transaction_id: str = ""
    bundles: dict[str, BundleManifestEntry] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstallerManifest:
        if data.get("owner") != "orchestune":
            raise ManifestError(f"Invalid manifest owner: {data.get('owner')}")
        if data.get("schema_version") != 1:
            raise ManifestError(
                f"Unsupported schema version: {data.get('schema_version')}"
            )

        bundles: dict[str, BundleManifestEntry] = {}
        for b_name, b_data in data.get("bundles", {}).items():
            skills = b_data.get("skills", {})
            for s_name in skills.keys():
                if ".." in s_name or s_name.startswith("/"):
                    raise ManifestError(f"Invalid skill path in manifest: {s_name}")
            bundles[b_name] = BundleManifestEntry(
                package_version=b_data["package_version"],
                source_kind=b_data["source_kind"],
                bundle_digest=b_data["bundle_digest"],
                consumers=b_data["consumers"],
                skills=skills,
            )

        return cls(
            schema_version=data["schema_version"],
            owner=data["owner"],
            generation=data.get("generation", 1),
            updated_at=data.get("updated_at", ""),
            transaction_id=data.get("transaction_id", ""),
            bundles=bundles,
        )


def get_installer_dir(skills_root: Path) -> Path:
    return skills_root / INSTALLER_DIR


def get_manifest_path(skills_root: Path) -> Path:
    return get_installer_dir(skills_root) / MANIFEST_FILENAME


def load_manifest(skills_root: Path) -> InstallerManifest | None:
    manifest_path = get_manifest_path(skills_root)
    if not manifest_path.is_file():
        return None

    try:
        content = manifest_path.read_text(encoding="utf-8")
        data = json.loads(content)
        return InstallerManifest.from_dict(data)
    except ManifestError:
        raise
    except Exception as e:
        raise ManifestError(f"Corrupted manifest at {manifest_path}: {e}") from e


def save_manifest(skills_root: Path, manifest: InstallerManifest) -> None:
    installer_dir = get_installer_dir(skills_root)
    installer_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = get_manifest_path(skills_root)

    data = manifest.to_dict()
    content = json.dumps(data, indent=2, sort_keys=True)

    # Atomic write
    with tempfile.NamedTemporaryFile(
        "w", dir=installer_dir, delete=False, encoding="utf-8"
    ) as tf:
        tf.write(content)
        tf.flush()
        os.fsync(tf.fileno())
        temp_name = tf.name

    os.replace(temp_name, manifest_path)


def _inspect_unmanifested_state(
    skills_root: Path, target_skill_names: list[str]
) -> BundleState:
    for name in target_skill_names:
        skill_path = skills_root / name
        if skill_path.is_symlink():
            if not skill_path.exists():
                return BundleState.BROKEN_LINK
            return BundleState.LEGACY_LINK
        if skill_path.exists():
            return BundleState.UNMANAGED
    return BundleState.ABSENT


def _skill_dir_matches_manifest(skill_dir: Path, skill_info: dict) -> bool:
    if not skill_dir.is_dir() or skill_dir.is_symlink():
        return False

    recorded_files = skill_info.get("files", {})
    for rel_file, f_meta in recorded_files.items():
        f_path = skill_dir / rel_file
        if not f_path.is_file():
            return False
        real_sha = calculate_file_sha256(f_path)
        if real_sha != f_meta["sha256"]:
            return False

    for root, _, filenames in os.walk(skill_dir):
        for fn in filenames:
            rel = (Path(root) / fn).relative_to(skill_dir).as_posix()
            if rel not in recorded_files:
                return False

    return True


def inspect_bundle_state(skills_root: Path, payload: BundlePayload) -> BundleState:
    installer_dir = get_installer_dir(skills_root)
    tx_dir = installer_dir / TRANSACTIONS_DIR
    if tx_dir.is_dir():
        for sub in tx_dir.iterdir():
            if sub.is_dir() and (sub / "journal.json").is_file():
                return BundleState.RECOVERY_REQUIRED

    try:
        manifest = load_manifest(skills_root)
    except ManifestError:
        return BundleState.STATE_INVALID

    target_skill_names = list(payload.skills.keys())

    if manifest is None or payload.bundle_name not in manifest.bundles:
        return _inspect_unmanifested_state(skills_root, target_skill_names)

    entry = manifest.bundles[payload.bundle_name]

    for skill_name, skill_info in entry.skills.items():
        skill_dir = skills_root / skill_name
        if not _skill_dir_matches_manifest(skill_dir, skill_info):
            return BundleState.MODIFIED

    if entry.bundle_digest == payload.bundle_digest:
        return BundleState.MANAGED_CURRENT
    return BundleState.MANAGED_OUTDATED
