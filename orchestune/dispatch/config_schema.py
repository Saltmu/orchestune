"""TOML schema validation for the dispatcher configuration table."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import orchestune.config_contracts as config_contracts
from orchestune.consistency.supervisor import MAX_REPAIR_PASSES
from orchestune.dag.models import (
    DAG_TOOL_CONFIG_KEYS,
    compile_extra_ignore_patterns,
    extract_dag_ignore_patterns,
    extract_dag_similarity_threshold,
)
from orchestune.dispatch.config import VALID_FINAL_ACCEPTANCE_REVIEW_MODES
from orchestune.dispatch.execution_profiles import extract_execution_profile_config
from orchestune.outcome_record import VALID_CHILD_REVIEW_GATE_MODES
from orchestune.review.rounds import MAX_REVIEW_ROUNDS
from orchestune.targets.cloud_routine import ROUTINE_TOKEN_ENV_VAR

PROHIBITED_TOML_KEYS: dict[str, str] = {
    "model_tiers": config_contracts.retired_model_tiers_message("model_tiers"),
    "model-tiers": config_contracts.retired_model_tiers_message("model-tiers"),
    "routine_token": (
        "setting 'routine_token' is prohibited in configuration files; "
        f"set the {ROUTINE_TOKEN_ENV_VAR} environment variable instead"
    ),
    "routine-token": (
        "setting 'routine-token' is prohibited in configuration files; "
        f"set the {ROUTINE_TOKEN_ENV_VAR} environment variable instead"
    ),
    "allow_unsafe_agent_execution": (
        "setting 'allow_unsafe_agent_execution' is prohibited in configuration files; "
        "use the --allow-unsafe-agent-execution CLI flag"
    ),
    "allow-unsafe-agent-execution": (
        "setting 'allow-unsafe-agent-execution' is prohibited in configuration files; "
        "use the --allow-unsafe-agent-execution CLI flag"
    ),
    "parent_issue": (
        "parent issue cannot be set in configuration files; "
        "pass -p / --parent-issue on the CLI or infer from a parent/issue-<N> branch"
    ),
    "parent-issue": (
        "parent issue cannot be set in configuration files; "
        "pass -p / --parent-issue on the CLI or infer from a parent/issue-<N> branch"
    ),
    "parent_issue_number": (
        "parent issue cannot be set in configuration files; "
        "pass -p / --parent-issue on the CLI or infer from a parent/issue-<N> branch"
    ),
    "parent-issue-number": (
        "parent issue cannot be set in configuration files; "
        "pass -p / --parent-issue on the CLI or infer from a parent/issue-<N> branch"
    ),
    "model": (
        "top-level 'model' is deprecated and prohibited in configuration files; "
        "define models inside [execution_profiles.<name>.<target>]"
    ),
    "reasoning_effort": (
        "top-level 'reasoning_effort' is deprecated and prohibited in configuration files; "
        "define reasoning_effort inside [execution_profiles.<name>.<target>]"
    ),
    "reasoning-effort": (
        "top-level 'reasoning-effort' is deprecated and prohibited in configuration files; "
        "define reasoning_effort inside [execution_profiles.<name>.<target>]"
    ),
    "effort": (
        "top-level 'effort' is deprecated and prohibited in configuration files; "
        "define reasoning_effort inside [execution_profiles.<name>.<target>]"
    ),
}

PATH_CONFIG_KEYS = frozenset(
    {
        "run_state_path",
        "worktree_root",
        "log_dir",
        "events_log_path",
        "not_needed_review_state_path",
        "report_dir",
    }
)

NON_NEGATIVE_INT_KEYS = frozenset(
    {
        "max_concurrent",
        "max_launches_per_window",
        "deviation_buffer_lines",
        "max_recompute_retries",
        "task_timeout_seconds",
        "max_task_reclaims",
        "early_death_window_seconds",
        "max_early_death_retries",
        "early_death_backoff_seconds",
        "max_usage_limit_retries",
        "usage_limit_reset_grace_seconds",
        "not_needed_review_timeout_seconds",
        "max_tokens_per_window",
        "max_tokens_per_task",
        # #820: zero retries means "the first timeout is terminal".
        "max_integration_timeout_retries",
        "max_semantic_fix_retries",
        # #1436: zero retries the unanswered review request immediately.
        "semantic_review_retry_backoff_seconds",
    }
)

# #820: seconds settings never accept 0, so the Integrator cannot be made unbounded.
POSITIVE_INT_KEYS = frozenset(
    {
        "window_seconds",
        "integration_dependency_timeout_seconds",
        "integration_ci_timeout_seconds",
        "integration_cycle_timeout_seconds",
        "integration_cleanup_timeout_seconds",
        "integration_command_timeout_seconds",
        "integration_timeout_backoff_seconds",
        # #1270: zero would retry a session-limit exit immediately.
        "usage_limit_backoff_seconds",
        "max_semantic_review_rounds",
        "semantic_review_timeout_seconds",
        "semantic_review_ack_timeout_seconds",
        "semantic_fix_timeout_seconds",
        "semantic_ci_timeout_seconds",
        "semantic_attempt_timeout_seconds",
    }
)

BOOLEAN_CONFIG_KEYS = frozenset(
    {"apply", "zombie_gc", "force_integration_review", "semantic_review_auto_retry"}
)

STRING_KEYS = frozenset(
    {"local_cmd", "routine_id", "codex_cloud_env", "default_execution_profile"}
)

_TARGET_CHOICES = frozenset(
    {
        "local",
        "cloud-routine",
        "codex-cloud",
        "claude-cli",
        "agy-cli",
        "codex-cli",
        "auto",
    }
)

_REVIEWER_BOT_CHOICES = frozenset({"auto", "claude", "codex"})

_CONSISTENCY_MODE_CHOICES = frozenset({"off", "shadow", "repair"})

_CHILD_REVIEW_GATE_CHOICES = VALID_CHILD_REVIEW_GATE_MODES

_SEMANTIC_REVIEW_MODE_CHOICES = frozenset({"required", "advisory", "off"})
_SEMANTIC_REVIEW_PROVIDER_CHOICES = frozenset({"auto", "claude", "codex"})
_SEMANTIC_FIX_PROVIDER_CHOICES = frozenset(
    {
        "auto",
        "claude-cli",
        "codex-cli",
        "agy-cli",
        "cloud-routine",
        "codex-cloud",
        "local",
    }
)
_SEMANTIC_REVIEW_TRIGGER_CHOICES = frozenset({"pr-comment", "dispatch-target"})

_EXECUTION_PROFILE_TABLE_KEYS = frozenset({"execution_profiles", "execution-profiles"})


def _normalize_key(key: str) -> str:
    return key.replace("-", "_")


def _validate_scalar_entry(normalized_key: str, raw_key: str, value: Any) -> Any:
    if normalized_key in BOOLEAN_CONFIG_KEYS:
        if not isinstance(value, bool):
            raise config_contracts.ConfigError(f"{raw_key!r} must be a boolean")
        return value
    if normalized_key in PATH_CONFIG_KEYS:
        if not isinstance(value, str | Path):
            raise config_contracts.ConfigError(f"{raw_key!r} must be a string path")
        return Path(value)
    if not isinstance(value, str):
        raise config_contracts.ConfigError(f"{raw_key!r} must be a string")
    return value


def _validate_numeric_entry(normalized_key: str, raw_key: str, value: Any) -> Any:
    if normalized_key == "consistency_max_repair_passes":
        if not isinstance(value, int) or isinstance(value, bool):
            raise config_contracts.ConfigError(f"{raw_key!r} must be an integer")
        if not 1 <= value <= MAX_REPAIR_PASSES:
            raise config_contracts.ConfigError(
                f"{raw_key!r} must be between 1 and {MAX_REPAIR_PASSES}"
            )
        return value
    if normalized_key in NON_NEGATIVE_INT_KEYS:
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise config_contracts.ConfigError(f"{raw_key!r} must be an integer")
        if value < 0:
            raise config_contracts.ConfigError(
                f"{raw_key!r} must be greater than or equal to 0"
            )
        return value
    if not isinstance(value, int) or isinstance(value, bool):
        raise config_contracts.ConfigError(f"{raw_key!r} must be an integer")
    if value < 1:
        raise config_contracts.ConfigError(
            f"{raw_key!r} must be greater than or equal to 1"
        )
    if normalized_key == "max_semantic_review_rounds" and value > MAX_REVIEW_ROUNDS:
        raise config_contracts.ConfigError(
            f"{raw_key!r} must be less than or equal to {MAX_REVIEW_ROUNDS}"
        )
    return value


def _validate_choice_entry(normalized_key: str, raw_key: str, value: Any) -> Any:
    choice_map = {
        "dispatch_target": _TARGET_CHOICES,
        "reviewer_bot": _REVIEWER_BOT_CHOICES,
        "consistency_mode": _CONSISTENCY_MODE_CHOICES,
        "child_review_gate": _CHILD_REVIEW_GATE_CHOICES,
        "semantic_review_mode": _SEMANTIC_REVIEW_MODE_CHOICES,
        "final_acceptance_review": VALID_FINAL_ACCEPTANCE_REVIEW_MODES,
        "semantic_review_provider": _SEMANTIC_REVIEW_PROVIDER_CHOICES,
        "semantic_fix_provider": _SEMANTIC_FIX_PROVIDER_CHOICES,
        "semantic_review_trigger": _SEMANTIC_REVIEW_TRIGGER_CHOICES,
    }
    allowed = choice_map[normalized_key]
    if not isinstance(value, str) or value not in allowed:
        choices = ", ".join(repr(c) for c in sorted(allowed))
        raise config_contracts.ConfigError(f"{raw_key!r} must be one of: {choices}")
    return value


def parse_ci_command(value: Any) -> list[str]:
    """Parse and validate CI command as a string or list of non-empty strings."""
    if isinstance(value, str):
        if not value.strip():
            raise config_contracts.ConfigError("ci-command must not be empty")
        try:
            parts = shlex.split(value)
        except ValueError as exc:
            raise config_contracts.ConfigError(f"invalid ci-command: {exc}") from exc
        if not parts:
            raise config_contracts.ConfigError("ci-command must not be empty")
        return parts
    if isinstance(value, list):
        if not value:
            raise config_contracts.ConfigError("ci-command must not be empty")
        for elem in value:
            if not isinstance(elem, str) or not elem.strip():
                raise config_contracts.ConfigError(
                    "ci-command list elements must be non-empty strings"
                )
        return list(value)
    raise config_contracts.ConfigError("ci-command must be a string or list of strings")


def _validate_compound_entry(normalized_key: str, raw_key: str, value: Any) -> Any:
    if normalized_key == "consistency_repair_code":
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise config_contracts.ConfigError(
                f"{raw_key!r} must be a list of non-empty strings"
            )
        return value
    if normalized_key == "ci_command":
        parse_ci_command(value)
        return value
    if raw_key in DAG_TOOL_CONFIG_KEYS:
        return value
    if (
        raw_key in _EXECUTION_PROFILE_TABLE_KEYS
        or normalized_key in _EXECUTION_PROFILE_TABLE_KEYS
    ):
        if not isinstance(value, dict):
            raise config_contracts.ConfigError(f"{raw_key!r} must be a table")
        return value
    raise config_contracts.ConfigError(f"unknown key {raw_key!r}")


def _validate_timezone_entry(raw_key: str, value: Any) -> str:
    """#1270: an IANA name only; an invalid name must not silently become UTC."""
    if not isinstance(value, str) or not value:
        raise config_contracts.ConfigError(
            f"{raw_key!r} must be a non-empty IANA timezone name"
        )
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError) as error:
        raise config_contracts.ConfigError(
            f"{raw_key!r} is not a known IANA timezone: {value!r}"
        ) from error
    return value


