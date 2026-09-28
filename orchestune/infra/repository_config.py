"""Shared repository configuration file discovery without workflow dependencies."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from orchestune.dag.models import ConfigError


def find_and_load_config_file(checkout_root: Path) -> dict[str, Any]:
    """Search and load configuration from orchestune.toml or pyproject.toml."""
    orchestune_toml = checkout_root / "orchestune.toml"
    if orchestune_toml.exists():
        try:
            with open(orchestune_toml, "rb") as f:
                return tomllib.load(f)
        except Exception as e:
            raise ConfigError(f"failed to load {orchestune_toml}: {e}") from e

    pyproject_toml = checkout_root / "pyproject.toml"
    if pyproject_toml.exists():
        try:
            with open(pyproject_toml, "rb") as f:
                data = tomllib.load(f)
        except Exception as e:
            raise ConfigError(f"failed to load {pyproject_toml}: {e}") from e
        tool = data.get("tool", {})
        if not isinstance(tool, dict):
            raise ConfigError(f"{pyproject_toml}: [tool] must be a table")
        config = tool.get("orchestune", {})
        if not isinstance(config, dict):
            raise ConfigError(f"{pyproject_toml}: [tool.orchestune] must be a table")
        return config

    return {}
