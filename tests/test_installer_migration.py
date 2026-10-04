from pathlib import Path

import pytest

from orchestune.installer.contracts import (
    BundlePayload,
    ConflictError,
    FileRecord,
    SkillPayload,
)
from orchestune.installer.migration import detect_legacy_skills, migrate_legacy_skills
from orchestune.installer.state import load_manifest


def _setup_skill_source(tmp_path: Path) -> tuple[Path, BundlePayload]:
    src_dir = tmp_path / "source" / "skills"
    src_dir.mkdir(parents=True)
    o_dir = src_dir / "orchestune"
    o_dir.mkdir()
    skill_file = o_dir / "SKILL.md"
    skill_file.write_text("valid content\n", encoding="utf-8")

    from orchestune.installer.payload import calculate_file_sha256

    real_sha = calculate_file_sha256(skill_file)

    payload = BundlePayload(
        bundle_name="standard",
        package_version="0.5.0",
        source_kind="source_directory",
        bundle_digest="mig_digest",
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
    return src_dir, payload


def test_migrate_valid_legacy_symlink(tmp_path: Path):
    src_dir, payload = _setup_skill_source(tmp_path)

    # Setup target root with a legacy symlink pointing to valid source
    target_root = tmp_path / "target" / ".agents" / "skills"
    target_root.mkdir(parents=True)
    legacy_link = target_root / "orchestune"
    legacy_link.symlink_to(src_dir / "orchestune")

    candidates = detect_legacy_skills(target_root, payload)
    assert len(candidates) == 1
    assert candidates[0].skill_name == "orchestune"
    assert candidates[0].is_symlink is True

    # Migrate with migrate_legacy=True
    migrate_legacy_skills(
        target_root, payload, consumers=["codex"], migrate_legacy=True
    )

    # Target is now a real directory (not a symlink)
    assert (target_root / "orchestune").is_dir()
    assert not (target_root / "orchestune").is_symlink()
    assert (target_root / "orchestune" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "valid content\n"

    # Manifest committed
    manifest = load_manifest(target_root)
    assert manifest is not None
    assert "standard" in manifest.bundles


def test_migrate_without_flag_raises_conflict(tmp_path: Path):
    src_dir, payload = _setup_skill_source(tmp_path)
    target_root = tmp_path / "target" / ".agents" / "skills"
    target_root.mkdir(parents=True)
    legacy_link = target_root / "orchestune"
    legacy_link.symlink_to(src_dir / "orchestune")

    with pytest.raises(ConflictError, match="--migrate-legacy"):
        migrate_legacy_skills(
            target_root, payload, consumers=["codex"], migrate_legacy=False
        )


def test_migrate_broken_or_unrelated_symlink_fails(tmp_path: Path):
    _, payload = _setup_skill_source(tmp_path)
    target_root = tmp_path / "target" / ".agents" / "skills"
    target_root.mkdir(parents=True)
    legacy_link = target_root / "orchestune"
    # Point to nonexistent or unrelated path
    legacy_link.symlink_to(tmp_path / "unrelated_dir")

    with pytest.raises(ConflictError, match="Cannot migrate"):
        migrate_legacy_skills(
            target_root, payload, consumers=["codex"], migrate_legacy=True
        )
