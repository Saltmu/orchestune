"""Unified claim orchestration service integrating validation, reservation, worktree, and state."""

from __future__ import annotations

from typing import Any


def claim_task(*args: Any, **kwargs: Any) -> Any:
    """Execute the sequential lifecycle to claim an issue."""
    raise NotImplementedError("claim_task is not yet implemented")


def resume_claim(*args: Any, **kwargs: Any) -> Any:
    """Resume an interrupted claim session matching a claim_id and owner token."""
    raise NotImplementedError("resume_claim is not yet implemented")
