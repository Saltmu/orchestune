"""Select explicitly configured values for ``DispatcherConfig`` (#1189).

``DispatcherConfig`` owns every default. Entry points (the Dispatcher's TOML loader and
standalone GC) forward only the settings the user actually specified; a missing key is
left to the config's field default instead of being re-stated as a literal.

The key sets are declarative allowlists: they select values, they do not define the
public TOML surface (``config_loader.validate_toml_config`` keeps that contract) and
they never expose an internal field by schema generation. Callers pass already
normalized (underscore) keys; this module does no I/O, parsing or validation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# Settings the Dispatcher accepts from configuration and forwards unchanged.
RUNTIME_TUNING_KEYS: tuple[str, ...] = (
    "max_concurrent",
    "max_launches_per_window",
    "window_seconds",
    "deviation_buffer_lines",
    "max_recompute_retries",
    "task_timeout_seconds",
    "max_task_reclaims",
    "early_death_window_seconds",
    "max_early_death_retries",
    "early_death_backoff_seconds",
    "zombie_gc",
    "max_tokens_per_window",
    "max_tokens_per_task",
    "not_needed_review_timeout_seconds",
    "consistency_max_repair_passes",
    # #820: bounds on the Integrator's dependency preparation, CI and cycle.
    "integration_dependency_timeout_seconds",
    "integration_ci_timeout_seconds",
    "integration_cycle_timeout_seconds",
    "integration_cleanup_timeout_seconds",
    "integration_command_timeout_seconds",
    "max_integration_timeout_retries",
    "integration_timeout_backoff_seconds",
)

# Settings standalone GC reads from the repository configuration file.
COMPLETION_POLICY_KEYS: tuple[str, ...] = (
    "max_review_timeout_retries",
    "review_timeout_backoff_seconds",
    "not_needed_review_timeout_seconds",
)


def _explicit(values: Mapping[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    return {key: values[key] for key in keys if key in values}


def runtime_tuning_overrides(values: Mapping[str, Any]) -> dict[str, Any]:
    """The explicitly configured runtime tuning values, keyed by field name."""
    return _explicit(values, RUNTIME_TUNING_KEYS)


def completion_policy_overrides(values: Mapping[str, Any]) -> dict[str, Any]:
    """The explicitly configured completion-policy values, keyed by field name."""
    return _explicit(values, COMPLETION_POLICY_KEYS)
