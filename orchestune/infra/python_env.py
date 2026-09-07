"""Python dependency installation and virtualenv resolution at the L1 boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path


def install_dependencies(repository_root: Path, env: dict[str, str]) -> str | None:
    """Synchronize a repository's Python dependencies with uv."""
    if not (repository_root / "pyproject.toml").exists():
        return None
    try:
        subprocess.run(
            ["uv", "sync"],
            cwd=str(repository_root),
            check=True,
            capture_output=True,
            env=env,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        return f"Failed to sync uv dependencies: {exc}"
    return None


def resolve_virtualenv_path(
    repository_root: Path, original_root: Path, env: dict[str, str]
) -> Path | None:
    """Resolve the repository-local environment created by ``uv sync``."""
    return _fallback_venv_path(repository_root, original_root)


def _fallback_venv_path(repository_root: Path, original_root: Path) -> Path | None:
    for path in (repository_root / ".venv", original_root / ".venv"):
        if path.exists():
            return path
    return _find_ancestor_venv(original_root)


def _find_ancestor_venv(start: Path) -> Path | None:
    for ancestor in start.parents:
        candidate = ancestor / ".venv"
        if candidate.exists():
            return candidate
    return None
