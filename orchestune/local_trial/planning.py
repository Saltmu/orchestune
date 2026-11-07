"""Planning helpers and prompt construction for local trial (#1734, #1737)."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestune.config_contracts import ConfigError
from orchestune.infra.repository_config import find_and_load_config_file
from orchestune.local_trial.contracts import TrialError, plan_digest, request_digest
from orchestune.local_trial.models import TrialPlan


def _validate_toml_config(raw_config: dict[str, Any]) -> dict[str, Any]:
    mod = importlib.import_module("orchestune.config_schema")
    fn: Callable[[dict[str, Any]], dict[str, Any]] = mod.validate_toml_config
    return fn(raw_config)


def _parse_ci_command(value: Any) -> list[str]:
    mod = importlib.import_module("orchestune.config_schema")
    fn: Callable[[Any], list[str]] = mod.parse_ci_command
    return fn(value)


def _suggest_ci_commands(root: Path) -> list[str]:
    mod = importlib.import_module("orchestune.config_wizard.ci_candidates")
    fn: Callable[[Path], list[str]] = mod.suggest_ci_commands
    return fn(root)


def resolve_ci_selection(
    primary_root: str,
    override: tuple[str, ...] | None = None,
) -> tuple[tuple[str, ...] | None, tuple[tuple[str, ...], ...]]:
    """Resolve preferred CI command and suggested candidates from config/root."""
    primary = Path(primary_root)
    try:
        raw_config = find_and_load_config_file(primary)
        config = _validate_toml_config(raw_config) if raw_config else {}
    except ConfigError as exc:
        raise TrialError(
            "config_invalid", f"Invalid repository configuration: {exc}"
        ) from exc
    except Exception as exc:
        raise TrialError(
            "config_invalid", f"Failed to load repository configuration: {exc}"
        ) from exc

    if override is not None:
        return (tuple(override), ())

    if "ci_command" in config:
        try:
            return (tuple(_parse_ci_command(config["ci_command"])), ())
        except ConfigError as exc:
            raise TrialError(
                "config_invalid", f"Invalid ci_command in configuration: {exc}"
            ) from exc

    cand_list: list[tuple[str, ...]] = []
    for raw in _suggest_ci_commands(primary):
        try:
            cand_list.append(tuple(_parse_ci_command(raw)))
        except ConfigError:
            pass
    return (None, tuple(cand_list))


def _validate_plan_paths(plan: TrialPlan, repo_root: Path) -> None:
    if not plan.write_paths:
        raise TrialError("artifact_invalid", "Plan write_paths cannot be empty")
    if len(set(plan.write_paths)) != len(plan.write_paths):
        raise TrialError("artifact_invalid", "Plan write_paths contains duplicates")

    root = repo_root.resolve()
    for p in plan.write_paths:
        if not p or not p.strip():
            raise TrialError("artifact_invalid", "Empty write_path")
        if p.startswith(("/", "\\")) or Path(p).is_absolute():
            raise TrialError("artifact_invalid", f"write_path must be relative: {p}")
        if any(ch in p for ch in "*?[]"):
            raise TrialError(
                "artifact_invalid", f"write_path cannot contain globs: {p}"
            )
        if p.endswith(("/", "\\")):
            raise TrialError("artifact_invalid", f"write_path cannot be directory: {p}")
        parts = Path(p).parts
        if ".." in parts:
            raise TrialError(
                "artifact_invalid",
                f"write_path cannot traverse parent directories: {p}",
            )
        if parts and parts[0] in (".git", ".orchestune"):
            raise TrialError(
                "artifact_invalid", f"write_path in forbidden directory: {p}"
            )
        target = (root / p).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise TrialError(
                "artifact_invalid", f"write_path escapes repository root: {p}"
            ) from None
        if (root / p).is_dir():
            raise TrialError(
                "artifact_invalid", f"write_path cannot be an existing directory: {p}"
            )


def _validate_discipline_paths(plan: TrialPlan, repo_root: Path) -> None:
    root = repo_root.resolve()
    for dp in plan.discipline_paths:
        if not dp or not dp.strip() or Path(dp).is_absolute() or ".." in Path(dp).parts:
            raise TrialError("artifact_invalid", f"Invalid discipline_path: {dp}")
        try:
            (root / dp).resolve().relative_to(root)
        except ValueError:
            raise TrialError(
                "artifact_invalid", f"discipline_path escapes repo root: {dp}"
            ) from None


def validate_plan(
    plan: TrialPlan,
    repo_root: str | Path,
    request_text: str,
    base_sha: str,
    preferred_ci: tuple[str, ...] | None = None,
) -> str:
    """Validate plan constraints and compute plan digest."""
    if not plan.goal or not plan.goal.strip():
        raise TrialError("artifact_invalid", "Plan goal cannot be empty")
    if not plan.acceptance_criteria:
        raise TrialError("artifact_invalid", "Plan acceptance criteria cannot be empty")
    for ac in plan.acceptance_criteria:
        if not ac or not ac.strip():
            raise TrialError("artifact_invalid", "Acceptance criterion cannot be empty")

    root = Path(repo_root)
    _validate_plan_paths(plan, root)
    _validate_discipline_paths(plan, root)

    if preferred_ci is not None:
        if plan.ci_argv is None or tuple(plan.ci_argv) != preferred_ci:
            raise TrialError(
                "artifact_invalid",
                f"Plan ci_argv {plan.ci_argv} does not match preferred CI command {preferred_ci}",
            )

    return plan_digest(plan, request_digest(request_text), base_sha)
