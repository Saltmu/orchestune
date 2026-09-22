"""Fail-closed workflow entry point for task completion."""

from __future__ import annotations

from typing import Any


def complete_task(request: Any) -> Any:
    """Complete a claimed task after all result-specific checks succeed."""
    raise NotImplementedError("task completion is not implemented")
