from pathlib import Path

import pytest

from orchestune.installer.contracts import (
    BundlePayload,
    ConflictError,
    FileRecord,
    PhysicalRoot,
    ScopeType,
    SkillPayload,
    TargetType,
)
from orchestune.installer.engine import (
    install_skills,
    uninstall_skills,
    update_skills,
)
from orchestune.installer.state import load_manifest
from orchestune.installer.transaction import SkillTransaction


def _create_payload(
    src_dir: Path, version="0.5.0", digest="digest_v1"
) -> BundlePayload:
    s_dir = src_dir / "orchestune"
    s_dir.mkdir(parents=True, exist_ok=True)
    sk = s_dir / "SKILL.md"
    sk.write_text("skill v1", encoding="utf-8")

    from orchestune.installer.payload import calculate_file_sha256

    real_sha = calculate_file_sha256(sk)

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


def test_install_skills_dry_run_no_writes(tmp_path: Path):
    target_root = tmp_path / "skills"
    # Not created
    payload = _create_payload(tmp_path / "src")

    root = PhysicalRoot(
        path=target_root, target_types=[TargetType.CODEX], scope=ScopeType.PROJECT
    )
    result = install_skills(root, payload, dry_run=True)

    assert result.success is True
    assert not target_root.exists()


def test_install_skills_and_add_consumer(tmp_path: Path):
    target_root = tmp_path / "skills"
    payload = _create_payload(tmp_path / "src")

    # 1. Install for codex
    root_codex = PhysicalRoot(
        path=target_root, target_types=[TargetType.CODEX], scope=ScopeType.PROJECT
    )
    res1 = install_skills(root_codex, payload, dry_run=False)
    assert res1.success is True
    assert (target_root / "orchestune" / "SKILL.md").is_file()

    m1 = load_manifest(target_root)
    assert m1 is not None
    assert m1.bundles["standard"].consumers == ["codex"]

    # 2. Install for antigravity (same physical root)
    root_ag = PhysicalRoot(
        path=target_root, target_types=[TargetType.ANTIGRAVITY], scope=ScopeType.PROJECT
    )
    res2 = install_skills(root_ag, payload, dry_run=False)
    assert res2.success is True

    m2 = load_manifest(target_root)
    assert m2 is not None
    assert sorted(m2.bundles["standard"].consumers) == ["antigravity", "codex"]


