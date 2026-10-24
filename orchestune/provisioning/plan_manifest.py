"""Manifest format definitions and detection for decomposition plans."""

from __future__ import annotations

from typing import Any

from orchestune.issue_parsing import PLAN_MANIFEST_FORMAT

SUPPORTED_PLAN_FORMATS = frozenset({PLAN_MANIFEST_FORMAT})


def is_manifest_plan(plan: dict[str, Any]) -> bool:
    """Return True if plan specifies a supported manifest plan_format.

    Raises ValueError if plan_format is present but not in SUPPORTED_PLAN_FORMATS.
    """
    plan_format = plan.get("plan_format")
    if plan_format is None:
        return False
    if plan_format not in SUPPORTED_PLAN_FORMATS:
        raise ValueError(
            f"unsupported plan_format: {plan_format!r} (must be one of {sorted(SUPPORTED_PLAN_FORMATS)})"
        )
    return bool(plan_format == PLAN_MANIFEST_FORMAT)


__all__ = [
    "PLAN_MANIFEST_FORMAT",
    "SUPPORTED_PLAN_FORMATS",
    "is_manifest_plan",
]
