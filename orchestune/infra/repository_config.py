"""Shared repository configuration file discovery without workflow dependencies."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import orchestune.config_contracts as config_contracts


def resolve_repo_root(plan_path: str | Path) -> Path:
    """Locate the Git repository root a plan file lives under.

    Walks upward from `plan_path`'s parent directory looking for a `.git`
    marker (a directory in a normal checkout, or a file pointing at the
    real gitdir in a linked worktree). Outside a Git repository, falls back
    to the plan's own parent directory to preserve pre-#410 behavior.

    Shared by orchestune-dag (dag_cli.py) and orchestune-provision
    (provisioning.py) so that both tools agree on which directory's
    orchestune.toml / pyproject.toml `[tool.orchestune]` table to read when
    `--plan` points to a file nested below the repository root (#410, #418).
    """
    plan_parent = Path(plan_path).resolve().parent
    for candidate in (plan_parent, *plan_parent.parents):
        git_marker = candidate / ".git"
        if git_marker.is_file() or (git_marker / "HEAD").is_file():
            return candidate
    return plan_parent


def find_and_load_config_file(checkout_root: str | Path) -> dict[str, Any]:
    """Search and load configuration from orchestune.toml or pyproject.toml.

    Checks `<checkout_root>/orchestune.toml` first (its entire top level is the
    config table); falls back to `<checkout_root>/pyproject.toml`'s
    `[tool.orchestune]` table. Missing files return an empty dict.

    Shared by orchestune-dag, orchestune-dispatch and orchestune-provision so
    all agree on the same discovery order and error semantics (#404 review).
    """
    checkout_root = Path(checkout_root)

    # Beyond `OSError`/`tomllib.TOMLDecodeError`, a pathological file can
    # make `open()`/`tomllib.load()` fail in other ways too — invalid UTF-8
    # raises `UnicodeDecodeError`, ~500+ levels of nested arrays/tables
    # raise `RecursionError` (Codex review, #441). `tomllib.load` is the
    # only risky operation in each block below, so any failure loading
    # user-supplied config data is a config problem, not an internal bug —
    # catch broadly rather than chase the parser's exception types one by
    # one (same reasoning as `compile_extra_ignore_patterns`).
    orchestune_toml = checkout_root / "orchestune.toml"
    if orchestune_toml.exists():
        try:
            with open(orchestune_toml, "rb") as f:
                return tomllib.load(f)
        except Exception as e:
            raise config_contracts.ConfigError(
                f"failed to load {orchestune_toml}: {e}"
            ) from e

    pyproject_toml = checkout_root / "pyproject.toml"
    if pyproject_toml.exists():
        try:
            with open(pyproject_toml, "rb") as f:
                data = tomllib.load(f)
        except Exception as e:
            raise config_contracts.ConfigError(
                f"failed to load {pyproject_toml}: {e}"
            ) from e
        tool = data.get("tool", {})
        if not isinstance(tool, dict):
            raise config_contracts.ConfigError(
                f"{pyproject_toml}: [tool] must be a table"
            )
        config = tool.get("orchestune", {})
        if not isinstance(config, dict):
            raise config_contracts.ConfigError(
                f"{pyproject_toml}: [tool.orchestune] must be a table"
            )
        return config

    return {}
