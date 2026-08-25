"""Execution profiles configuration and deterministic selector.

Defines the configuration schema for repository-level execution profiles
([tool.orchestune.execution_profiles.<profile>.<target>]) and the pure selector
logic that deterministically maps an abstract profile name and dispatch target
to a concrete model and reasoning effort.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestune.dag.models import ConfigError, load_orchestune_config

logger = logging.getLogger(__name__)

DEFAULT_EXECUTION_PROFILE = "balanced"

# Profile name: alphanumeric, hyphen, underscore, 1..64 chars, cannot start with hyphen
_PROFILE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_-]{0,63}$")

# Model name: safe chars, no leading hyphen, no shell injection characters
# Examples: claude-3-7-sonnet-20250219, o3-mini, gpt-4o, anthropic/claude-3.5-sonnet:beta
_MODEL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}$")

# Reasoning effort: e.g. low, medium, high, none, off, min, max, budget_2048
_REASONING_EFFORT_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$")

# Target name: identifier for dispatch target (e.g. claude-cli, codex-cli, cloud-routine)
_TARGET_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_-]{0,63}$")

_ALLOWED_TARGET_KEYS = frozenset({"model", "reasoning_effort", "reasoning-effort"})


def validate_profile_name(name: str) -> str:
    """Validate that a profile name is non-empty, safe, and matches pattern."""
    if not isinstance(name, str) or not _PROFILE_NAME_PATTERN.fullmatch(name):
        raise ConfigError(
            f"invalid execution profile name {name!r}: "
            "must match pattern ^[a-zA-Z0-9_][a-zA-Z0-9_-]{0,63}$ and not start with '-'"
        )
    return name


def validate_model_name(model: str) -> str:
    """Validate that a model name is non-empty, safe, and matches pattern."""
    if not isinstance(model, str) or not _MODEL_NAME_PATTERN.fullmatch(model):
        raise ConfigError(
            f"invalid model name {model!r}: "
            "must match pattern ^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}$ and not start with '-'"
        )
    return model


def validate_reasoning_effort(effort: str) -> str:
    """Validate that a reasoning effort string is non-empty, safe, and matches pattern."""
    if not isinstance(effort, str) or not _REASONING_EFFORT_PATTERN.fullmatch(effort):
        raise ConfigError(
            f"invalid reasoning_effort {effort!r}: "
            "must match pattern ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$ and not start with '-'"
        )
    return effort


def validate_target_name(target_name: str) -> str:
    """Validate that a target name is non-empty and matches pattern."""
    if not isinstance(target_name, str) or not _TARGET_NAME_PATTERN.fullmatch(
        target_name
    ):
        raise ConfigError(
            f"invalid target name {target_name!r}: "
            "must match pattern ^[a-zA-Z0-9_][a-zA-Z0-9_-]{0,63}$ and not start with '-'"
        )
    return target_name


@dataclass(frozen=True)
class TargetExecutionConfig:
    """Target-specific model and reasoning effort settings within an execution profile."""

    model: str | None = None
    reasoning_effort: str | None = None


TargetProfileConfig = TargetExecutionConfig


@dataclass(frozen=True)
class ExecutionProfileConfig:
    """Repository-level configuration of execution profiles."""

    default_execution_profile: str = DEFAULT_EXECUTION_PROFILE
    profiles: dict[str, dict[str, TargetExecutionConfig]] = field(default_factory=dict)

    @property
    def default_profile(self) -> str:
        """Alias for default_execution_profile."""
        return self.default_execution_profile


@dataclass(frozen=True)
class ExecutionSelection:
    """Deterministic selection result for a task execution profile."""

    profile: str
    model: str | None
    reasoning_effort: str | None
    reason: str


def _parse_target_execution_config(
    profile_name: str, target_name: str, target_settings: Any
) -> TargetExecutionConfig:
    if not isinstance(target_settings, dict):
        raise ConfigError(
            f"target {target_name!r} under profile {profile_name!r} must be a table (dict)"
        )
    for key in target_settings:
        if key not in _ALLOWED_TARGET_KEYS:
            raise ConfigError(
                f"unknown key {key!r} in target {target_name!r} of profile {profile_name!r}"
            )

    model_val: str | None = None
    if "model" in target_settings:
        raw_model = target_settings["model"]
        if not isinstance(raw_model, str) or isinstance(raw_model, bool):
            raise ConfigError(
                f"'model' in profile {profile_name!r}, target {target_name!r} must be a string"
            )
        model_val = validate_model_name(raw_model)

    effort_val: str | None = None
    raw_effort = target_settings.get("reasoning_effort")
    if raw_effort is None:
        raw_effort = target_settings.get("reasoning-effort")
    if raw_effort is not None:
        if not isinstance(raw_effort, str) or isinstance(raw_effort, bool):
            raise ConfigError(
                f"'reasoning_effort' in profile {profile_name!r}, target {target_name!r} must be a string"
            )
        effort_val = validate_reasoning_effort(raw_effort)

    return TargetExecutionConfig(model=model_val, reasoning_effort=effort_val)


def _parse_profile_targets(
    profile_name: str, targets_map: Any
) -> dict[str, TargetExecutionConfig]:
    if not isinstance(targets_map, dict):
        raise ConfigError(
            f"profile {profile_name!r} under 'execution_profiles' must be a table (dict)"
        )
    parsed_targets: dict[str, TargetExecutionConfig] = {}
    for target_name, target_settings in targets_map.items():
        validated_target_name = validate_target_name(target_name)
        parsed_targets[validated_target_name] = _parse_target_execution_config(
            profile_name, validated_target_name, target_settings
        )
    return parsed_targets


def extract_execution_profile_config(
    config_data: dict[str, Any],
) -> ExecutionProfileConfig:
    """Extract and validate execution profiles configuration from a loaded TOML dict."""
    default_profile = config_data.get("default_execution_profile")
    if default_profile is None:
        default_profile = config_data.get("default-execution-profile")
    if default_profile is not None:
        if not isinstance(default_profile, str) or isinstance(default_profile, bool):
            raise ConfigError(
                "'default_execution_profile' (or 'default-execution-profile') must be a string"
            )
        default_profile = validate_profile_name(default_profile)
    else:
        default_profile = DEFAULT_EXECUTION_PROFILE

    raw_profiles = config_data.get("execution_profiles")
    if raw_profiles is None:
        raw_profiles = config_data.get("execution-profiles")

    if raw_profiles is None:
        return ExecutionProfileConfig(
            default_execution_profile=default_profile, profiles={}
        )

    if not isinstance(raw_profiles, dict):
        raise ConfigError(
            "'execution_profiles' (or 'execution-profiles') must be a table (dict)"
        )

    parsed_profiles: dict[str, dict[str, TargetExecutionConfig]] = {}
    for profile_name, targets_map in raw_profiles.items():
        validated_profile_name = validate_profile_name(profile_name)
        parsed_profiles[validated_profile_name] = _parse_profile_targets(
            validated_profile_name, targets_map
        )

    return ExecutionProfileConfig(
        default_execution_profile=default_profile,
        profiles=parsed_profiles,
    )


def load_execution_profile_config(
    repo_root_or_config: dict[str, Any] | str | Path,
) -> ExecutionProfileConfig:
    """Load and validate execution profiles configuration from a repo root or config dict."""
    if isinstance(repo_root_or_config, dict):
        return extract_execution_profile_config(repo_root_or_config)
    config_data = load_orchestune_config(repo_root_or_config)
    return extract_execution_profile_config(config_data)


def _extract_target_name(target: str | Any) -> str:
    if isinstance(target, str):
        return target.strip().lower().replace("_", "-")

    type_name = type(target).__name__
    if type_name == "ClaudeCodeCloudRoutineDispatchTarget":
        return "cloud-routine"
    if type_name == "CodexCloudDispatchTarget":
        return "codex-cloud"
    if type_name == "LocalProcessDispatchTarget":
        local_cmd = getattr(target, "_local_cmd", getattr(target, "local_cmd", None))
        if isinstance(local_cmd, str):
            cmd_lower = local_cmd.lower()
            if "claude" in cmd_lower:
                return "claude-cli"
            if "agy" in cmd_lower:
                return "agy-cli"
            if "codex" in cmd_lower:
                return "codex-cli"
        return "local"
    return "local"


_TARGET_ALIASES: dict[str, tuple[str, ...]] = {
    "claude": ("claude", "claude-cli"),
    "claude-cli": ("claude-cli", "claude"),
    "agy": ("agy", "agy-cli"),
    "agy-cli": ("agy-cli", "agy"),
    "codex": ("codex", "codex-cli"),
    "codex-cli": ("codex-cli", "codex"),
    "cloud-routine": ("cloud-routine", "cloud_routine"),
    "cloud_routine": ("cloud_routine", "cloud-routine"),
    "codex-cloud": ("codex-cloud", "codex_cloud"),
    "codex_cloud": ("codex_cloud", "codex-cloud"),
}


def _target_lookup_candidates(target_name: str) -> list[str]:
    norm = target_name.strip().lower().replace("_", "-")
    return list(_TARGET_ALIASES.get(norm, (norm,)))


def resolve_execution_profile(
    profile: str | None,
    target: str | Any,
    config: ExecutionProfileConfig | Any | None = None,
) -> ExecutionSelection:
    """Deterministically resolve an execution profile and target into model/reasoning settings.

    If `profile` is unspecified or empty, uses `default_execution_profile`.
    If `profile` is not defined in `config`, falls back to `default_execution_profile`
    with a warning log.
    """
    target_name = _extract_target_name(target)
    lookup_order = _target_lookup_candidates(target_name)

    if config is None:
        profile_config = ExecutionProfileConfig()
    elif isinstance(config, ExecutionProfileConfig):
        profile_config = config
    else:
        profile_config = getattr(
            config, "execution_profile_config", ExecutionProfileConfig()
        )
        if profile_config is None:
            profile_config = ExecutionProfileConfig()

    default_profile = profile_config.default_execution_profile

    if not profile:
        selected_profile = default_profile
        reason = f"default profile '{default_profile}' applied (no profile specified)"
    elif profile in profile_config.profiles:
        selected_profile = profile
        reason = f"profile '{profile}' resolved for target '{target_name}'"
    else:
        logger.warning(
            "Unknown execution profile %r specified; falling back to default profile %r",
            profile,
            default_profile,
        )
        selected_profile = default_profile
        reason = f"unknown profile '{profile}', fell back to default profile '{default_profile}'"

    profile_targets = profile_config.profiles.get(selected_profile, {})
    matched_target_config: TargetExecutionConfig | None = None
    for candidate in lookup_order:
        if candidate in profile_targets:
            matched_target_config = profile_targets[candidate]
            break

    if matched_target_config is not None:
        model = matched_target_config.model
        reasoning_effort = matched_target_config.reasoning_effort
    else:
        model = None
        reasoning_effort = None

    return ExecutionSelection(
        profile=selected_profile,
        model=model,
        reasoning_effort=reasoning_effort,
        reason=reason,
    )
