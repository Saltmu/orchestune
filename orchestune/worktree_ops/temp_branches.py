"""Cleanup operations for temporary integration branches."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from orchestune.forge import Forge, GitHubForge
from orchestune.infra.git_cli import run_git

HOLDS_DIRNAME = ".holds"


def holds_dir(original_root: Path) -> Path:
    """Where ownership records of held (never auto-reclaimed) worktrees live (#820)."""
    return original_root / "worktrees" / HOLDS_DIRNAME


def load_holds(original_root: Path) -> list[dict[str, Any]] | None:
    """Every hold record, or ``None`` when the records cannot be read and reconciled.

    ``None`` means "do not reclaim anything": an unreadable or malformed record is
    never read as "no hold".
    """
    directory = holds_dir(original_root)
    if not directory.exists():
        return []
    holds: list[dict[str, Any]] = []
    try:
        for entry in sorted(directory.iterdir()):
            if entry.suffix != ".json":
                continue
            record = json.loads(entry.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or not isinstance(
                record.get("temp_branch"), str
            ):
                return None
            holds.append(record)
    except (OSError, ValueError):
        return None
    return holds


def _list_remote_temp_refs(root: Path, forge: Forge) -> tuple[str, set[str]] | None:
    """Fetch remote temp refs and the open PR heads that must remain protected."""
    try:
        run_git(
            [
                "fetch",
                "--prune",
                "origin",
                "+refs/heads/integration/temp-*:refs/remotes/origin/integration/temp-*",
            ],
            cwd=root,
            check=True,
        )
        refs = run_git(
            [
                "for-each-ref",
                "--format=%(refname:short) %(committerdate:unix)",
                "refs/remotes/origin/integration/temp-",
            ],
            cwd=root,
            check=True,
        )
        protected_heads = {pr.head_ref for pr in forge.list_open_prs()}
        return refs.stdout, protected_heads
    except Exception as error:
        print(
            f"Warning: Failed to enumerate stale integration temp branches: {error}",
            file=sys.stderr,
        )
        return None


def _is_stale_temp_branch(
    line: str, protected_heads: set[str], cutoff: float
) -> str | None:
    """Return a stale integration temp branch name from a for-each-ref row."""
    try:
        remote_name, timestamp = line.rsplit(maxsplit=1)
        branch = remote_name.removeprefix("origin/")
        if not branch.startswith("integration/temp-"):
            return None
        if branch in protected_heads or float(timestamp) > cutoff:
            return None
        return branch
    except (TypeError, ValueError):
        return None


def prune_stale_integration_temp_branches(
    repository_root: str | Path,
    *,
    forge: Forge | None = None,
    now: float | None = None,
    max_age_seconds: float = 24 * 60 * 60,
) -> list[str]:
    """Delete old integration temp branches that are not open PR heads."""
    forge = forge or GitHubForge()
    # #820: a held worktree's temp branch is evidence for a human. If the hold records
    # cannot be read and reconciled, nothing is collected.
    holds = load_holds(Path(repository_root))
    if holds is None:
        print(
            "Warning: Skipping stale integration temp branch GC: hold records "
            "could not be reconciled.",
            file=sys.stderr,
        )
        return []
    held_branches = {str(hold["temp_branch"]) for hold in holds}
    ref_info = _list_remote_temp_refs(Path(repository_root), forge)
    if ref_info is None:
        return []

    refs_stdout, protected_heads = ref_info
    cutoff = (time.time() if now is None else now) - max_age_seconds
    deleted: list[str] = []
    for line in refs_stdout.splitlines():
        branch = _is_stale_temp_branch(line, protected_heads | held_branches, cutoff)
        if branch is None:
            continue
        try:
            forge.delete_branch(branch)
            deleted.append(branch)
        except Exception as error:
            print(
                f"Warning: Failed to delete stale integration branch '{branch}': {error}",
                file=sys.stderr,
            )
    return deleted
