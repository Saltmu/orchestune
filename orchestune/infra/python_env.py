"""Python dependency installation and virtualenv resolution at the L1 boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path


def install_dependencies(repository_root: Path, env: dict[str, str]) -> str | None:
    """Install a repository's Python dependencies when it uses Poetry."""
    if not (repository_root / "pyproject.toml").exists():
        return None
    try:
        subprocess.run(
            ["poetry", "install"],
            cwd=str(repository_root),
            check=True,
            capture_output=True,
            env=env,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        return f"Failed to install Poetry dependencies: {exc}"
    return None


def resolve_virtualenv_path(
    repository_root: Path, original_root: Path, env: dict[str, str]
) -> Path | None:
    """Resolve the managed virtualenv, falling back to nearby ``.venv`` paths."""
    poetry_path = _poetry_virtualenv_path(repository_root, env)
    return poetry_path or _fallback_venv_path(repository_root, original_root)


def _poetry_virtualenv_path(repository_root: Path, env: dict[str, str]) -> Path | None:
    if not (repository_root / "pyproject.toml").exists():
        return None
    try:
        result = subprocess.run(
            ["poetry", "env", "info", "--path"],
            cwd=str(repository_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            env=env,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    path = Path(result.stdout.strip())
    return path if path.exists() else None


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
