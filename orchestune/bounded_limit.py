"""Shared decisions for finite retry counts and elapsed-time limits."""

from __future__ import annotations


def exceeds_limit(value: int | float, limit: int | float) -> bool:
    """Return whether *value* is strictly greater than its configured limit."""
    return value > limit
