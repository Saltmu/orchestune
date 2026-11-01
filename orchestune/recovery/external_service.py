"""External operator recovery with fresh checks and one atomic ledger write."""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import orchestune.claim.workspace as claim_workspace
from orchestune.claim.inspection import inspect_claim
from orchestune.claim.local_identity import registered_claim_path
from orchestune.claim.workspace import ClaimWorkspace
from orchestune.infra.json_state import write_json_atomic
from orchestune.infra.process_utils import FileLock
from orchestune.ledger.external_stop_receipts import (
    confirmation_key,
    confirmation_record,
    execution_identity,
    matching_confirmation,
    valid_confirmation,
)
from orchestune.ledger.recovery_receipts import (
    ExternalStopConfirmation,
    RecoveryReceiptValue,
    ReleaseReceipt,
)
from orchestune.ledger.run_state import (
    ActiveWorktree,
    RunState,
    load_run_state_readonly,
)
from orchestune.recovery import external_execution
from orchestune.recovery.contracts import RecoveryRequest, RecoveryResult
from orchestune.recovery.external_completion import completion_retention
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
        identity.repository_id != workspace.repository_identity
        or identity.claim_id != request.claim_id
        or identity.external_id != request.external_id
        or identity.launch_attempt_id != request.launch_attempt_id
    ):
        raise ValueError("external execution generation changed")
    receipts = _requested_confirmations(request, state, workspace)
    if receipts and (
        len(receipts) != 1
        or receipts[0][0] != confirmation_key(active)
        or receipts[0][1].execution_identity != identity
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
    release: RecoveryReceiptValue | None = state.recovery_receipts.get(
        f"{workspace.repository_identity}::{request.issue_number}::{request.claim_id}::release",
        None,
    )
    released = (
        isinstance(release, ReleaseReceipt)
        and release.confirmation_receipt_key == key
        and release.repository_id == workspace.repository_identity
        and release.issue_number == request.issue_number
        and release.claim_id == request.claim_id
        and release.execution_identity == confirmation.execution_identity
    )
    if (
        isinstance(release, ReleaseReceipt)
        and release.confirmation_receipt_key == key
        and not _valid_linked_release(key, release)
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
) -> list[tuple[str, ExternalStopConfirmation]]:
    matches = [
        (k, r)
        for k, r in state.recovery_receipts.items()
        if isinstance(r, ExternalStopConfirmation)
        and all(
            actual == expected
            for actual, expected in (
                (r.repository_id, workspace.repository_identity),
                (r.issue_number, request.issue_number),
                (r.claim_id, request.claim_id),
                (r.external_id, request.external_id),
                (r.launch_attempt_id, request.launch_attempt_id),
            )
        )
    ]
    return matches


def _valid_linked_release(key: str, release: Any) -> bool:
    return (
        isinstance(release, ReleaseReceipt)
        and release.confirmation_receipt_key == key
        and release.execution_identity is not None
        and release.active.value.claim.claim_id == release.claim_id
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
    primary, checkout = claim_workspace.resolve_checkout_roots(cwd)
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
    confirmation_value = confirmation_record(active, request.reason or "")
    receipts.setdefault(confirmation, confirmation_value.to_dict())
    if not retain:
        release_key = f"{workspace.repository_identity}::{request.issue_number}::{active.claim.claim_id}::release"
        if release_key in receipts and not _valid_linked_release(
            confirmation, receipts[release_key]
        ):
            raise ValueError("external_release_receipt_invalid")
        release = _linked_release_record(confirmation_value, confirmation)
        receipts.setdefault(release_key, release.to_dict())
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


def _linked_release_record(
    confirmation_value: ExternalStopConfirmation, confirmation: str
) -> ReleaseReceipt:
    return ReleaseReceipt(
        schema_version=1,
        repository_id=confirmation_value.repository_id,
        issue_number=confirmation_value.issue_number,
        claim_id=confirmation_value.claim_id,
        operation="release",
        reason=confirmation_value.reason,
        recorded_at=confirmation_value.recorded_at,
        worktree_action=confirmation_value.worktree_action,
        active=confirmation_value.active,
        confirmation_receipt_key=confirmation,
        source=confirmation_value.source,
        confirmed_external_stopped=confirmation_value.confirmed_external_stopped,
        launch_attempt_id=confirmation_value.launch_attempt_id,
        external_id=confirmation_value.external_id,
        execution_identity=confirmation_value.execution_identity,
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
