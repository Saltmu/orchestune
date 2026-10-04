from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orchestune.installer.contracts import BundlePayload, ConflictError
from orchestune.installer.state import InstallerManifest, load_manifest
from orchestune.installer.transaction import SkillTransaction


@dataclass
class LegacySkillCandidate:
    skill_name: str
    path: Path
    is_symlink: bool
    link_target: Path | None
    can_migrate: bool
    reason: str


def _inspect_symlink_candidate(
    skill_name: str, skill_path: Path, payload: BundlePayload
) -> LegacySkillCandidate:
    try:
        target = skill_path.resolve()
        if not target.exists():
            return LegacySkillCandidate(
                skill_name=skill_name,
                path=skill_path,
                is_symlink=True,
                link_target=None,
                can_migrate=False,
                reason="Symlink is broken",
            )

        expected_source = (payload.source_path / skill_name).resolve()
        if target == expected_source or (target / "SKILL.md").is_file():
            return LegacySkillCandidate(
                skill_name=skill_name,
                path=skill_path,
                is_symlink=True,
                link_target=target,
                can_migrate=True,
                reason="Valid symlink pointing to skill source",
            )
        return LegacySkillCandidate(
            skill_name=skill_name,
            path=skill_path,
            is_symlink=True,
            link_target=target,
            can_migrate=False,
            reason="Symlink points to unrelated path",
        )
    except Exception as e:
        return LegacySkillCandidate(
            skill_name=skill_name,
            path=skill_path,
            is_symlink=True,
            link_target=None,
            can_migrate=False,
            reason=f"Failed to resolve symlink: {e}",
        )


def _inspect_dir_candidate(skill_name: str, skill_path: Path) -> LegacySkillCandidate:
    if (skill_path / "SKILL.md").is_file():
        return LegacySkillCandidate(
            skill_name=skill_name,
            path=skill_path,
            is_symlink=False,
            link_target=None,
            can_migrate=True,
            reason="Legacy copy of skill present",
        )
    return LegacySkillCandidate(
        skill_name=skill_name,
        path=skill_path,
        is_symlink=False,
        link_target=None,
        can_migrate=False,
        reason="Occupied by unrelated directory without SKILL.md",
    )


def detect_legacy_skills(
    skills_root: Path,
    payload: BundlePayload,
) -> list[LegacySkillCandidate]:
    manifest = load_manifest(skills_root)
    managed_skills = set()
    if manifest and payload.bundle_name in manifest.bundles:
        managed_skills = set(manifest.bundles[payload.bundle_name].skills.keys())

    candidates: list[LegacySkillCandidate] = []

    for skill_name in payload.skills.keys():
        if skill_name in managed_skills:
            continue

        skill_path = skills_root / skill_name
        if not (skill_path.exists() or skill_path.is_symlink()):
            continue

        if skill_path.is_symlink():
            candidates.append(
                _inspect_symlink_candidate(skill_name, skill_path, payload)
            )
        elif skill_path.is_dir():
            candidates.append(_inspect_dir_candidate(skill_name, skill_path))

    return candidates


def migrate_legacy_skills(
    skills_root: Path,
    payload: BundlePayload,
    consumers: list[str],
    migrate_legacy: bool = False,
) -> None:
    candidates = detect_legacy_skills(skills_root, payload)
    if not candidates:
        return

    if not migrate_legacy:
        names = ", ".join(c.skill_name for c in candidates)
        raise ConflictError(
            f"Legacy unmanaged skill(s) detected: {names}. Use --migrate-legacy to migrate them."
        )

    for c in candidates:
        if not c.can_migrate:
            raise ConflictError(
                f"Cannot migrate legacy skill '{c.skill_name}': {c.reason}. Please remove or backup manually."
            )

    # Perform migration transaction
    tx = SkillTransaction(skills_root)
    with tx:
        tx.prepare_stage(payload)
        manifest = load_manifest(skills_root) or InstallerManifest()
        tx.commit(payload, manifest, consumers=consumers)
