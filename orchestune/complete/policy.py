"""Pure token-limit verdict and publication-time provider/config resolution."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from orchestune.complete.journal import thaw_json
from orchestune.complete.preflight import active_field
from orchestune.models import Usage
from orchestune.targets.completion_policy import (
    snapshot_publication_policy as snapshot_publication_policy,
)
from orchestune.targets.completion_policy import (
    token_limit_decision as token_limit_decision,
)
from orchestune.targets.contracts import DispatchHandle
from orchestune.targets.usage import LocalUsageProvider


def _collect_policy_usage(active: Any, snapshot: dict[str, Any]) -> Usage | None:
    limit = snapshot.get("max_tokens_per_task")
    if limit is None:
        return None
    handle = DispatchHandle(
        pid=active_field(active, "launch", "pid"),
        external_id=active_field(active, "launch", "external_id"),
        external_url=active_field(active, "launch", "external_url"),
        branch_name=active_field(active, "core", "branch"),
        issue_number=active_field(active, "core", "issue_number"),
        started_at=active_field(active, "launch", "started_at"),
        launch_attempt_id=active_field(active, "launch", "launch_attempt_id"),
    )
    supported_targets = {"auto", "local", "claude-cli", "agy-cli", "codex-cli"}
    if (
        handle.external_id is None
        and snapshot.get("dispatch_target") in supported_targets
    ):
        return LocalUsageProvider(snapshot["log_dir"]).collect_usage(handle)
    return None


def evaluate_publication_policy(
    active: Any, worktree: Path, forge: Any
) -> dict[str, Any]:
    try:
        policy_config = active_field(active, "completion", "completion_policy_config")
        snapshot = (
            thaw_json(policy_config)
            if policy_config is not None
            else snapshot_publication_policy(worktree)
        )
        limit = snapshot["max_tokens_per_task"]
        usage = _collect_policy_usage(active, snapshot)
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
            "config": active_field(active, "completion", "completion_policy_config"),
        }
