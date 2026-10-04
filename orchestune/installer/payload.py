from __future__ import annotations

import hashlib
import importlib.metadata
import os
import tomllib
from pathlib import Path

from orchestune.installer.contracts import (
    BundlePayload,
    FileRecord,
    PayloadError,
    SkillPayload,
)
from orchestune.version import get_version

STANDARD_SKILLS = ("orchestune", "orchestune-provision", "orchestune-dispatch")
WORKFLOW_SKILL = "workflow-template"
EXCLUDED_SKILLS = ("local-ci-developer",)


def calculate_file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def _read_pyproject_version(pyproject_path: Path) -> str:
    try:
        content = pyproject_path.read_text(encoding="utf-8")
        data = tomllib.loads(content)
        return str(data["project"]["version"])
    except Exception as e:
        raise PayloadError(f"Failed to read version from {pyproject_path}: {e}") from e


def _build_skill_payload(skill_dir: Path, skill_name: str) -> SkillPayload:
    if not skill_dir.is_dir():
        raise PayloadError(f"Skill directory not found: {skill_dir}")

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise PayloadError(f"Missing SKILL.md in skill '{skill_name}' at {skill_dir}")

    files: dict[str, FileRecord] = {}
    directories: set[str] = set()

    for root, dirs, filenames in os.walk(skill_dir):
        root_path = Path(root)
        rel_root = root_path.relative_to(skill_dir)
        if str(rel_root) != ".":
            directories.add(rel_root.as_posix())

        for d in dirs:
            dir_full = root_path / d
            if dir_full.is_symlink():
                raise PayloadError(
                    f"Symlinks are not allowed in payload source: {dir_full}"
                )
            rel_dir = (rel_root / d).as_posix() if str(rel_root) != "." else d
            directories.add(rel_dir)

        for filename in filenames:
            file_full = root_path / filename
            if file_full.is_symlink():
                raise PayloadError(
                    f"Symlinks are not allowed in payload source: {file_full}"
                )
            if not file_full.is_file():
                raise PayloadError(
                    f"Special files are not allowed in payload source: {file_full}"
                )

            rel_file = (
                (rel_root / filename).as_posix() if str(rel_root) != "." else filename
            )
            sha = calculate_file_sha256(file_full)
            mode = (
                "executable"
                if os.access(file_full, os.X_OK) and not os.name == "nt"
                else "regular"
            )
            files[rel_file] = FileRecord(relative_path=rel_file, sha256=sha, mode=mode)

    return SkillPayload(
        name=skill_name,
        files=files,
        directories=sorted(directories),
    )


def _calculate_bundle_digest(skills: dict[str, SkillPayload]) -> str:
    hasher = hashlib.sha256()
    for skill_name in sorted(skills.keys()):
        skill = skills[skill_name]
        hasher.update(skill_name.encode("utf-8"))
        for rel_path in sorted(skill.files.keys()):
            file_record = skill.files[rel_path]
            hasher.update(
                f"{rel_path}:{file_record.sha256}:{file_record.mode}\n".encode()
            )
    return hasher.hexdigest()


def _resolve_distribution_skills_dir() -> tuple[Path, str]:
    current_version = get_version()
    try:
        dist = importlib.metadata.distribution("orchestune")
        if dist.version != current_version:
            raise PayloadError(
                f"Installed distribution version '{dist.version}' does not match CLI version '{current_version}'"
            )
        # Try to locate files from distribution relative to distribution root
        if dist.files:
            for f in dist.files:
                if "skills" in f.parts:
                    idx = f.parts.index("skills")
                    rel_skills = Path(*f.parts[: idx + 1])
                    skills_candidate = Path(str(dist.locate_file(rel_skills)))
                    if (skills_candidate / "orchestune" / "SKILL.md").is_file():
                        return skills_candidate.resolve(), current_version
    except importlib.metadata.PackageNotFoundError:
        pass

    # Fallback to package relative (for editable installs or source tree execution)
    pkg_dir = Path(__file__).resolve().parent.parent
    candidates = [
        pkg_dir / "skills",
        pkg_dir.parent / "skills",
    ]

    for cand in candidates:
        if cand.is_dir() and (cand / "orchestune" / "SKILL.md").is_file():
            return cand.resolve(), current_version

    raise PayloadError(
        "Could not locate package skills. Please specify --source-dir or ensure orchestune is installed."
    )


def resolve_payload(
    source_dir: Path | None = None,
    with_workflow_skill: bool = False,
) -> BundlePayload:
    current_version = get_version()

    if source_dir is not None:
        source_root = source_dir.resolve()
        pyproject = source_root / "pyproject.toml"
        if not pyproject.is_file():
            raise PayloadError(
                f"Missing pyproject.toml in source directory: {source_root}"
            )
        version = _read_pyproject_version(pyproject)
        if version != current_version:
            raise PayloadError(
                f"Version mismatch: source directory has version '{version}' but running CLI is '{current_version}'"
            )
        skills_dir = source_root / "skills"
        if not skills_dir.is_dir():
            raise PayloadError(
                f"Missing skills/ directory in source directory: {source_root}"
            )
        source_kind = "source_directory"
    else:
        skills_dir, version = _resolve_distribution_skills_dir()
        source_kind = "installed_distribution"

    bundle_name = "workflow" if with_workflow_skill else "standard"
    target_skill_names = (
        [WORKFLOW_SKILL] if with_workflow_skill else list(STANDARD_SKILLS)
    )

    skills: dict[str, SkillPayload] = {}
    for skill_name in target_skill_names:
        skill_path = skills_dir / skill_name
        skills[skill_name] = _build_skill_payload(skill_path, skill_name)

    digest = _calculate_bundle_digest(skills)

    return BundlePayload(
        bundle_name=bundle_name,
        package_version=version,
        source_kind=source_kind,
        bundle_digest=digest,
        skills=skills,
        source_path=skills_dir,
    )
