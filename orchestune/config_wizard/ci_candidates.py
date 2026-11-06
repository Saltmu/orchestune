"""Suggest CI commands from repository files without executing tools."""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from pathlib import Path

from orchestune.infra.process_utils import (
    default_ci_command,
    platform_ci_script_relative,
)


def _make_targets(root: Path) -> set[str]:
    try:
        lines = (root / "Makefile").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return set()
    targets = set()
    for line in lines:
        if line.startswith(("\t", "#")):
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*:", line)
        if match:
            targets.add(match[1])
            if match[1] == ".PHONY":
                targets.update(line.split(":", 1)[1].split("#", 1)[0].split())
    return targets


def _npm_scripts(root: Path) -> set[str]:
    try:
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return set()
    scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
    if not isinstance(scripts, dict):
        return set()
    return {
        key
        for key, value in scripts.items()
        if isinstance(value, str) and value.strip()
    }


def _has_pytest(root: Path) -> bool:
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    tool = data.get("tool", {})
    return isinstance(tool, dict) and isinstance(tool.get("pytest"), dict)


def suggest_ci_commands(root: Path) -> list[str]:
    """Return ordered, file-based candidates; unreadable files are ignored."""
    candidates = []
    if (root / platform_ci_script_relative()).is_file():
        candidates.append(shlex.join(default_ci_command()))
    targets, scripts = _make_targets(root), _npm_scripts(root)
    for name in ("ci", "test"):
        if name in targets:
            candidates.append(f"make {name}")
        if name in scripts:
            candidates.append(f"npm run {name}")
    if _has_pytest(root):
        candidates.append("pytest")
    return candidates
