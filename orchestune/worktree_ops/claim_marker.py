"""Ownership marker helpers used by worktree preparation and cleanup."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def claim_marker_path(worktree_path: Path) -> Path:
    """Return the ownership marker path beside the worktree."""
    return worktree_path.parent / f"{worktree_path.name}.claim.json"


def read_claim_marker(worktree_path: Path) -> dict[str, Any] | None:
    try:
        raw = claim_marker_path(worktree_path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        decoded = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def write_claim_marker(
    worktree_path: Path,
    *,
    claim_id: str,
    branch: str,
    base_sha: str | None,
    branch_created: bool,
) -> None:
    marker_path = claim_marker_path(worktree_path)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps(
            {
                "claim_id": claim_id,
                "branch": branch,
                "base_sha": base_sha,
                "branch_created": branch_created,
            }
        ),
        encoding="utf-8",
    )


def remove_claim_marker(worktree_path: Path) -> None:
    claim_marker_path(worktree_path).unlink(missing_ok=True)


def claim_lock_path(worktree_path: Path) -> Path:
    """Return the lock path shared by preparation and rollback operations."""
    return claim_marker_path(worktree_path).with_suffix(".lock")
