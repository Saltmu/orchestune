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
VALID_MODEL_TIERS = frozenset({"weak", "middle", "strong"})

DEFAULT_MODEL_TIERS: dict[str, dict[str, str]] = {
    "strong": {
        "claude": "claude-3-7-sonnet",
        "claude-cli": "claude-3-7-sonnet",
        "codex": "o3-mini",
        "codex-cli": "o3-mini",
        "agy": "gemini-2.5-pro",
        "agy-cli": "gemini-2.5-pro",
        "cloud-routine": "claude-3-7-sonnet",
        "codex-cloud": "o3-mini",
    },
    "middle": {
        "claude": "claude-3-5-sonnet",
        "claude-cli": "claude-3-5-sonnet",
        "codex": "gpt-4o",
        "codex-cli": "gpt-4o",
        "agy": "gemini-2.5-flash",
        "agy-cli": "gemini-2.5-flash",
        "cloud-routine": "claude-3-5-sonnet",
        "codex-cloud": "gpt-4o",
    },
    "weak": {
        "claude": "claude-3-5-haiku",
        "claude-cli": "claude-3-5-haiku",
        "codex": "gpt-4o-mini",
        "codex-cli": "gpt-4o-mini",
        "agy": "gemini-2.5-flash-lite",
        "agy-cli": "gemini-2.5-flash-lite",
        "cloud-routine": "claude-3-5-haiku",
        "codex-cloud": "gpt-4o-mini",
    },
}

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


def validate_model_tier(tier: str) -> str:
    """Validate that a model tier name is one of the supported tiers."""
    if (
        not isinstance(tier, str)
        or isinstance(tier, bool)
        or tier.strip().lower() not in VALID_MODEL_TIERS
    ):
        raise ConfigError(
            f"invalid model tier {tier!r}: must be one of {sorted(VALID_MODEL_TIERS)}"
        )
    return tier.strip().lower()


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
    """Repository-level configuration of execution profiles and model tiers."""

    default_execution_profile: str = DEFAULT_EXECUTION_PROFILE
    profiles: dict[str, dict[str, TargetExecutionConfig]] = field(default_factory=dict)
    model_tiers: dict[str, dict[str, str]] = field(default_factory=dict)

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


def _get_aliased_value(table: dict[str, Any], key_underscore: str) -> Any:
    """Retrieve value for key using either underscore or hyphen notation."""
    if key_underscore in table:
        return table[key_underscore]
    key_hyphen = key_underscore.replace("_", "-")
    return table.get(key_hyphen)


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

    raw_model = _get_aliased_value(target_settings, "model")
    model_val: str | None = None
    if raw_model is not None:
        if not isinstance(raw_model, str) or isinstance(raw_model, bool):
            raise ConfigError(
                f"'model' in profile {profile_name!r}, target {target_name!r} must be a string"
            )
        model_val = validate_model_name(raw_model)

    raw_effort = _get_aliased_value(target_settings, "reasoning_effort")
    effort_val: str | None = None
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
        normalized_target_name = validated_target_name.replace("_", "-")
        if normalized_target_name in parsed_targets:
            raise ConfigError(
                f"duplicate target definition {target_name!r} (normalized to {normalized_target_name!r}) in profile {profile_name!r}"
            )
        parsed_targets[normalized_target_name] = _parse_target_execution_config(
            profile_name, normalized_target_name, target_settings
        )
    return parsed_targets


def _parse_model_tiers(raw_model_tiers: Any) -> dict[str, dict[str, str]]:
    if not isinstance(raw_model_tiers, dict):
        raise ConfigError("'model_tiers' (or 'model-tiers') must be a table (dict)")
    parsed: dict[str, dict[str, str]] = {}
    for tier_name, targets_map in raw_model_tiers.items():
        validated_tier = validate_model_tier(tier_name)
        if not isinstance(targets_map, dict):
            raise ConfigError(
                f"tier {tier_name!r} under 'model_tiers' must be a table (dict)"
            )
        parsed_targets: dict[str, str] = {}
        for target_name, model_val in targets_map.items():
            validated_target = validate_target_name(target_name)
            normalized_target = validated_target.replace("_", "-")
            if not isinstance(model_val, str) or isinstance(model_val, bool):
                raise ConfigError(
                    f"model in tier {tier_name!r}, target {target_name!r} must be a string"
                )
            validated_model = validate_model_name(model_val)
            parsed_targets[normalized_target] = validated_model
        parsed[validated_tier] = parsed_targets
    return parsed


