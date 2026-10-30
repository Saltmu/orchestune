"""Shared L0 configuration contracts (error type for `orchestune.toml` problems)."""

from __future__ import annotations

MODEL_TIER_MIGRATION_GUIDANCE = (
    "replace model_tier with an execution_profile that defines both the model "
    "and reasoning effort for each dispatch target"
)
MODEL_TIERS_MIGRATION_GUIDANCE = (
    "move each tier model to execution_profiles.<name>.<target>.model and "
    "select that profile with execution_profile"
)


def retired_model_tier_message() -> str:
    """Return shared migration guidance for a retired task model tier."""
    return f"'model_tier' is retired; {MODEL_TIER_MIGRATION_GUIDANCE}"


def retired_model_tiers_message(key: str) -> str:
    """Return shared migration guidance for a retired model-tier table."""
    return (
        f"setting {key!r} is retired and prohibited in configuration files; "
        f"{MODEL_TIERS_MIGRATION_GUIDANCE}"
    )


class ConfigError(ValueError):
    """Raised for invalid `orchestune.toml` / `pyproject.toml` config values.

    A `ValueError` subclass so existing `except ValueError` call sites keep
    working unchanged, but distinct from other `ValueError`s (e.g.
    `DagCycleError`, footprint validation) so callers can catch config
    problems specifically and map them to a dedicated exit code (#428:
    orchestune-dag / orchestune-provision / orchestune-dispatch all exit 2
    for config errors, distinct from exit 1 for other failures).
    """
