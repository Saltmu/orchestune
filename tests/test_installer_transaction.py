from pathlib import Path

import pytest

from orchestune.installer.contracts import (
    BundlePayload,
    FileRecord,
    SkillPayload,
)
from orchestune.installer.state import (
    BundleManifestEntry,
    InstallerManifest,
    load_manifest,
    save_manifest,
)
from orchestune.installer.transaction import (
    SkillTransaction,
    recover_pending_transactions,
)


def _make_dummy_payload(
    skills_dir: Path, version="0.5.0", digest="digest1"
) -> BundlePayload:
    # Create actual source files on disk
    src_dir = skills_dir / "src_skills"
    src_dir.mkdir(parents=True, exist_ok=True)
    o_dir = src_dir / "orchestune"
    o_dir.mkdir(parents=True, exist_ok=True)
    skill_file = o_dir / "SKILL.md"
    skill_file.write_text("dummy skill", encoding="utf-8")
    from orchestune.installer.payload import calculate_file_sha256

    real_sha = calculate_file_sha256(skill_file)

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
                        relative_path="SKILL.md", sha256=real_sha, mode="regular"
                    )
                },
                directories=[],
            )
        },
        source_path=src_dir,
    )


def test_transaction_lifecycle_success(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()
    payload = _make_dummy_payload(tmp_path)

    tx = SkillTransaction(root)
    with tx:
        tx.prepare_stage(payload)
        assert (tx.stage_dir / "orchestune" / "SKILL.md").is_file()

        # Perform install / publish
        manifest = load_manifest(root) or InstallerManifest()
        tx.commit(payload, manifest, consumers=["codex"])

    # Verify skill is published to root
    assert (root / "orchestune" / "SKILL.md").is_file()
    # Verify transaction directory is cleaned up
    assert not tx.tx_dir.exists()

    # Verify manifest is updated
    updated_manifest = load_manifest(root)
    assert updated_manifest is not None
    assert "standard" in updated_manifest.bundles
    assert updated_manifest.bundles["standard"].consumers == ["codex"]


def test_transaction_rollback_on_failure(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    # Pre-existing skill
    existing_skill = root / "orchestune"
    existing_skill.mkdir()
    (existing_skill / "SKILL.md").write_text("old skill", encoding="utf-8")

    initial_manifest = InstallerManifest(
        bundles={
            "standard": BundleManifestEntry(
                package_version="0.4.0",
                source_kind="installed_distribution",
                bundle_digest="old_digest",
                consumers=["codex"],
                skills={
                    "orchestune": {
                        "files": {"SKILL.md": {"sha256": "old_sha", "mode": "regular"}},
                        "directories": [],
                    }
                },
            )
        }
    )
    save_manifest(root, initial_manifest)

    payload = _make_dummy_payload(tmp_path, version="0.5.0", digest="new_digest")

    tx = SkillTransaction(root)
    with pytest.raises(RuntimeError, match="Simulated crash"):
        with tx:
            tx.prepare_stage(payload)
            tx.stage_publish(payload)
            # Simulate failure before manifest commit
            raise RuntimeError("Simulated crash")

    # Verify old skill is restored on rollback
    assert (root / "orchestune" / "SKILL.md").read_text(encoding="utf-8") == "old skill"


def test_transaction_recovery_after_crash(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    existing_skill = root / "orchestune"
    existing_skill.mkdir()
    (existing_skill / "SKILL.md").write_text("old skill", encoding="utf-8")

    tx = SkillTransaction(root)
    # Manually simulate a crashed transaction state
    tx.tx_dir.mkdir(parents=True)
    tx.backup_dir.mkdir(parents=True)
    # Move old skill to backup
    (tx.backup_dir / "orchestune").mkdir()
    (tx.backup_dir / "orchestune" / "SKILL.md").write_text(
        "old skill", encoding="utf-8"
    )
    existing_skill.rmdir() if not any(existing_skill.iterdir()) else None

    # Write journal indicating OLD_MOVED
    tx._write_journal(
        "OLD_MOVED", {"bundle_name": "standard", "skills": ["orchestune"]}
    )

    # Run recovery
    recover_pending_transactions(root)

    # Old skill should be restored
    assert (root / "orchestune" / "SKILL.md").read_text(encoding="utf-8") == "old skill"
    assert not tx.tx_dir.exists()
