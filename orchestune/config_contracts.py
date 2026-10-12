"""Shared L0 configuration contracts (error type for `orchestune.toml` problems)."""

from __future__ import annotations


class ConfigError(ValueError):
    """Raised for invalid `orchestune.toml` / `pyproject.toml` config values.

    A `ValueError` subclass so existing `except ValueError` call sites keep
    working unchanged, but distinct from other `ValueError`s (e.g.
    `DagCycleError`, footprint validation) so callers can catch config
    problems specifically and map them to a dedicated exit code (#428:
    orchestune-dag / orchestune-provision / orchestune-dispatch all exit 2
    for config errors, distinct from exit 1 for other failures).
    """