def extract_execution_profile_config(
    config_data: dict[str, Any],
) -> ExecutionProfileConfig:
    """Extract and validate execution profiles configuration from a loaded TOML dict."""
    default_profile = _get_aliased_value(config_data, "default_execution_profile")
    if default_profile is not None:
        if not isinstance(default_profile, str) or isinstance(default_profile, bool):
            raise ConfigError(
                "'default_execution_profile' (or 'default-execution-profile') must be a string"
            )
        default_profile = validate_profile_name(default_profile)
    else:
        default_profile = DEFAULT_EXECUTION_PROFILE

    raw_model_tiers = _get_aliased_value(config_data, "model_tiers")
    parsed_model_tiers: dict[str, dict[str, str]] = {}
    if raw_model_tiers is not None:
        parsed_model_tiers = _parse_model_tiers(raw_model_tiers)

    raw_profiles = _get_aliased_value(config_data, "execution_profiles")
    if raw_profiles is None:
        return ExecutionProfileConfig(
            default_execution_profile=default_profile,
            profiles={},
            model_tiers=parsed_model_tiers,
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

    if parsed_profiles and default_profile not in parsed_profiles:
        raise ConfigError(
            f"default_execution_profile {default_profile!r} is not defined under 'execution_profiles'"
        )

    return ExecutionProfileConfig(
        default_execution_profile=default_profile,
        profiles=parsed_profiles,
        model_tiers=parsed_model_tiers,
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

    explicit_name = getattr(
        target,
        "target_name",
        getattr(target, "name", getattr(target, "_target_name", None)),
    )
    if isinstance(explicit_name, str) and explicit_name.strip():
        return explicit_name.strip().lower().replace("_", "-")

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
    return list(_TARGET_ALIASES.get(target_name, (target_name,)))


def _normalize_profile_config(
    config: ExecutionProfileConfig | Any | None,
) -> ExecutionProfileConfig:
    if config is None:
        return ExecutionProfileConfig()
    if isinstance(config, ExecutionProfileConfig):
        return config
    cfg = getattr(config, "execution_profile_config", None)
    return cfg if isinstance(cfg, ExecutionProfileConfig) else ExecutionProfileConfig()


def _select_profile_and_reason(
    profile: str | None, target_name: str, config: ExecutionProfileConfig
) -> tuple[str, str]:
    default_profile = config.default_execution_profile
    if not profile:
        if config.profiles and default_profile not in config.profiles:
            logger.warning(
                "Default execution profile %r is not configured in profiles; proceeding with empty profile settings",
                default_profile,
            )
        return (
            default_profile,
            f"default profile '{default_profile}' applied (no profile specified)",
        )
    if profile in config.profiles:
        return profile, f"profile '{profile}' resolved for target '{target_name}'"
    logger.warning(
        "Unknown execution profile %r specified; falling back to default profile %r",
        profile,
        default_profile,
    )
    return (
        default_profile,
        f"unknown profile '{profile}', fell back to default profile '{default_profile}'",
    )


def _resolve_tier_model(
    model_tier: str | None,
    target_name: str,
    profile_config: ExecutionProfileConfig,
    current_reason: str,
) -> tuple[str | None, str]:
    if model_tier is None:
        return None, current_reason
    clean_tier = (
        model_tier.strip().lower()
        if isinstance(model_tier, str) and not isinstance(model_tier, bool)
        else ""
    )
    if clean_tier not in VALID_MODEL_TIERS:
        logger.warning(
            "Unknown model_tier %r specified; falling back to profile model settings",
            model_tier,
        )
        return None, current_reason

    configured_tier = profile_config.model_tiers.get(clean_tier, {})
    for candidate in _target_lookup_candidates(target_name):
        if candidate in configured_tier:
            tier_model = configured_tier[candidate]
            return (
                tier_model,
                f"model_tier '{clean_tier}' resolved to model '{tier_model}' for target '{target_name}'; {current_reason}",
            )

    builtin_tier = DEFAULT_MODEL_TIERS.get(clean_tier, {})
    for candidate in _target_lookup_candidates(target_name):
        if candidate in builtin_tier:
            tier_model = builtin_tier[candidate]
            return (
                tier_model,
                f"model_tier '{clean_tier}' resolved to model '{tier_model}' for target '{target_name}'; {current_reason}",
            )

    return None, current_reason


def resolve_execution_profile(
    profile: str | None,
    target: str | Any,
    config: ExecutionProfileConfig | Any | None = None,
    *,
    model_tier: str | None = None,
) -> ExecutionSelection:
    """Deterministically resolve an execution profile, model tier, and target into model/reasoning settings.

    If `model_tier` is specified, maps the tier (weak/middle/strong) to a concrete model name
    from `model_tiers` config (or built-in defaults), overriding the model configured in the profile.
    If `profile` is unspecified or empty, uses `default_execution_profile`.
    If `profile` is not defined in `config`, falls back to `default_execution_profile`
    with a warning log.
    """
    target_name = _extract_target_name(target)
    profile_config = _normalize_profile_config(config)
    selected_profile, reason = _select_profile_and_reason(
        profile, target_name, profile_config
    )

    profile_targets = profile_config.profiles.get(selected_profile, {})
    matched_target_config: TargetExecutionConfig | None = None
    for candidate in _target_lookup_candidates(target_name):
        if candidate in profile_targets:
            matched_target_config = profile_targets[candidate]
            break

    model = matched_target_config.model if matched_target_config else None
    reasoning_effort = (
        matched_target_config.reasoning_effort if matched_target_config else None
    )

    tier_model, reason = _resolve_tier_model(
        model_tier, target_name, profile_config, reason
    )
    if tier_model is not None:
        model = tier_model

    return ExecutionSelection(
        profile=selected_profile,
        model=model,
        reasoning_effort=reasoning_effort,
        reason=reason,
    )
