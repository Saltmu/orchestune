import json
import os
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from orchestune.installer.contracts import (
    BundlePayload,
    BundleState,
    ConflictError,
    FileRecord,
    PhysicalRoot,
    ScopeType,
    SkillPayload,
    TargetType,
    TransactionError,
)
from orchestune.installer.engine import install_skills, uninstall_skills
from orchestune.installer.payload import calculate_file_sha256
from orchestune.installer.state import (
    BundleManifestEntry,
    inspect_bundle_state,
    load_manifest,
    save_manifest,
)
from orchestune.installer.transaction import (
    SkillTransaction,
    recover_pending_transactions,
)


def _make_payload(
    tmp_path: Path, skill_names: tuple[str, ...] = ("orchestune",)
) -> BundlePayload:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    skills: dict[str, SkillPayload] = {}

    for skill_name in skill_names:
        skill_dir = source_dir / skill_name
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(f"managed {skill_name}", encoding="utf-8")
        skills[skill_name] = SkillPayload(
            name=skill_name,
            files={
                "SKILL.md": FileRecord(
                    relative_path="SKILL.md",
                    sha256=calculate_file_sha256(skill_file),
                    mode="regular",
                )
            },
            directories=[],
        )

    return BundlePayload(
        bundle_name="standard",
        package_version="0.5.0",
        source_kind="installed_distribution",
        bundle_digest="test-digest",
        skills=skills,
        source_path=source_dir,
    )


def _physical_root(path: Path, target: TargetType = TargetType.CODEX) -> PhysicalRoot:
    return PhysicalRoot(path=path, target_types=[target], scope=ScopeType.PROJECT)


def _uninstall_journal_details(
    skills_root: Path, tx: SkillTransaction
) -> dict[str, object]:
    manifest = load_manifest(skills_root)
    assert manifest is not None
    entry = manifest.bundles["standard"]
    return {
        "schema_version": 1,
        "operation": "uninstall",
        "bundle_name": "standard",
        "manifest_generation_before": manifest.generation,
        "entry_before": asdict(entry),
        "remaining_consumers": [],
        "skills": list(entry.skills),
        "moved_to_backup": [],
    }


