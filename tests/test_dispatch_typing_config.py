"""Keep the dispatch boundary's module-scoped mypy checks enabled."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

STRICT_MODULES = (
    "orchestune.dispatch.cycle",
    "orchestune.dispatch.cycle_actions",
    "orchestune.dispatch.cycle_state_changes",
)


def _matches(pattern: str, module: str) -> bool:
    # Mypy's structured trailing wildcard includes the module itself; an
    # interior wildcard matches zero or more complete module components.
    parts = pattern.split(".")
    expression = re.escape(parts[0]) if parts[0] != "*" else r"[^.]+(?:\.[^.]+)*"
    for part in parts[1:]:
        expression += r"(?:\.[^.]+)*" if part == "*" else r"\." + re.escape(part)
    return re.fullmatch(expression, module) is not None


def test_global_mypy_policy_remains_incremental() -> None:
    with (Path(__file__).parents[1] / "pyproject.toml").open("rb") as stream:
        config = tomllib.load(stream)["tool"]["mypy"]
    assert config["disallow_untyped_defs"] is False


@pytest.mark.parametrize("module", STRICT_MODULES)
def test_dispatch_boundary_has_no_relaxed_matching_override(module: str) -> None:
    with (Path(__file__).parents[1] / "pyproject.toml").open("rb") as stream:
        config = tomllib.load(stream)["tool"]["mypy"]
    matching = []
    for override in config.get("overrides", []):
        patterns = override["module"]
        if isinstance(patterns, str):
            patterns = [patterns]
        if any(_matches(pattern, module) for pattern in patterns):
            matching.append(override)
    assert matching, f"No strict override for {module}"
    for flag in ("disallow_untyped_defs", "disallow_incomplete_defs"):
        assert any(override.get(flag) is True for override in matching)
        assert all(override.get(flag) is not False for override in matching)
    for options in [config, *matching]:
        assert not options.get("ignore_errors", False)
        assert not options.get("no_type_check", False)
        assert options.get("follow_imports", "normal") not in {"skip", "silent"}
        assert not options.get("disable_error_code", [])
        assert options.get("check_untyped_defs", True) is not False
