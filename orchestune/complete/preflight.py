"""Fail-closed extension points for completion preflight checks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class WorktreeStatus(str, Enum):
    """Observed state of a completion worktree."""

    CLEAN = "clean"
    DIRTY = "dirty"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CompletePreflight:
    """Placeholder result for the result-specific completion preflight."""

    accepted: bool
    worktree_status: WorktreeStatus = WorktreeStatus.UNKNOWN
    reason: str | None = None


def inspect_worktree_status(worktree_path: Path | str | None) -> WorktreeStatus:
    """Inspect a worktree once the preflight implementation is available."""
    raise NotImplementedError("complete worktree inspection is not implemented")


def evaluate_complete_preflight(request: Any) -> CompletePreflight:
    """Evaluate ownership, result, PR, and worktree preconditions."""
    raise NotImplementedError("complete preflight is not implemented")