def test_uninstall_cleanup_failure_is_recoverable_and_retry_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skills_root = tmp_path / "skills"
    payload = _make_payload(tmp_path)
    root = _physical_root(skills_root)
    install_skills(root, payload)

    real_rmtree = shutil.rmtree

    def fail_managed_skill_cleanup(path: str | Path, *args: Any, **kwargs: Any) -> None:
        candidate = Path(path)
        if candidate.name in payload.skills and (
            candidate.parent == skills_root or candidate.parent.name == "backup"
        ):
            raise PermissionError("injected skill deletion failure")
        real_rmtree(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", fail_managed_skill_cleanup)
        with pytest.raises((PermissionError, TransactionError)):
            uninstall_skills(root, payload)

    assert inspect_bundle_state(skills_root, payload) is BundleState.RECOVERY_REQUIRED
    transaction_root = skills_root / ".orchestune-installer" / "transactions"
    journals = list(transaction_root.glob("*/journal.json"))
    assert len(journals) == 1
    journal = json.loads(journals[0].read_text(encoding="utf-8"))
    assert journal["operation"] == "uninstall"
    assert journal["bundle_name"] == "standard"
    assert journal["entry_before"]["skills"] == {
        "orchestune": {
            "files": {
                "SKILL.md": {
                    "sha256": payload.skills["orchestune"].files["SKILL.md"].sha256,
                    "mode": "regular",
                }
            },
            "directories": [],
        }
    }

    result = uninstall_skills(root, payload)

    assert result.state_before is BundleState.RECOVERY_REQUIRED
    assert result.state_after is BundleState.ABSENT
    assert any("pending uninstall" in action for action in result.actions)
    assert not (skills_root / "orchestune").exists()
    manifest = load_manifest(skills_root)
    assert manifest is not None
    assert "standard" not in manifest.bundles
    assert not list(transaction_root.glob("*/journal.json"))


def test_uninstall_dry_run_does_not_create_or_change_transaction_files(
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    payload = _make_payload(tmp_path)
    root = _physical_root(skills_root)
    install_skills(root, payload)
    transaction_root = skills_root / ".orchestune-installer" / "transactions"
    before = {
        path.relative_to(skills_root): (
            path.stat().st_mtime_ns,
            path.read_bytes() if path.is_file() else None,
        )
        for path in skills_root.rglob("*")
    }

    result = uninstall_skills(root, payload, dry_run=True)

    after = {
        path.relative_to(skills_root): (
            path.stat().st_mtime_ns,
            path.read_bytes() if path.is_file() else None,
        )
        for path in skills_root.rglob("*")
    }
    assert result.actions == ["dry_run_preview"]
    assert before == after
    assert not list(transaction_root.glob("*/journal.json"))


def test_uninstall_rechecks_initially_absent_state_under_transaction_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skills_root = tmp_path / "skills"
    payload = _make_payload(tmp_path)
    root = _physical_root(skills_root)
    install_skills(root, payload)
    real_inspect = inspect_bundle_state
    inspect_calls = 0

    def report_stale_absence(path: Path, current_payload: BundlePayload) -> BundleState:
        nonlocal inspect_calls
        inspect_calls += 1
        if inspect_calls == 1:
            return BundleState.ABSENT
        return real_inspect(path, current_payload)

    with monkeypatch.context() as patch:
        patch.setattr(
            "orchestune.installer.engine.inspect_bundle_state", report_stale_absence
        )
        result = uninstall_skills(root, payload)

    assert inspect_calls >= 3
    assert any("removed standard bundle files" in action for action in result.actions)
    assert not (skills_root / "orchestune").exists()


def test_uninstall_preserves_files_owned_by_another_bundle(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    payload = _make_payload(tmp_path)
    root = _physical_root(skills_root)
    install_skills(root, payload)
    manifest = load_manifest(skills_root)
    assert manifest is not None
    manifest.bundles["other"] = BundleManifestEntry(
        package_version="0.5.0",
        source_kind="installed_distribution",
        bundle_digest="other-digest",
        consumers=["claude"],
        skills=manifest.bundles["standard"].skills.copy(),
    )
    save_manifest(skills_root, manifest)
    content = (skills_root / "orchestune" / "SKILL.md").read_text(encoding="utf-8")

    with pytest.raises(ConflictError, match="another bundle"):
        uninstall_skills(root, payload)

    assert (skills_root / "orchestune" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == content
    updated_manifest = load_manifest(skills_root)
    assert updated_manifest is not None
    assert "standard" in updated_manifest.bundles
    assert "other" in updated_manifest.bundles


def test_uninstall_recovery_refuses_to_overwrite_new_public_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skills_root = tmp_path / "skills"
    payload = _make_payload(tmp_path)
    root = _physical_root(skills_root)
    install_skills(root, payload)
    tx = SkillTransaction(skills_root)
    tx.tx_dir.mkdir(parents=True)
    tx.backup_dir.mkdir()
    tx._write_journal(
        "UNINSTALL_PREPARED",
        {
            "schema_version": 1,
            "operation": "uninstall",
            "bundle_name": "standard",
            **_uninstall_journal_details(skills_root, tx),
        },
    )
    backup_skill = tx.backup_dir / "orchestune"
    (skills_root / "orchestune").replace(backup_skill)
    tx.journal_path.write_text(
        json.dumps(
            {
                **json.loads(tx.journal_path.read_text(encoding="utf-8")),
                "moved_to_backup": ["orchestune"],
            }
        ),
        encoding="utf-8",
    )
    new_skill = skills_root / "orchestune"
    new_skill.mkdir()
    (new_skill / "SKILL.md").write_text("user-created", encoding="utf-8")

    with pytest.raises(TransactionError, match="(?i)both the public path and backup"):
        recover_pending_transactions(skills_root)

    assert (new_skill / "SKILL.md").read_text(encoding="utf-8") == "user-created"
    assert (backup_skill / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "managed orchestune"
    assert tx.journal_path.is_file()


def test_partial_multiskill_staging_failure_restores_every_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skills_root = tmp_path / "skills"
    payload = _make_payload(tmp_path, ("skill_a", "skill_b"))
    root = _physical_root(skills_root)
    install_skills(root, payload)
    real_replace = os.replace

    def fail_second_backup_move(source: str | Path, destination: str | Path) -> None:
        if (
            Path(source) == skills_root / "skill_b"
            and Path(destination).parent.name == "backup"
        ):
            raise OSError("injected second-skill staging failure")
        real_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_second_backup_move)
        with pytest.raises(OSError, match="second-skill staging failure"):
            uninstall_skills(root, payload)

    for skill_name in payload.skills:
        assert (skills_root / skill_name / "SKILL.md").read_text(encoding="utf-8") == (
            f"managed {skill_name}"
        )
    manifest = load_manifest(skills_root)
    assert manifest is not None
    assert manifest.bundles["standard"].consumers == ["codex"]
    assert not list(
        (skills_root / ".orchestune-installer" / "transactions").glob("*/journal.json")
    )


def test_recovery_uses_backup_contents_when_rename_progress_was_not_journaled(
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    payload = _make_payload(tmp_path, ("skill_a", "skill_b"))
    root = _physical_root(skills_root)
    install_skills(root, payload)
    tx = SkillTransaction(skills_root)
    tx.tx_dir.mkdir(parents=True)
    tx.backup_dir.mkdir()
    tx._write_journal("UNINSTALL_PREPARED", _uninstall_journal_details(skills_root, tx))
    (skills_root / "skill_a").replace(tx.backup_dir / "skill_a")

    recover_pending_transactions(skills_root)

    for skill_name in payload.skills:
        assert (skills_root / skill_name / "SKILL.md").read_text(encoding="utf-8") == (
            f"managed {skill_name}"
        )
    assert not tx.tx_dir.exists()


def test_unknown_uninstall_journal_version_is_preserved_and_rejected(
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    payload = _make_payload(tmp_path)
    root = _physical_root(skills_root)
    install_skills(root, payload)
    tx = SkillTransaction(skills_root)
    tx.tx_dir.mkdir(parents=True)
    tx.backup_dir.mkdir()
    details = _uninstall_journal_details(skills_root, tx)
    details["schema_version"] = 99
    tx._write_journal("UNINSTALL_PREPARED", details)

    with pytest.raises(TransactionError, match="unsupported uninstall journal schema"):
        recover_pending_transactions(skills_root)

    assert (skills_root / "orchestune" / "SKILL.md").read_text(encoding="utf-8") == (
        "managed orchestune"
    )
    assert tx.journal_path.is_file()


def test_unknown_transaction_operation_is_preserved_and_rejected(
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    payload = _make_payload(tmp_path)
    install_skills(_physical_root(skills_root), payload)
    tx = SkillTransaction(skills_root)
    tx.tx_dir.mkdir(parents=True)
    tx.backup_dir.mkdir()
    details = _uninstall_journal_details(skills_root, tx)
    details["operation"] = "reinstall"
    tx._write_journal("UNINSTALL_PREPARED", details)

    with pytest.raises(TransactionError, match="Unknown transaction operation"):
        recover_pending_transactions(skills_root)

    assert (skills_root / "orchestune" / "SKILL.md").read_text(encoding="utf-8") == (
        "managed orchestune"
    )
    assert tx.journal_path.is_file()


def test_uninstall_journal_rejects_unsafe_target_path(
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    payload = _make_payload(tmp_path)
    install_skills(_physical_root(skills_root), payload)
    tx = SkillTransaction(skills_root)
    tx.tx_dir.mkdir(parents=True)
    tx.backup_dir.mkdir()
    details = _uninstall_journal_details(skills_root, tx)
    details["skills"] = ["../outside"]
    tx._write_journal("UNINSTALL_PREPARED", details)

    with pytest.raises(TransactionError, match="Invalid uninstall skill path"):
        recover_pending_transactions(skills_root)

    assert (skills_root / "orchestune" / "SKILL.md").read_text(encoding="utf-8") == (
        "managed orchestune"
    )
    assert tx.journal_path.is_file()


def test_manifest_save_failure_restores_all_staged_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skills_root = tmp_path / "skills"
    payload = _make_payload(tmp_path, ("skill_a", "skill_b"))
    root = _physical_root(skills_root)
    install_skills(root, payload)

    def fail_manifest_save(*args: object, **kwargs: object) -> None:
        raise OSError("injected manifest replacement failure")

    with monkeypatch.context() as patch:
        patch.setattr(
            "orchestune.installer.transaction.save_manifest", fail_manifest_save
        )
        with pytest.raises(OSError, match="manifest replacement failure"):
            uninstall_skills(root, payload)

    for skill_name in payload.skills:
        assert (skills_root / skill_name / "SKILL.md").read_text(encoding="utf-8") == (
            f"managed {skill_name}"
        )
    manifest = load_manifest(skills_root)
    assert manifest is not None
    assert set(manifest.bundles["standard"].skills) == set(payload.skills)
    assert not list(
        (skills_root / ".orchestune-installer" / "transactions").glob("*/journal.json")
    )


def test_recovery_after_manifest_save_finishes_committed_uninstall(
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    payload = _make_payload(tmp_path, ("skill_a", "skill_b"))
    root = _physical_root(skills_root)
    install_skills(root, payload)
    tx = SkillTransaction(skills_root)
    tx.tx_dir.mkdir(parents=True)
    tx.backup_dir.mkdir()
    details = _uninstall_journal_details(skills_root, tx)
    details["moved_to_backup"] = ["skill_a", "skill_b"]
    tx._write_journal("UNINSTALL_STAGED", details)
    for skill_name in payload.skills:
        (skills_root / skill_name).replace(tx.backup_dir / skill_name)

    manifest = load_manifest(skills_root)
    assert manifest is not None
    del manifest.bundles["standard"]
    manifest.generation += 1
    manifest.transaction_id = tx.tx_id
    save_manifest(skills_root, manifest)
    (tx.backup_dir / "skill_a" / "SKILL.md").unlink()

    completed = recover_pending_transactions(skills_root)

    assert completed == ["standard"]
    assert not (skills_root / "skill_a").exists()
    assert not (skills_root / "skill_b").exists()
    assert not tx.tx_dir.exists()
    committed_manifest = load_manifest(skills_root)
    assert committed_manifest is not None
    assert "standard" not in committed_manifest.bundles


def test_committed_recovery_preserves_changed_backup_and_journal(
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    payload = _make_payload(tmp_path)
    install_skills(_physical_root(skills_root), payload)
    tx = SkillTransaction(skills_root)
    tx.tx_dir.mkdir(parents=True)
    tx.backup_dir.mkdir()
    details = _uninstall_journal_details(skills_root, tx)
    details["moved_to_backup"] = ["orchestune"]
    tx._write_journal("UNINSTALL_STAGED", details)
    (skills_root / "orchestune").replace(tx.backup_dir / "orchestune")

    manifest = load_manifest(skills_root)
    assert manifest is not None
    del manifest.bundles["standard"]
    manifest.generation += 1
    manifest.transaction_id = tx.tx_id
    save_manifest(skills_root, manifest)
    user_file = tx.backup_dir / "orchestune" / "user.txt"
    user_file.write_text("keep me", encoding="utf-8")

    with pytest.raises(TransactionError, match="Committed uninstall backup changed"):
        recover_pending_transactions(skills_root)

    assert user_file.read_text(encoding="utf-8") == "keep me"
    assert (tx.backup_dir / "orchestune" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "managed orchestune"
    assert tx.journal_path.is_file()


def test_uninstall_does_not_remove_other_consumers_skills(
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    payload = _make_payload(tmp_path)
    install_skills(_physical_root(skills_root), payload)
    antigravity_root = _physical_root(skills_root, TargetType.ANTIGRAVITY)

    result = uninstall_skills(antigravity_root, payload)

    assert result.success is True
    assert "no-op" in result.actions[0]
    assert (skills_root / "orchestune" / "SKILL.md").read_text(encoding="utf-8") == (
        "managed orchestune"
    )
    transaction_root = skills_root / ".orchestune-installer" / "transactions"
    assert list(transaction_root.iterdir()) == []


def test_uninstall_revalidates_backup_before_manifest_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skills_root = tmp_path / "skills"
    payload = _make_payload(tmp_path)
    root = _physical_root(skills_root)
    install_skills(root, payload)
    real_replace = os.replace

    def add_user_file_after_staging(
        source: str | Path, destination: str | Path
    ) -> None:
        real_replace(source, destination)
        if Path(source) == skills_root / "orchestune":
            (Path(destination) / "user.txt").write_text("keep me", encoding="utf-8")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", add_user_file_after_staging)
        with pytest.raises(ConflictError, match="changed while staging"):
            uninstall_skills(root, payload)

    assert (skills_root / "orchestune" / "SKILL.md").read_text(encoding="utf-8") == (
        "managed orchestune"
    )
    assert (skills_root / "orchestune" / "user.txt").read_text(encoding="utf-8") == (
        "keep me"
    )
    manifest = load_manifest(skills_root)
    assert manifest is not None
    assert "standard" in manifest.bundles
    assert not list(
        (skills_root / ".orchestune-installer" / "transactions").glob("*/journal.json")
    )


def test_uninstall_refuses_symlinked_managed_file(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    payload = _make_payload(tmp_path)
    root = _physical_root(skills_root)
    install_skills(root, payload)
    skill_file = skills_root / "orchestune" / "SKILL.md"
    external_file = tmp_path / "external.md"
    external_file.write_text("managed orchestune", encoding="utf-8")
    skill_file.unlink()
    try:
        skill_file.symlink_to(external_file)
    except OSError as err:
        pytest.skip(f"symlinks are unavailable: {err}")

    with pytest.raises(ConflictError, match="has changed"):
        uninstall_skills(root, payload)

    assert skill_file.is_symlink()
    manifest = load_manifest(skills_root)
    assert manifest is not None
    assert "standard" in manifest.bundles
