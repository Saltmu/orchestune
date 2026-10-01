"""Pure token-limit verdict and publication-time provider/config resolution."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from orchestune.ledger.active_records import _thaw_json
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
    launch = getattr(active, "launch", active)
    core = getattr(active, "core", active)
    handle = DispatchHandle(
        pid=launch.pid,
        external_id=launch.external_id,
        external_url=launch.external_url,
        branch_name=core.branch,
        issue_number=core.issue_number,
        started_at=launch.started_at,
        launch_attempt_id=launch.launch_attempt_id,
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
        policy_config = (
            active.completion.completion_policy_config
            if hasattr(active, "completion")
            else getattr(active, "completion_policy_config", None)
        )
        snapshot = (
            _thaw_json(policy_config)
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
            "config": getattr(
                getattr(active, "completion", active),
                "completion_policy_config",
                None,
            ),
        }
