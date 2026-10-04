from pathlib import Path

import pytest

from orchestune.installer.contracts import (
    BundlePayload,
    FileRecord,
    SkillPayload,
    TransactionError,
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


def _make_two_skills_payload(skills_dir: Path) -> BundlePayload:
    src_dir = skills_dir / "src_two_skills"
    src_dir.mkdir(parents=True, exist_ok=True)
    for s_name in ("skill_a", "skill_b"):
        d = src_dir / s_name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"new {s_name}", encoding="utf-8")

    from orchestune.installer.payload import calculate_file_sha256

    return BundlePayload(
        bundle_name="standard",
        package_version="0.5.0",
        source_kind="installed_distribution",
        bundle_digest="two_digest",
        skills={
            s_name: SkillPayload(
                name=s_name,
                files={
                    "SKILL.md": FileRecord(
                        relative_path="SKILL.md",
                        sha256=calculate_file_sha256(src_dir / s_name / "SKILL.md"),
                        mode="regular",
                    )
                },
                directories=[],
            )
            for s_name in ("skill_a", "skill_b")
        },
        source_path=src_dir,
    )


def test_transaction_rollback_partial_backup_failure(tmp_path: Path, monkeypatch):
    import os

    root = tmp_path / "skills"
    root.mkdir()
    for s_name in ("skill_a", "skill_b"):
        d = root / s_name
        d.mkdir()
        (d / "SKILL.md").write_text(f"old {s_name}", encoding="utf-8")

    payload = _make_two_skills_payload(tmp_path)
    tx = SkillTransaction(root)

    # Monkeypatch os.replace to fail when moving skill_b to backup
    orig_replace = os.replace
    call_count = 0

    def faulty_replace(src, dst):
        nonlocal call_count
        call_count += 1
        # First call moves skill_a to backup, second call raises error
        if "skill_b" in str(src) and "backup" in str(dst):
            raise OSError("Injected disk failure during backup")
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", faulty_replace)

    with pytest.raises(OSError, match="Injected disk failure during backup"):
        with tx:
            tx.stage_publish(payload)

    # After rollback, both skill_a and skill_b must still be intact in root
    assert (root / "skill_a" / "SKILL.md").read_text(encoding="utf-8") == "old skill_a"
    assert (root / "skill_b" / "SKILL.md").read_text(encoding="utf-8") == "old skill_b"


def test_transaction_rollback_partial_publish_failure(tmp_path: Path, monkeypatch):
    import os

    root = tmp_path / "skills"
    root.mkdir()
    for s_name in ("skill_a", "skill_b"):
        d = root / s_name
        d.mkdir()
        (d / "SKILL.md").write_text(f"old {s_name}", encoding="utf-8")

    payload = _make_two_skills_payload(tmp_path)
    tx = SkillTransaction(root)

    orig_replace = os.replace

    def faulty_replace(src, dst):
        # Allow moves to backup, but fail when publishing skill_b to root
        if "skill_b" in str(src) and "stage" in str(src):
            raise OSError("Injected disk failure during publish")
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", faulty_replace)

    with pytest.raises(OSError, match="Injected disk failure during publish"):
        with tx:
            tx.stage_publish(payload)

    # After rollback, both original skills must still be restored intact
    assert (root / "skill_a" / "SKILL.md").read_text(encoding="utf-8") == "old skill_a"
    assert (root / "skill_b" / "SKILL.md").read_text(encoding="utf-8") == "old skill_b"


