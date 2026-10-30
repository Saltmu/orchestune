"""Validation adapters and diagnosis helpers for the configuration wizard."""

from __future__ import annotations

import tomllib
from typing import Any

from orchestune.config_contracts import ConfigError
from orchestune.dag.models import (
    DAG_TOOL_CONFIG_KEYS,
    compile_extra_ignore_patterns,
    extract_dag_ignore_patterns,
    extract_dag_similarity_threshold,
)
from orchestune.dispatch.config_schema import (
    BOOLEAN_CONFIG_KEYS,
    NON_NEGATIVE_INT_KEYS,
    PATH_CONFIG_KEYS,
    POSITIVE_INT_KEYS,
    PROHIBITED_TOML_KEYS,
    STRING_KEYS,
    validate_toml_config,
)
from orchestune.dispatch.execution_profiles import extract_execution_profile_config

_KNOWN_SCALAR_KEYS = frozenset(
    BOOLEAN_CONFIG_KEYS
    | PATH_CONFIG_KEYS
    | STRING_KEYS
    | NON_NEGATIVE_INT_KEYS
    | POSITIVE_INT_KEYS
    | {
        "dispatch_target",
        "reviewer_bot",
        "consistency_mode",
        "child_review_gate",
        "consistency_repair_code",
        "consistency_max_repair_passes",
        "ci_command",
    }
)

_KNOWN_COMPOUND_KEYS = frozenset(
    {
        "execution_profiles",
    }
    | DAG_TOOL_CONFIG_KEYS
)


def _is_known_key(key: str) -> bool:
    norm = key.replace("-", "_")
    return (
        norm in _KNOWN_SCALAR_KEYS
        or norm in _KNOWN_COMPOUND_KEYS
        or key in DAG_TOOL_CONFIG_KEYS
    )


def diagnose_existing_document(doc_data: dict[str, Any]) -> list[str]:
    """Diagnose existing TOML content for prohibited keys, unknown keys, or invalid values without leaking secrets."""
    diagnostics: list[str] = []
    seen: set[str] = set()

    for raw_key in doc_data:
        norm_key = raw_key.replace("-", "_")
        if raw_key in PROHIBITED_TOML_KEYS or norm_key in PROHIBITED_TOML_KEYS:
            guidance = (
                PROHIBITED_TOML_KEYS[raw_key]
                if raw_key in PROHIBITED_TOML_KEYS
                else PROHIBITED_TOML_KEYS[norm_key]
            )
            msg = f"Prohibited key '{raw_key}': {guidance}"
            diagnostics.append(msg)
            seen.add(raw_key)
            seen.add(norm_key)
        elif not _is_known_key(raw_key):
            msg = f"Unknown key '{raw_key}' is not recognized in the current configuration schema."
            diagnostics.append(msg)
            seen.add(raw_key)
            seen.add(norm_key)

    try:
        validate_toml_config(doc_data)
    except ConfigError as exc:
        err_msg = str(exc)
        # Avoid redundant duplicate message if key was already diagnosed
        already_reported = any(f"'{k}'" in err_msg for k in seen)
        if not already_reported:
            diagnostics.append(err_msg)
    except Exception as exc:
        diagnostics.append(f"Validation error: {exc}")

    return diagnostics


def validate_candidate_document(doc_str: str) -> tuple[bool, list[str]]:
    """Validate full candidate TOML string against all existing schema validators."""
    try:
        data = tomllib.loads(doc_str)
    except Exception as exc:
        return False, [f"TOML syntax error: {exc}"]

    errors: list[str] = []
    try:
        validate_toml_config(data)
    except ConfigError as exc:
        errors.append(str(exc))
    except Exception as exc:
        errors.append(f"Unexpected validation error: {exc}")

    if any(
        k in data
        for k in (
            "execution_profiles",
            "execution-profiles",
            "default_execution_profile",
            "default-execution-profile",
        )
    ):
        try:
            extract_execution_profile_config(data)
        except ConfigError as exc:
            if str(exc) not in errors:
                errors.append(str(exc))
        except Exception as exc:
            errors.append(f"Profile error: {exc}")

    if any(k in data for k in ("dag_similarity_threshold", "dag-similarity-threshold")):
        try:
            extract_dag_similarity_threshold(data)
        except ConfigError as exc:
            if str(exc) not in errors:
                errors.append(str(exc))

    if any(k in data for k in ("dag_ignore_patterns", "dag-ignore-patterns")):
        try:
            patterns = extract_dag_ignore_patterns(data)
            compile_extra_ignore_patterns(patterns)
        except ConfigError as exc:
            if str(exc) not in errors:
                errors.append(str(exc))

    return len(errors) == 0, errors
