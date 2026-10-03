"""External operator recovery with fresh checks and one atomic ledger write."""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from orchestune.claim.local_identity import registered_claim_path
from orchestune.claim.workspace import ClaimWorkspace
from orchestune.dispatch.config_loader import _resolve_checkout_roots
from orchestune.infra.json_state import write_json_atomic
from orchestune.infra.process_utils import FileLock
from orchestune.ledger.external_stop_receipts import (
    confirmation_key,
    confirmation_record,
    execution_identity,
    matching_confirmation,
    valid_confirmation,
)
from orchestune.ledger.run_state import (
    ActiveWorktree,
    RunState,
    load_run_state_readonly,
)
from orchestune.recovery import external_execution
from orchestune.recovery.contracts import RecoveryRequest, RecoveryResult
from orchestune.recovery.external_completion import completion_retention
from orchestune.recovery.inspection import inspect_claim
from orchestune.worktree_ops.claim_marker import claim_lock_path


def _select(
    request: RecoveryRequest, state: RunState, workspace: ClaimWorkspace
) -> tuple[str, ActiveWorktree] | None:
    matches = [
        (k, a)
        for k, a in state.active_worktrees.items()
        if a.core.issue_number == request.issue_number
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("ambiguous active claims")
    key, active = matches[0]
    identity = execution_identity(active)
    if (
        identity["repository_id"] != workspace.repository_identity
        or identity["claim_id"] != request.claim_id
        or identity["external_id"] != request.external_id
        or identity["launch_attempt_id"] != request.launch_attempt_id
    ):
        raise ValueError("external execution generation changed")
    receipts = _requested_confirmations(request, state, workspace)
    if receipts and (
        len(receipts) != 1
        or receipts[0][0] != confirmation_key(active)
        or receipts[0][1].get("execution_identity") != identity
    ):
        raise ValueError("external execution generation changed")
    return key, active


def _absent(
    request: RecoveryRequest, state: RunState, workspace: ClaimWorkspace
) -> RecoveryResult:
    matches = _requested_confirmations(request, state, workspace)
    if len(matches) != 1 or not valid_confirmation(*matches[0]):
        raise ValueError("no unique valid external confirmation")
    key, confirmation = matches[0]
    release = state.recovery_receipts.get(
        f"{workspace.repository_identity}::{request.issue_number}::{request.claim_id}::release",
        {},
    )
    released = (
        release.get("operation") == "release"
        and release.get("schema_version") == 1
        and release.get("confirmation_receipt_key") == key
        and release.get("repository_id") == workspace.repository_identity
        and release.get("issue_number") == request.issue_number
        and release.get("claim_id") == request.claim_id
        and _snapshot_matches(release.get("active"), confirmation["execution_identity"])
    )
    if release.get("confirmation_receipt_key") == key and not _valid_linked_release(
        key, release
    ):
        raise ValueError("external_release_receipt_invalid")
    action = "already_external_stop_confirmed_" + (
        "released" if released else "active_absent"
    )
    return RecoveryResult(
        True, request.issue_number, action, "saved operator confirmation; active absent"
    )


def _requested_confirmations(
    request: RecoveryRequest, state: RunState, workspace: ClaimWorkspace
) -> list[tuple[str, dict[str, Any]]]:
    matches = [
        (k, r)
        for k, r in state.recovery_receipts.items()
        if r.get("operation") == "external-stop-confirmation"
        and all(
            r.get(n) == v
            for n, v in (
                ("repository_id", workspace.repository_identity),
                ("issue_number", request.issue_number),
                ("claim_id", request.claim_id),
                ("external_id", request.external_id),
                ("launch_attempt_id", request.launch_attempt_id),
            )
        )
    ]
    return matches


def _snapshot_matches(snapshot: Any, identity: dict[str, Any]) -> bool:
    return isinstance(snapshot, dict) and all(
        n in snapshot and snapshot[n] == v for n, v in identity.items()
    )


def _valid_linked_release(key: str, release: dict[str, Any]) -> bool:
    return (
        release.get("operation") == "release"
        and release.get("confirmation_receipt_key") == key
        and valid_confirmation(
            key, {**release, "operation": "external-stop-confirmation"}
        )
    )


def _inspect(
    request: RecoveryRequest,
    active: ActiveWorktree,
    state: RunState,
    workspace: ClaimWorkspace,
    cwd: Path,
) -> tuple[dict[str, Any], str | None, bool]:
    diagnostics, problem = inspect_claim(
        active, state, workspace, cwd, restore_marker=False, external=True
    )
    if problem:
        return diagnostics, problem, True
    retain, completion_reason = completion_retention(active, state)
    key = confirmation_key(active)
    if key in state.recovery_receipts and not matching_confirmation(
        state, active, workspace.repository_identity
    ):
        return diagnostics, "external_confirmation_invalid", retain
    runtime, reason = external_execution.read_runtime(active, workspace)
    diagnostics.update(
        runtime_state=runtime,
        stop_evidence_source="provider"
        if runtime == "stopped"
        else "operator"
        if runtime == "unknown"
        else None,
        provider_observation_reason=reason,
        completion_decision=completion_reason,
    )
    return (
        diagnostics,
        "external_execution_running" if runtime == "running" else None,
        retain,
    )


def recover_external(
    request: RecoveryRequest, workspace: ClaimWorkspace, cwd: Path
) -> RecoveryResult:
    primary, checkout = _resolve_checkout_roots(cwd)
    if primary != checkout:
        raise ValueError("run recovery from the primary checkout")
    state = load_run_state_readonly(workspace.run_state_path)
    selected = _select(request, state, workspace)
    if selected is None:
        return _absent(request, state, workspace)
    key, active = selected
    diagnostics, problem, retain = _inspect(request, active, state, workspace, cwd)
    if problem:
        return RecoveryResult(False, request.issue_number, "held", problem, diagnostics)
    if not request.apply:
        action = "would_external_stop_confirm_" + (
            "active_retained" if retain else "release"
        )
        return RecoveryResult(
            True,
            request.issue_number,
            action,
            "verified external generation; work retained",
            diagnostics,
        )
    return _apply_external(request, workspace, cwd, active)


def _apply_external(
    request: RecoveryRequest,
    workspace: ClaimWorkspace,
    cwd: Path,
    active: ActiveWorktree,
) -> RecoveryResult:
    target = registered_claim_path(active, workspace.run_state_path)
    with (
        FileLock(claim_lock_path(target), timeout=request.timeout_seconds)
        if active.core.worktree_path
        else nullcontext()
    ):
        fresh = load_run_state_readonly(workspace.run_state_path)
        selected = _select(request, fresh, workspace)
        if selected is None or execution_identity(selected[1]) != execution_identity(
            active
        ):
            raise ValueError("external execution generation changed")
        diagnostics, problem, retain = _inspect(
            request, selected[1], fresh, workspace, cwd
        )
        if problem:
            return RecoveryResult(
                False, request.issue_number, "held", problem, diagnostics
            )
        return _save(
            request,
            workspace,
            selected[0],
            selected[1],
            fresh,
            retain,
            diagnostics,
            cwd,
        )


def _save(
    request: RecoveryRequest,
    workspace: ClaimWorkspace,
    key: str,
    active: ActiveWorktree,
    state: RunState,
    retain: bool,
    diagnostics: dict[str, Any],
    cwd: Path,
) -> RecoveryResult:
    state, active, retain = _refresh_after_observation(
        request, workspace, active, cwd, diagnostics
    )
    confirmation = confirmation_key(active)
    existed = confirmation in state.recovery_receipts
    if retain and existed:
        return RecoveryResult(
            True,
            request.issue_number,
            "already_external_stop_confirmed_active_retained",
            "first confirmation retained",
            diagnostics,
        )
    raw = json.loads(workspace.run_state_path.read_text(encoding="utf-8"))
    receipts = raw.setdefault("recovery_receipts", {})
    receipts.setdefault(confirmation, confirmation_record(active, request.reason or ""))
    if not retain:
        release_key = f"{workspace.repository_identity}::{request.issue_number}::{active.claim.claim_id}::release"
        if release_key in receipts and not _valid_linked_release(
            confirmation, receipts[release_key]
        ):
            raise ValueError("external_release_receipt_invalid")
        release = dict(confirmation_record(active, request.reason or ""))
        release.update(operation="release", confirmation_receipt_key=confirmation)
        receipts.setdefault(release_key, release)
        del raw["active_worktrees"][key]
    write_json_atomic(workspace.run_state_path, raw)
    action = "external_stop_confirmed_" + ("active_retained" if retain else "released")
    return RecoveryResult(
        True,
        request.issue_number,
        action,
        "worktree, branch, marker and GitHub retained",
        diagnostics,
    )


def _refresh_after_observation(
    request: RecoveryRequest,
    workspace: ClaimWorkspace,
    active: ActiveWorktree,
    cwd: Path,
    diagnostics: dict[str, Any],
) -> tuple[RunState, ActiveWorktree, bool]:
    state = load_run_state_readonly(workspace.run_state_path)
    selected = _select(request, state, workspace)
    if selected is None or execution_identity(selected[1]) != execution_identity(
        active
    ):
        raise ValueError("external execution generation changed")
    current = selected[1]
    _, problem = inspect_claim(
        current, state, workspace, cwd, restore_marker=False, external=True
    )
    if problem:
        raise ValueError(problem)
    retain, completion_reason = completion_retention(current, state)
    diagnostics["completion_decision"] = completion_reason
    active = current
    confirmation = confirmation_key(active)
    if confirmation in state.recovery_receipts and not matching_confirmation(
        state, active, workspace.repository_identity
    ):
        raise ValueError("external_confirmation_invalid")
    return state, active, retain
