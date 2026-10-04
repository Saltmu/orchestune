import json
from pathlib import Path

import pytest

from orchestune.installer.contracts import (
    BundlePayload,
    BundleState,
    FileRecord,
    ManifestError,
    SkillPayload,
)
from orchestune.installer.state import (
    INSTALLER_DIR,
    MANIFEST_FILENAME,
    BundleManifestEntry,
    InstallerManifest,
    inspect_bundle_state,
    load_manifest,
    save_manifest,
)


def _make_dummy_payload(version="0.5.0", digest="abc123digest") -> BundlePayload:
    return BundlePayload(
        bundle_name="standard",
        package_version=version,
        source_kind="installed_distribution",
        bundle_digest=digest,
        skills={
            "orchestune": SkillPayload(
                name="orchestune",
                files={
                    "SKILL.md": FileRecord(
                        relative_path="SKILL.md", sha256="sha_skill_md", mode="regular"
                    )
                },
                directories=[],
            )
        },
        source_path=Path("/dummy"),
    )


def test_manifest_serialization_and_deserialization(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    manifest = InstallerManifest(
        schema_version=1,
        owner="orchestune",
        generation=1,
        updated_at="2026-10-04T00:00:00Z",
        transaction_id="tx-123",
        bundles={
            "standard": BundleManifestEntry(
                package_version="0.5.0",
                source_kind="installed_distribution",
                bundle_digest="digest_123",
                consumers=["codex"],
                skills={
                    "orchestune": {
                        "files": {"SKILL.md": {"sha256": "sha_123", "mode": "regular"}},
                        "directories": ["references"],
                    }
                },
            )
        },
    )

    save_manifest(root, manifest)

    manifest_file = root / INSTALLER_DIR / MANIFEST_FILENAME
    assert manifest_file.is_file()

    loaded = load_manifest(root)
    assert loaded is not None
    assert loaded.schema_version == 1
    assert loaded.owner == "orchestune"
    assert loaded.generation == 1
    assert loaded.transaction_id == "tx-123"
    assert "standard" in loaded.bundles
    entry = loaded.bundles["standard"]
    assert entry.package_version == "0.5.0"
    assert entry.consumers == ["codex"]
    assert "orchestune" in entry.skills


def test_manifest_rejects_path_traversal(tmp_path: Path):
    root = tmp_path / "skills"
    installer_dir = root / INSTALLER_DIR
    installer_dir.mkdir(parents=True)
    manifest_file = installer_dir / MANIFEST_FILENAME

    bad_data = {
        "schema_version": 1,
        "owner": "orchestune",
        "generation": 1,
        "updated_at": "2026-10-04T00:00:00Z",
        "transaction_id": "tx-123",
        "bundles": {
            "standard": {
                "package_version": "0.5.0",
                "source_kind": "installed_distribution",
                "bundle_digest": "digest_123",
                "consumers": ["codex"],
                "skills": {
                    "../malicious": {
                        "files": {"SKILL.md": {"sha256": "sha_123", "mode": "regular"}},
                        "directories": [],
                    }
                },
            }
        },
    }
    manifest_file.write_text(json.dumps(bad_data), encoding="utf-8")

    with pytest.raises(ManifestError, match="Invalid skill path"):
        load_manifest(root)


def test_inspect_state_absent(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()
    payload = _make_dummy_payload()
    state = inspect_bundle_state(root, payload)
    assert state == BundleState.ABSENT


def test_inspect_state_managed_current(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    # Create real skill files matching payload
    skill_dir = root / "orchestune"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("hello", encoding="utf-8")
    real_sha = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"

    matching_payload = BundlePayload(
        bundle_name="standard",
        package_version="0.5.0",
        source_kind="installed_distribution",
        bundle_digest="digest_matching",
        skills={
            "orchestune": SkillPayload(
                name="orchestune",
                files={
                    "SKILL.md": FileRecord(
                        relative_path="SKILL.md", sha256=real_sha, mode="regular"
                    )
                },
                directories=[],
            )
        },
        source_path=Path("/dummy"),
    )

    manifest = InstallerManifest(
        schema_version=1,
        owner="orchestune",
        generation=1,
        updated_at="2026-10-04T00:00:00Z",
        transaction_id="tx-1",
        bundles={
            "standard": BundleManifestEntry(
                package_version="0.5.0",
                source_kind="installed_distribution",
                bundle_digest="digest_matching",
                consumers=["codex"],
                skills={
                    "orchestune": {
                        "files": {"SKILL.md": {"sha256": real_sha, "mode": "regular"}},
                        "directories": [],
                    }
                },
            )
        },
    )
    save_manifest(root, manifest)

    state = inspect_bundle_state(root, matching_payload)
    assert state == BundleState.MANAGED_CURRENT


def test_inspect_state_managed_outdated(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    skill_dir = root / "orchestune"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("hello", encoding="utf-8")
    real_sha = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"

    manifest = InstallerManifest(
        schema_version=1,
        owner="orchestune",
        generation=1,
        updated_at="2026-10-04T00:00:00Z",
        transaction_id="tx-1",
        bundles={
            "standard": BundleManifestEntry(
                package_version="0.4.0",
                source_kind="installed_distribution",
                bundle_digest="old_digest",
                consumers=["codex"],
                skills={
                    "orchestune": {
                        "files": {"SKILL.md": {"sha256": real_sha, "mode": "regular"}},
                        "directories": [],
                    }
                },
            )
        },
    )
    save_manifest(root, manifest)

    new_payload = BundlePayload(
        bundle_name="standard",
        package_version="0.5.0",
        source_kind="installed_distribution",
        bundle_digest="new_digest",
        skills={
            "orchestune": SkillPayload(
                name="orchestune",
                files={
                    "SKILL.md": FileRecord(
                        relative_path="SKILL.md", sha256="new_sha", mode="regular"
                    )
                },
                directories=[],
            )
        },
        source_path=Path("/dummy"),
    )

    state = inspect_bundle_state(root, new_payload)
    assert state == BundleState.MANAGED_OUTDATED


def test_inspect_state_modified_by_user(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    skill_dir = root / "orchestune"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("edited by user", encoding="utf-8")

    manifest = InstallerManifest(
        schema_version=1,
        owner="orchestune",
        generation=1,
        updated_at="2026-10-04T00:00:00Z",
        transaction_id="tx-1",
        bundles={
            "standard": BundleManifestEntry(
                package_version="0.5.0",
                source_kind="installed_distribution",
                bundle_digest="digest_matching",
                consumers=["codex"],
                skills={
                    "orchestune": {
                        "files": {
                            "SKILL.md": {"sha256": "original_sha", "mode": "regular"}
                        },
                        "directories": [],
                    }
                },
            )
        },
    )
    save_manifest(root, manifest)

    payload = _make_dummy_payload(version="0.5.0", digest="digest_matching")
    state = inspect_bundle_state(root, payload)
    assert state == BundleState.MODIFIED


def test_inspect_state_unmanaged(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    skill_dir = root / "orchestune"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("unmanaged skill", encoding="utf-8")

    payload = _make_dummy_payload()
    state = inspect_bundle_state(root, payload)
    assert state == BundleState.UNMANAGED
