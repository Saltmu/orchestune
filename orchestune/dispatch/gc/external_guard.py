"""Fresh external identity, evidence and runtime checks at GC boundaries."""

from __future__ import annotations

from orchestune.claim.workspace import ClaimWorkspace, resolve_claim_workspace
from orchestune.dispatch import external_execution as runtime_observation
from orchestune.dispatch import runtime_reader as external_execution
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.external_execution import (
    ExternalExecutionHold,
    HoldReason,
    hold_if_not_stopped,
    is_external_execution,
)
from orchestune.infra.process_utils import is_process_alive
from orchestune.ledger.external_stop_receipts import matching_confirmation
from orchestune.ledger.external_stop_receipts import same_execution as same_execution
from orchestune.ledger.run_state import (
    ActiveWorktree,
    RunState,
    load_run_state_readonly,
)


def _held(active: ActiveWorktree, reason: HoldReason) -> ExternalExecutionHold:
    return ExternalExecutionHold(
        active.core.issue_number,
        reason,
        "unknown",
        active.claim.claim_id,
        active.launch.launch_attempt_id,
        active.launch.external_id,
    )


class ExternalExecutionChanged(RuntimeError):
    def __init__(self, hold: ExternalExecutionHold):
        super().__init__("external_execution_held")
        self.hold = hold


def require_external_stop(
    active: ActiveWorktree, config: DispatcherConfig, state: RunState | None = None
) -> None:
    if hold := fresh_external_hold(active, config, "completion", state):
        raise ExternalExecutionChanged(hold)


def fresh_external_hold(
    active: ActiveWorktree,
    config: DispatcherConfig,
    reason: HoldReason,
    state: RunState | None = None,
) -> ExternalExecutionHold | None:
    if not is_external_execution(active):
        return None
    workspace = resolve_claim_workspace(explicit_state_path=config.run_state_path)
    fresh = load_run_state_readonly(config.run_state_path)
    if config.run_state_path.exists() and not _current_execution(active, fresh):
        return _held(active, reason)
    runtime = runtime_observation.observe_runtime_state(active, config)
    fresh = load_run_state_readonly(config.run_state_path)
    if config.run_state_path.exists() and not _current_execution(active, fresh):
        return _held(active, reason)
    # Older callers without a persisted ledger cannot supply operator evidence.
    if state is not None:
        state.recovery_receipts = fresh.recovery_receipts
    return hold_if_not_stopped(
        active,
        config,
        reason,
        state=fresh,
        repository_id=workspace.repository_identity,
        observe=lambda *_: runtime,
    )


def _current_execution(active: ActiveWorktree, state: RunState) -> bool:
    matches = [
        a
        for a in state.active_worktrees.values()
        if a.core.issue_number == active.core.issue_number
    ]
    return (
        len(matches) == 1
        and same_execution(active, matches[0])
        and not (matches[0].launch.pid and is_process_alive(matches[0].launch.pid))
    )


def collection_stop_problem(
    active: ActiveWorktree,
    workspace: ClaimWorkspace,
    config: DispatcherConfig | None = None,
) -> str | None:
    if not is_external_execution(active):
        return None
    state = load_run_state_readonly(workspace.run_state_path)
    if not _current_execution(active, state):
        return "external_execution_identity_changed"
    if config is not None:
        runtime = runtime_observation.observe_runtime_state(active, config)
    else:
        runtime, _ = external_execution.read_runtime(active, workspace)
    state = load_run_state_readonly(workspace.run_state_path)
    if not _current_execution(active, state):
        return "external_execution_identity_changed"
    if runtime == "stopped":
        return None
    if runtime == "unknown" and matching_confirmation(
        state, active, workspace.repository_identity
    ):
        return None
    return "external_execution_held"