def test_update_skills(tmp_path: Path):
    target_root = tmp_path / "skills"
    payload_v1 = _create_payload(
        tmp_path / "src_v1", version="0.5.0", digest="digest_v1"
    )

    root = PhysicalRoot(
        path=target_root, target_types=[TargetType.CODEX], scope=ScopeType.PROJECT
    )
    install_skills(root, payload_v1)

    # Prepare v2 payload
    src_v2 = tmp_path / "src_v2"
    payload_v2 = _create_payload(src_v2, version="0.6.0", digest="digest_v2")
    (src_v2 / "orchestune" / "SKILL.md").write_text("skill v2", encoding="utf-8")
    from orchestune.installer.payload import calculate_file_sha256

    v2_sha = calculate_file_sha256(src_v2 / "orchestune" / "SKILL.md")
    payload_v2 = BundlePayload(
        bundle_name="standard",
        package_version="0.6.0",
        source_kind="installed_distribution",
        bundle_digest="digest_v2",
        skills={
            "orchestune": SkillPayload(
                name="orchestune",
                files={
                    "SKILL.md": FileRecord(
                        relative_path="SKILL.md", sha256=v2_sha, mode="regular"
                    )
                },
                directories=[],
            )
        },
        source_path=src_v2,
    )

    update_res = update_skills(root, payload_v2)
    assert update_res.success is True
    assert (target_root / "orchestune" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "skill v2"

    m = load_manifest(target_root)
    assert m is not None
    assert m.bundles["standard"].package_version == "0.6.0"


def test_update_rejected_on_user_modification(tmp_path: Path):
    target_root = tmp_path / "skills"
    payload_v1 = _create_payload(
        tmp_path / "src_v1", version="0.5.0", digest="digest_v1"
    )
    root = PhysicalRoot(
        path=target_root, target_types=[TargetType.CODEX], scope=ScopeType.PROJECT
    )
    install_skills(root, payload_v1)

    # User modifies SKILL.md
    (target_root / "orchestune" / "SKILL.md").write_text(
        "modified by user", encoding="utf-8"
    )

    src_v2 = tmp_path / "src_v2"
    payload_v2 = _create_payload(src_v2, version="0.6.0", digest="digest_v2")

    with pytest.raises(ConflictError, match="modified"):
        update_skills(root, payload_v2)


def test_uninstall_skills_consumer_and_full_removal(tmp_path: Path):
    target_root = tmp_path / "skills"
    payload = _create_payload(tmp_path / "src")

    # Install for codex and antigravity
    root_both = PhysicalRoot(
        path=target_root,
        target_types=[TargetType.CODEX, TargetType.ANTIGRAVITY],
        scope=ScopeType.PROJECT,
    )
    install_skills(root_both, payload)

    # 1. Uninstall codex -> antigravity remains, skill directory stays
    root_codex = PhysicalRoot(
        path=target_root, target_types=[TargetType.CODEX], scope=ScopeType.PROJECT
    )
    res1 = uninstall_skills(root_codex, payload)
    assert res1.success is True
    assert (target_root / "orchestune" / "SKILL.md").is_file()
    m1 = load_manifest(target_root)
    assert m1 is not None
    assert m1.bundles["standard"].consumers == ["antigravity"]

    # 2. Uninstall antigravity -> last consumer removed, skill directory removed
    root_ag = PhysicalRoot(
        path=target_root, target_types=[TargetType.ANTIGRAVITY], scope=ScopeType.PROJECT
    )
    res2 = uninstall_skills(root_ag, payload)
    assert res2.success is True
    assert not (target_root / "orchestune").exists()
    m2 = load_manifest(target_root)
    assert m2 is not None
    assert "standard" not in m2.bundles


def test_install_skills_recovers_pending_transaction(tmp_path: Path):
    target_root = tmp_path / "skills"
    target_root.mkdir()
    payload = _create_payload(tmp_path / "src")

    # Simulate a crashed transaction
    tx = SkillTransaction(target_root)
    tx.tx_dir.mkdir(parents=True)
    tx.backup_dir.mkdir(parents=True)
    tx._write_journal("PREPARED", {"bundle_name": "standard", "skills": ["orchestune"]})

    root = PhysicalRoot(
        path=target_root, target_types=[TargetType.CODEX], scope=ScopeType.PROJECT
    )
    res = install_skills(root, payload)
    assert res.success is True
    assert (target_root / "orchestune" / "SKILL.md").is_file()
    assert not tx.tx_dir.exists()


def test_update_skills_recovers_pending_transaction(tmp_path: Path):
    target_root = tmp_path / "skills"
    payload_v1 = _create_payload(
        tmp_path / "src_v1", version="0.5.0", digest="digest_v1"
    )
    root = PhysicalRoot(
        path=target_root, target_types=[TargetType.CODEX], scope=ScopeType.PROJECT
    )
    install_skills(root, payload_v1)

    # Simulate crashed transaction during previous update
    tx = SkillTransaction(target_root)
    tx.tx_dir.mkdir(parents=True)
    tx.backup_dir.mkdir(parents=True)
    (tx.backup_dir / "orchestune").mkdir()
    (tx.backup_dir / "orchestune" / "SKILL.md").write_text("skill v1", encoding="utf-8")
    if (target_root / "orchestune" / "SKILL.md").exists():
        (target_root / "orchestune" / "SKILL.md").unlink()
    tx._write_journal(
        "OLD_MOVED", {"bundle_name": "standard", "skills": ["orchestune"]}
    )

    # Prepare v2 payload
    src_v2 = tmp_path / "src_v2"
    payload_v2 = _create_payload(src_v2, version="0.6.0", digest="digest_v2")
    (src_v2 / "orchestune" / "SKILL.md").write_text("skill v2", encoding="utf-8")
    from orchestune.installer.payload import calculate_file_sha256

    v2_sha = calculate_file_sha256(src_v2 / "orchestune" / "SKILL.md")
    payload_v2.skills["orchestune"].files["SKILL.md"] = FileRecord(
        relative_path="SKILL.md", sha256=v2_sha, mode="regular"
    )

    update_res = update_skills(root, payload_v2)
    assert update_res.success is True
    assert (target_root / "orchestune" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "skill v2"
    m = load_manifest(target_root)
    assert m is not None
    assert m.bundles["standard"].package_version == "0.6.0"
