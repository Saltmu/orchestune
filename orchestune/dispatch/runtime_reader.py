"""Read-only provider configuration and fresh, bounded runtime observations."""

from __future__ import annotations

import os

from orchestune.claim.workspace import ClaimWorkspace
from orchestune.dag.models import ConfigError
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.config_loader import (
    _resolve_checkout_roots,
    _resolve_cloud_settings,
    validate_toml_config,
)
from orchestune.dispatch.external_execution import (
    RuntimeState,
    _attempt_matches_provider,
    observe_runtime_state,
)
from orchestune.dispatch.targets import (
    TargetBuildConfig,
    build_dispatch_target,
    resolve_default_dispatch_target_name,
)
from orchestune.forge import GitHubForge
from orchestune.infra.repository_config import find_and_load_config_file
from orchestune.ledger.run_state import ActiveWorktree


def provider_config(workspace: ClaimWorkspace) -> DispatcherConfig | None:
    # Resolve the primary checkout even when invoked from a linked worktree.
    primary, _ = _resolve_checkout_roots(workspace.repository_root)
    try:
        raw = validate_toml_config(find_and_load_config_file(primary))
    except (ConfigError, ValueError, TypeError, OSError) as error:
        raise ValueError("provider_config_invalid") from error
    routine, token, environment = _resolve_cloud_settings(raw)
    name = raw.get("dispatch_target") or resolve_default_dispatch_target_name(
        os.environ
    )
    if name not in {"cloud-routine", "codex-cloud"}:
        return None
    if (name == "cloud-routine" and not (routine and token)) or (
        name == "codex-cloud" and not environment
    ):
        return None
    try:
        target = build_dispatch_target(
            TargetBuildConfig(
                dispatch_target_name=name,
                routine_id=routine,
                routine_token=token,
                codex_cloud_env=environment,
                log_dir=primary / "logs",
                reviewer_bot=raw.get("reviewer_bot", "auto"),
            )
        )
        if target.target_name != name:
            return None
        return DispatcherConfig(
            parent_issue_number=0,
            # Attempt evidence is read under recovery locks; bound gh I/O too.
            forge=GitHubForge(timeout_seconds=30),
            dispatch_target=target,
            run_state_path=workspace.run_state_path,
            worktree_root=workspace.worktree_root,
            log_dir=primary / "logs",
            events_log_path=primary / "events.jsonl",
            not_needed_review_state_path=primary / "not_needed_review_state.json",
        )
    except Exception:
        return None


def read_runtime(
    active: ActiveWorktree, workspace: ClaimWorkspace
) -> tuple[RuntimeState, str]:
    config = provider_config(workspace)
    if config is None:
        return "unknown", "provider_unavailable"
    # Legacy launches have no evidence tying their ID to the configured provider.
    if not active.launch.launch_attempt_id or not _attempt_matches_provider(
        active, config, strict=True
    ):
        return "unknown", "provider_identity_unknown"
    status = observe_runtime_state(active, config)
    return (
        status,
        "provider_observed" if status != "unknown" else "provider_unavailable",
    )
