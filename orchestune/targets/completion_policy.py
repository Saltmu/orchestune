"""Snapshot publication limits from the shared repository configuration loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestune.infra.repository_config import find_and_load_config_file
from orchestune.models import Usage


def snapshot_publication_policy(worktree: Path) -> dict[str, Any]:
    raw = find_and_load_config_file(worktree)
    config = {key.replace("-", "_"): value for key, value in raw.items()}
    limit = config.get("max_tokens_per_task")
    if limit is not None and (
        not isinstance(limit, int) or isinstance(limit, bool) or limit < 0
    ):
        raise ValueError(
            "max_tokens_per_task must be a nonnegative integer or unlimited"
        )
    log_dir = Path(config.get("log_dir", "logs"))
    if not log_dir.is_absolute():
        log_dir = worktree / log_dir
    return {
        "max_tokens_per_task": limit,
        "source": "repository-config",
        "dispatch_target": config.get("dispatch_target", "auto"),
        "log_dir": str(log_dir),
    }


def token_limit_decision(limit: int | None, usage: Usage | None) -> str:
    if limit is None:
        return "allowed"
    if not isinstance(usage, Usage):
        return "unknown"
    return "exceeded" if usage.total_tokens > limit else "allowed"