def test_transaction_recovery_partial_backup_in_prepared_state(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    # Old skill_b is in root, old skill_a already moved to backup before crash
    (root / "skill_b").mkdir()
    (root / "skill_b" / "SKILL.md").write_text("old skill_b", encoding="utf-8")

    tx = SkillTransaction(root)
    tx.tx_dir.mkdir(parents=True)
    tx.backup_dir.mkdir(parents=True)
    (tx.backup_dir / "skill_a").mkdir()
    (tx.backup_dir / "skill_a" / "SKILL.md").write_text("old skill_a", encoding="utf-8")

    # Crash happened when journal was still PREPARED or MOVING_BACKUP
    tx._write_journal(
        "PREPARED", {"bundle_name": "standard", "skills": ["skill_a", "skill_b"]}
    )

    recover_pending_transactions(root)

    # Both skills must be present in root
    assert (root / "skill_a" / "SKILL.md").read_text(encoding="utf-8") == "old skill_a"
    assert (root / "skill_b" / "SKILL.md").read_text(encoding="utf-8") == "old skill_b"
    assert not tx.tx_dir.exists()


def test_transaction_enter_releases_lock_on_recovery_error(tmp_path: Path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()

    def failing_recover(*args, **kwargs):
        raise RuntimeError("Simulated recovery failure")

    monkeypatch.setattr(
        "orchestune.installer.transaction.recover_pending_transactions",
        failing_recover,
    )

    tx1 = SkillTransaction(root)
    with pytest.raises(RuntimeError, match="Simulated recovery failure"):
        with tx1:
            pass

    # Lock must be released, allowing subsequent acquisition immediately
    monkeypatch.undo()
    tx2 = SkillTransaction(root)
    with tx2:
        assert tx2.lock is not None


def test_recovery_respects_already_committed_manifest(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()
    (root / "orchestune").mkdir()
    (root / "orchestune" / "SKILL.md").write_text("new skill", encoding="utf-8")

    tx = SkillTransaction(root)
    committed_tx_id = tx.tx_id

    manifest = InstallerManifest(
        transaction_id=committed_tx_id,
        bundles={
            "standard": BundleManifestEntry(
                package_version="0.5.0",
                source_kind="installed_distribution",
                bundle_digest="digest_new",
                consumers=["codex"],
                skills={
                    "orchestune": {
                        "files": {"SKILL.md": {"sha256": "new_sha", "mode": "regular"}},
                        "directories": [],
                    }
                },
            )
        },
    )
    save_manifest(root, manifest)

    # Crash occurred after save_manifest, before MANIFEST_COMMITTED
    tx.tx_dir.mkdir(parents=True)
    tx.backup_dir.mkdir(parents=True)
    (tx.backup_dir / "orchestune").mkdir()
    (tx.backup_dir / "orchestune" / "SKILL.md").write_text(
        "old skill", encoding="utf-8"
    )

    tx._write_journal(
        "NEW_PUBLISHED",
        {
            "bundle_name": "standard",
            "skills": ["orchestune"],
            "published_skills": ["orchestune"],
            "moved_to_backup": ["orchestune"],
        },
    )

    recover_pending_transactions(root)

    # Manifest matches tx_id, so new skill is preserved and tx_dir is cleaned up
    assert (root / "orchestune" / "SKILL.md").read_text(encoding="utf-8") == "new skill"
    assert not tx.tx_dir.exists()


def test_rollback_failure_propagates_underlying_error(tmp_path: Path, monkeypatch):
    import os

    root = tmp_path / "skills"
    root.mkdir()
    existing_skill = root / "orchestune"
    existing_skill.mkdir()
    (existing_skill / "SKILL.md").write_text("old skill", encoding="utf-8")

    payload = _make_dummy_payload(tmp_path)
    tx = SkillTransaction(root)

    orig_replace = os.replace

    def faulty_replace(src, dst):
        if "backup" in str(src):
            raise OSError("Disk corruption during restore")
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", faulty_replace)

    with pytest.raises(TransactionError, match="Disk corruption during restore"):
        with tx:
            tx.stage_publish(payload)
            raise RuntimeError("Trigger rollback")


def test_commit_journal_failure_does_not_rollback(tmp_path: Path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    existing_skill = root / "orchestune"
    existing_skill.mkdir()
    (existing_skill / "SKILL.md").write_text("old skill", encoding="utf-8")

    payload = _make_dummy_payload(tmp_path, version="0.5.0")
    tx = SkillTransaction(root)

    orig_write_journal = tx._write_journal

    def faulty_write_journal(status, details):
        if status == "MANIFEST_COMMITTED":
            raise OSError("Disk full during commit journal write")
        return orig_write_journal(status, details)

    monkeypatch.setattr(tx, "_write_journal", faulty_write_journal)

    with pytest.raises(OSError, match="Disk full during commit journal write"):
        with tx:
            manifest = load_manifest(root) or InstallerManifest()
            tx.commit(payload, manifest, consumers=["codex"])

    assert (root / "orchestune" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "dummy skill"
    updated_manifest = load_manifest(root)
    assert updated_manifest is not None
    assert updated_manifest.transaction_id == tx.tx_id


def test_recovery_with_corrupted_manifest_raises_transaction_error(
    tmp_path: Path,
):
    root = tmp_path / "skills"
    root.mkdir()
    (root / "orchestune").mkdir()
    (root / "orchestune" / "SKILL.md").write_text("current skill", encoding="utf-8")

    tx = SkillTransaction(root)
    tx.tx_dir.mkdir(parents=True)
    tx.backup_dir.mkdir(parents=True)
    (tx.backup_dir / "orchestune").mkdir()
    (tx.backup_dir / "orchestune" / "SKILL.md").write_text(
        "backup skill", encoding="utf-8"
    )

    tx._write_journal(
        "NEW_PUBLISHED",
        {
            "bundle_name": "standard",
            "skills": ["orchestune"],
            "published_skills": ["orchestune"],
            "moved_to_backup": ["orchestune"],
        },
    )

    manifest_file = root / ".orchestune-installer" / "manifest.json"
    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    manifest_file.write_text("{invalid json", encoding="utf-8")

    with pytest.raises(TransactionError, match="corrupted manifest"):
        recover_pending_transactions(root)

    assert (root / "orchestune" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "current skill"
