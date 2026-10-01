"""Generation-guarded recovery with retained work and atomic durable receipts."""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from orchestune.claim.local_identity import registered_claim_path
from orchestune.claim.workspace import ClaimWorkspace, resolve_claim_workspace
from orchestune.infra.json_state import write_json_atomic
from orchestune.infra.process_utils import FileLock, run_state_lock
from orchestune.ledger.run_state import (
    ActiveWorktree,
    RunState,
    load_run_state_readonly,
)
from orchestune.recovery.contracts import RecoveryRequest, RecoveryResult
from orchestune.recovery.inspection import inspect_claim
from orchestune.worktree_ops.claim_marker import claim_lock_path, write_claim_marker


def _recover(
    request: RecoveryRequest, workspace: ClaimWorkspace, cwd: Path
) -> RecoveryResult:
    state = load_run_state_readonly(workspace.run_state_path)
    matches = [
        (key, active)
        for key, active in state.active_worktrees.items()
        if active.issue_number == request.issue_number
    ]
    receipt_key = f"{workspace.repository_identity}::{request.issue_number}::{request.claim_id}::release"
    if not matches:
        if request.claim_id and receipt_key in state.recovery_receipts:
            return RecoveryResult(
                True, request.issue_number, "already_released", "saved release receipt"
            )
        return RecoveryResult(False, request.issue_number, "held", "no active claim")
    if len(matches) != 1:
        return RecoveryResult(
            False, request.issue_number, "held", "ambiguous active claims"
        )
    key, active = matches[0]
    if request.claim_id is not None and request.claim_id != active.claim_id:
        return RecoveryResult(
            False, request.issue_number, "held", "claim generation changed"
        )
    diagnostics, problem = inspect_claim(
        active, state, workspace, cwd, restore_marker=request.restore_marker
    )
    if problem:
        return RecoveryResult(False, request.issue_number, "held", problem, diagnostics)
    operation = "restore-marker" if request.restore_marker else "release"
    if not request.apply:
        return RecoveryResult(
            True,
            request.issue_number,
            f"would_{operation.replace('-', '_')}",
            "verified local claim; worktree and GitHub state will be retained",
            diagnostics,
        )
    return _apply_recovery(request, workspace, cwd, key, active, state)


def _apply_recovery(
    request: RecoveryRequest,
    workspace: ClaimWorkspace,
    cwd: Path,
    key: str,
    active: ActiveWorktree,
    state: RunState,
) -> RecoveryResult:
    operation = "restore-marker" if request.restore_marker else "release"
    target = registered_claim_path(active, workspace.run_state_path)
    with (
        FileLock(claim_lock_path(target), timeout=request.timeout_seconds)
        if active.worktree_path
        else nullcontext()
    ):
        # Marker and process state can change without taking the shared ledger lock.
        diagnostics, problem = inspect_claim(
            active, state, workspace, cwd, restore_marker=request.restore_marker
        )
        if problem:
            return RecoveryResult(
                False, request.issue_number, "held", problem, diagnostics
            )
        if request.restore_marker:
            assert active.claim_id is not None
            # Recovery cannot prove who created the branch; retain it on rollback.
            write_claim_marker(
                target,
                claim_id=active.claim_id,
                branch=active.branch,
                base_sha=active.base_sha,
                branch_created=False,
            )
        _save_recovery(request, workspace, key, active, operation)
    return RecoveryResult(
        True,
        request.issue_number,
        "marker_restored" if request.restore_marker else "released",
        "worktree, branch and GitHub state retained",
        diagnostics,
    )


def _save_recovery(
    request: RecoveryRequest,
    workspace: ClaimWorkspace,
    key: str,
    active: ActiveWorktree,
    operation: str,
) -> None:
    raw = json.loads(workspace.run_state_path.read_text(encoding="utf-8"))
    if operation == "release":
        del raw["active_worktrees"][key]
    record_key = f"{workspace.repository_identity}::{request.issue_number}::{active.claim_id}::{operation}"
    raw.setdefault("recovery_receipts", {})[record_key] = {
        "schema_version": 1,
        "repository_id": workspace.repository_identity,
        "issue_number": request.issue_number,
        "claim_id": active.claim_id,
        "operation": operation,
        "reason": request.reason,
        "recorded_at": datetime.now(UTC).isoformat(),
        "worktree_action": "retain",
        "active": asdict(active),
    }
    # Preserve all other records and extensions exactly; no retention pruning.
    write_json_atomic(workspace.run_state_path, raw)


def recover_claim(
    request: RecoveryRequest, *, cwd: str | Path | None = None
) -> RecoveryResult:
    try:
        request.validate()
        workspace = resolve_claim_workspace(cwd, explicit_state_path=request.state_path)
        current = Path(cwd or Path.cwd()).resolve()
        if not request.apply:
            return _recover(request, workspace, current)
        with run_state_lock(workspace.lock_path, timeout=request.timeout_seconds):
            return _recover(request, workspace, current)
    except (OSError, ValueError, RuntimeError) as error:
        return RecoveryResult(False, request.issue_number, "held", str(error))
