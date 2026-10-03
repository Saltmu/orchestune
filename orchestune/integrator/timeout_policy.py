"""Execution-time limits and failure vocabulary for the Integrator (#820).

The defaults live here so ``DispatcherConfig`` and a directly constructed
``IntegratorConfig`` cannot drift apart. The values bound *waiting* (dependency
preparation, CI, the whole parent-Issue cycle, post-deadline cleanup and auxiliary
git/gh calls) and the number of automatic retries after a confirmed timeout. They do
not change the worker's ``task_timeout_seconds`` / reclaim policy: that is a
separate execution budget.

This module is dependency free (L0).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

DEFAULT_INTEGRATION_DEPENDENCY_TIMEOUT_SECONDS = 600
DEFAULT_INTEGRATION_CI_TIMEOUT_SECONDS = 1800
DEFAULT_INTEGRATION_CYCLE_TIMEOUT_SECONDS = 3600
DEFAULT_INTEGRATION_CLEANUP_TIMEOUT_SECONDS = 30
DEFAULT_INTEGRATION_COMMAND_TIMEOUT_SECONDS = 60
DEFAULT_MAX_INTEGRATION_TIMEOUT_RETRIES = 2
DEFAULT_INTEGRATION_TIMEOUT_BACKOFF_SECONDS = 60

# TOML keys use hyphens and Python fields underscores; these are the Python names.
POSITIVE_SECONDS_FIELDS: tuple[str, ...] = (
    "integration_dependency_timeout_seconds",
    "integration_ci_timeout_seconds",
    "integration_cycle_timeout_seconds",
    "integration_cleanup_timeout_seconds",
    "integration_command_timeout_seconds",
    "integration_timeout_backoff_seconds",
)
NON_NEGATIVE_COUNT_FIELDS: tuple[str, ...] = ("max_integration_timeout_retries",)
INTEGRATION_EXECUTION_FIELDS: tuple[str, ...] = (
    *POSITIVE_SECONDS_FIELDS,
    *NON_NEGATIVE_COUNT_FIELDS,
)


class ExecutionFailureCause(StrEnum):
    """Why an integration attempt did not complete normally."""

    DEPENDENCY_TIMEOUT = "dependency_timeout"
    CI_TIMEOUT = "ci_timeout"
    CYCLE_DEADLINE_EXCEEDED = "cycle_deadline_exceeded"
    CLEANUP_FAILED = "cleanup_failed"
    SIDE_EFFECT_INDETERMINATE = "side_effect_indeterminate"
    RETRY_BUDGET_EXHAUSTED = "retry_budget_exhausted"


# Causes that, once the stop and rollback are confirmed, count toward the retry budget.
COUNTED_TIMEOUT_CAUSES = frozenset(
    {
        ExecutionFailureCause.DEPENDENCY_TIMEOUT,
        ExecutionFailureCause.CI_TIMEOUT,
        ExecutionFailureCause.CYCLE_DEADLINE_EXCEEDED,
    }
)

SIDE_EFFECT_NONE = "none"
SIDE_EFFECT_UNKNOWN = "unknown"

# ``IntegrationStatus`` values for execution-bound outcomes. They live here so the
# execution/retry modules can name them without importing ``integrator.types``.
STATUS_EXECUTION_TIMED_OUT = "execution_timed_out"
STATUS_EXECUTION_CLEANUP_FAILED = "execution_cleanup_failed"
STATUS_EXECUTION_RETRY_EXHAUSTED = "execution_retry_exhausted"
STATUS_EXECUTION_INDETERMINATE = "execution_indeterminate"
EXECUTION_STATUSES = frozenset(
    {
        STATUS_EXECUTION_TIMED_OUT,
        STATUS_EXECUTION_CLEANUP_FAILED,
        STATUS_EXECUTION_RETRY_EXHAUSTED,
        STATUS_EXECUTION_INDETERMINATE,
    }
)


def _validate_positive(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")


def _validate_non_negative(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {value!r}")


@dataclass(frozen=True)
class IntegrationExecutionPolicy:
    """The seven settings that bound one parent Issue's integration execution."""

    integration_dependency_timeout_seconds: int = (
        DEFAULT_INTEGRATION_DEPENDENCY_TIMEOUT_SECONDS
    )
    integration_ci_timeout_seconds: int = DEFAULT_INTEGRATION_CI_TIMEOUT_SECONDS
    integration_cycle_timeout_seconds: int = DEFAULT_INTEGRATION_CYCLE_TIMEOUT_SECONDS
    integration_cleanup_timeout_seconds: int = (
        DEFAULT_INTEGRATION_CLEANUP_TIMEOUT_SECONDS
    )
    integration_command_timeout_seconds: int = (
        DEFAULT_INTEGRATION_COMMAND_TIMEOUT_SECONDS
    )
    max_integration_timeout_retries: int = DEFAULT_MAX_INTEGRATION_TIMEOUT_RETRIES
    integration_timeout_backoff_seconds: int = (
        DEFAULT_INTEGRATION_TIMEOUT_BACKOFF_SECONDS
    )

    def __post_init__(self) -> None:
        for name in POSITIVE_SECONDS_FIELDS:
            _validate_positive(name, getattr(self, name))
        for name in NON_NEGATIVE_COUNT_FIELDS:
            _validate_non_negative(name, getattr(self, name))

    @property
    def max_attempts(self) -> int:
        """The first attempt plus the allowed automatic retries."""
        return 1 + self.max_integration_timeout_retries


__all__ = [
    "COUNTED_TIMEOUT_CAUSES",
    "DEFAULT_INTEGRATION_CI_TIMEOUT_SECONDS",
    "DEFAULT_INTEGRATION_CLEANUP_TIMEOUT_SECONDS",
    "DEFAULT_INTEGRATION_COMMAND_TIMEOUT_SECONDS",
    "DEFAULT_INTEGRATION_CYCLE_TIMEOUT_SECONDS",
    "DEFAULT_INTEGRATION_DEPENDENCY_TIMEOUT_SECONDS",
    "DEFAULT_INTEGRATION_TIMEOUT_BACKOFF_SECONDS",
    "DEFAULT_MAX_INTEGRATION_TIMEOUT_RETRIES",
    "INTEGRATION_EXECUTION_FIELDS",
    "NON_NEGATIVE_COUNT_FIELDS",
    "POSITIVE_SECONDS_FIELDS",
    "EXECUTION_STATUSES",
    "SIDE_EFFECT_NONE",
    "SIDE_EFFECT_UNKNOWN",
    "STATUS_EXECUTION_CLEANUP_FAILED",
    "STATUS_EXECUTION_INDETERMINATE",
    "STATUS_EXECUTION_RETRY_EXHAUSTED",
    "STATUS_EXECUTION_TIMED_OUT",
    "ExecutionFailureCause",
    "IntegrationExecutionPolicy",
]
