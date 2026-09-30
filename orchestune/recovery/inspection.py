"""Read-only checks shared by recovery preview and locked application."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestune.claim.local_identity import (
    registered_claim_path,
    validate_claim_worktree,
)
from orchestune.claim.workspace import ClaimWorkspace
from orchestune.infra.git_cli import inspect_worktree_status
from orchestune.infra.process_utils import is_process_alive
from orchestune.ledger.completion_reservations import completion_reservation_status
from orchestune.ledger.run_state import ActiveWorktree, RunState
from orchestune.worktree_ops.claim_marker import claim_marker_path, read_claim_marker


def inspect_claim(
    active: ActiveWorktree,
    state: RunState,
    workspace: ClaimWorkspace,
    cwd: Path,
    *,
    restore_marker: bool,
) -> tuple[dict[str, Any], str | None]:
    target = registered_claim_path(active, workspace.run_state_path)
    marker = read_claim_marker(target)
    running = bool(active.pid and is_process_alive(active.pid))
    diagnostics = {
        "claim_id": active.claim_id,
        "owner_kind": active.owner_kind,
        "claim_stage": active.claim_stage,
        "worktree": active.worktree_path,
        "branch": active.branch,
        "pid": active.pid,
        "running": running,
        "launch_phase": active.launch_phase,
        "completion_id": active.completion_id,
        "completion_stage": active.completion_stage,
        "worktree_status": inspect_worktree_status(target).value
        if target.exists()
        else "absent",
        "marker": "missing" if marker is None else "present",
        "worktree_action": "retain",
    }
    problem = _worktree_problem(active, workspace, cwd, restore_marker)
    if problem is None and not restore_marker:
        problem = _completion_problem(active, state)
    return diagnostics, problem


def _worktree_problem(
    active: ActiveWorktree, workspace: ClaimWorkspace, cwd: Path, restore_marker: bool
) -> str | None:
    target = registered_claim_path(active, workspace.run_state_path)
    marker = read_claim_marker(target)
    if active.repository_id != workspace.repository_identity:
        return "repository identity differs"
    if active.worktree_path and (
        target.resolve() == cwd or target.resolve() in cwd.parents
    ):
        return "run recovery from the primary checkout, outside the target worktree"
    if active.pid and is_process_alive(active.pid):
        return "agent is still running; stop it before recovery"
    if active.external_id or active.launch_phase in {
        "launching",
        "unknown",
        "prepared",
    }:
        return "launch status is uncertain or external; reconcile it with the Dispatcher first"
    if active.worktree_path and target.exists():
        try:
            validate_claim_worktree(
                active, workspace.run_state_path, require_marker=False
            )
        except ValueError as error:
            return str(error)
    elif restore_marker:
        return "cannot restore a marker without a verified worktree"
    if claim_marker_path(target).is_symlink():
        return "claim marker is a symlink"
    if marker and (
        marker.get("claim_id") != active.claim_id
        or marker.get("branch") != active.branch
    ):
        return "claim marker belongs to another generation"
    return None


def _completion_problem(active: ActiveWorktree, state: RunState) -> str | None:
    try:
        status = completion_reservation_status(state, active.issue_number)
    except ValueError:
        return "completion state is invalid; inspect and resume completion"
    if active.completion_id and not active.completion_handoff_ready:
        return (
            "completion publication is unfinished; restore marker and resume complete"
        )
    if status not in {"legacy", "handed_off"}:
        return "completion reservation is unfinished; resume complete"
    return None