def _validate_single_entry(normalized_key: str, raw_key: str, value: Any) -> Any:
    if normalized_key == "usage_limit_timezone":
        return _validate_timezone_entry(raw_key, value)
    if (
        normalized_key in BOOLEAN_CONFIG_KEYS
        or normalized_key in PATH_CONFIG_KEYS
        or normalized_key in STRING_KEYS
    ):
        return _validate_scalar_entry(normalized_key, raw_key, value)
    if (
        normalized_key in NON_NEGATIVE_INT_KEYS
        or normalized_key in POSITIVE_INT_KEYS
        or normalized_key == "consistency_max_repair_passes"
    ):
        return _validate_numeric_entry(normalized_key, raw_key, value)
    if normalized_key in {
        "dispatch_target",
        "reviewer_bot",
        "consistency_mode",
        "child_review_gate",
        "semantic_review_mode",
        "final_acceptance_review",
        "semantic_review_provider",
        "semantic_fix_provider",
        "semantic_review_trigger",
    }:
        return _validate_choice_entry(normalized_key, raw_key, value)
    return _validate_compound_entry(normalized_key, raw_key, value)


def validate_toml_config(config_data: dict[str, Any]) -> dict[str, Any]:
    """Validate TOML dictionary against the Dispatcher configuration schema."""
    validated: dict[str, Any] = {}
    for raw_key, value in config_data.items():
        if raw_key in PROHIBITED_TOML_KEYS:
            raise config_contracts.ConfigError(PROHIBITED_TOML_KEYS[raw_key])
        normalized = _normalize_key(raw_key)
        if normalized in PROHIBITED_TOML_KEYS:
            raise config_contracts.ConfigError(PROHIBITED_TOML_KEYS[normalized])

        val = _validate_single_entry(normalized, raw_key, value)
        validated[normalized] = val
        if raw_key != normalized:
            validated[raw_key] = val

    if (
        "dag_similarity_threshold" in config_data
        or "dag-similarity-threshold" in config_data
    ):
        extract_dag_similarity_threshold(config_data)
    if "dag_ignore_patterns" in config_data or "dag-ignore-patterns" in config_data:
        patterns = extract_dag_ignore_patterns(config_data)
        compile_extra_ignore_patterns(patterns)
    if (
        "execution_profiles" in config_data
        or "execution-profiles" in config_data
        or "default_execution_profile" in config_data
        or "default-execution-profile" in config_data
    ):
        extract_execution_profile_config(config_data)

    return validated
