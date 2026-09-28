"""Pure token-limit verdict and publication-time provider/config resolution."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from orchestune.models import Usage
from orchestune.targets.completion_policy import (
    snapshot_publication_policy as snapshot_publication_policy,
)
from orchestune.targets.completion_policy import (
    token_limit_decision as token_limit_decision,
)
from orchestune.targets.contracts import DispatchHandle
from orchestune.targets.usage import LocalUsageProvider


def evaluate_publication_policy(
    active: Any, worktree: Path, forge: Any
) -> dict[str, Any]:
    try:
        snapshot = active.completion_policy_config or snapshot_publication_policy(
            worktree
        )
        limit = snapshot["max_tokens_per_task"]
        usage = None
        if limit is not None:
            handle = DispatchHandle(
                pid=active.pid,
                external_id=active.external_id,
                external_url=active.external_url,
                branch_name=active.branch,
                issue_number=active.issue_number,
                started_at=active.started_at,
                launch_attempt_id=active.launch_attempt_id,
            )
            if handle.external_id is None and snapshot.get("dispatch_target") in {
                "auto",
                "local",
                "claude-cli",
                "agy-cli",
                "codex-cli",
            }:
                usage = LocalUsageProvider(snapshot["log_dir"]).collect_usage(handle)
        return {
            "decision": token_limit_decision(limit, usage),
            "limit": limit,
            "usage": asdict(usage) if isinstance(usage, Usage) else None,
            "config": snapshot,
        }
    except Exception as error:
        return {
            "decision": "unknown",
            "error": str(error),
            "config": getattr(active, "completion_policy_config", None),
        }
