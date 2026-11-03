"""Generation-guarded recovery with retained work and atomic durable receipts."""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from orchestune.claim.inspection import inspect_claim
from orchestune.claim.local_identity import registered_claim_path
from orchestune.claim.workspace import ClaimWorkspace, resolve_claim_workspace
from orchestune.forge import Forge
from orchestune.infra.git_cli import inspect_worktree_status
from orchestune.infra.json_state import write_json_atomic
from orchestune.infra.process_utils import FileLock, run_state_lock
from orchestune.ledger.active_codec import decode_active_worktree
from orchestune.ledger.active_lifecycle import has_completion_reservation
from orchestune.ledger.recovery_receipts import (
    RELEASE_OPERATION,
    record_recovery_in_raw,
    recovery_receipt_key,
)
from orchestune.ledger.run_state import (
    ActiveWorktree,
    RunState,
    load_run_state_readonly,
)
from orchestune.recovery.blocked_restart import restart_blocked
from orchestune.recovery.contracts import RecoveryRequest, RecoveryResult
from orchestune.recovery.external_service import recover_external
from orchestune.recovery.release_review import (
    apply_release_review_label,
    verify_release_review_target,
)
from orchestune.worktree_ops.claim_marker import claim_lock_path, write_claim_marker


def _release_key(workspace: ClaimWorkspace, request: RecoveryRequest) -> str:
    return recovery_receipt_key(
        workspace.repository_identity,
        request.issue_number,
        request.claim_id,
        RELEASE_OPERATION,
    )


def _already_released_result(
    request: RecoveryRequest,
    workspace: ClaimWorkspace,
    receipt_key: str,
) -> RecoveryResult:
    """#1655: report who released the claim and whether its worktree still exists."""
    try:
        raw = json.loads(workspace.run_state_path.read_text(encoding="utf-8"))
        receipt = raw["recovery_receipts"][receipt_key]
        released_active = decode_active_worktree(receipt["active"])
        worktree = released_active.core.worktree_path
        diagnostics: dict[str, Any] = {
            "release_operation": RELEASE_OPERATION,
            "release_reason": receipt.get("reason"),
            "released_at": receipt.get("recorded_at"),
            "worktree": worktree,
            "worktree_action": receipt.get("worktree_action"),
            "worktree_status": (
                "absent"
                if not worktree or not Path(worktree).is_dir()
                else inspect_worktree_status(worktree).value
            ),
        }
    except (KeyError, TypeError, ValueError, OSError):
        diagnostics = {"release_reason": "unreadable"}
    return RecoveryResult(
        True,
        request.issue_number,
        "already_released",
        "saved release receipt",
        diagnostics,
    )


def _handle_existing_receipt(
    request: RecoveryRequest,
    forge: Forge | None,
    workspace: ClaimWorkspace,
    receipt_key: str,
) -> RecoveryResult:
    if request.release_to_review:
        assert request.claim_id is not None
        if forge is None:
            raise ValueError("Forge is required for release-to-review")
        _issue, old_labels = verify_release_review_target(
            forge, request.issue_number, request.claim_id
        )
        if not request.apply:
            return RecoveryResult(
                True,
                request.issue_number,
                "would_release_to_review",
                "verified release receipt; label will transition to status:blocked-human-review",
            )
        apply_release_review_label(forge, request.issue_number, old_labels)
        return RecoveryResult(
            True,
            request.issue_number,
            "released_to_review",
            "status transitioned to status:blocked-human-review; release receipt verified; worktree retained",
        )
    return _already_released_result(request, workspace, receipt_key)


def _validate_release_review_active(
    active: ActiveWorktree, request: RecoveryRequest, forge: Forge | None
) -> tuple[RecoveryResult | None, tuple[str, ...]]:
    if has_completion_reservation(active):
        return (
            RecoveryResult(
                False, request.issue_number, "held", "claim is undergoing completion"
            ),
            (),
        )
    if active.launch and active.launch.external_id:
        return (
            RecoveryResult(
                False,
                request.issue_number,
                "held",
                "external execution claims must use --confirm-external-stopped",
            ),
            (),
        )
    assert active.claim.claim_id is not None
    if forge is None:
        raise ValueError("Forge is required for release-to-review")
    _issue, old = verify_release_review_target(
        forge, request.issue_number, active.claim.claim_id
    )
    return None, old


def _find_active_or_held(
    request: RecoveryRequest,
    state: RunState,
    workspace: ClaimWorkspace,
    forge: Forge | None,
) -> tuple[str, ActiveWorktree] | RecoveryResult:
    matches = [
        (key, active)
        for key, active in state.active_worktrees.items()
        if active.core.issue_number == request.issue_number
    ]
    receipt_key = _release_key(workspace, request)
    if not matches:
        if request.claim_id and receipt_key in state.recovery_receipts:
            return _handle_existing_receipt(request, forge, workspace, receipt_key)
        return RecoveryResult(
            False,
            request.issue_number,
            "held",
            f"no active claim; for an eligible orphan PR, preview orchestune claim {request.issue_number} --adopt-pr <PR> --no-apply",
        )
    if len(matches) != 1:
        return RecoveryResult(
            False, request.issue_number, "held", "ambiguous active claims"
        )
    key, active = matches[0]
    if request.claim_id is not None and request.claim_id != active.claim.claim_id:
        return RecoveryResult(
            False, request.issue_number, "held", "claim generation changed"
        )
    return key, active


def _recover_release_to_review(
    request: RecoveryRequest,
    workspace: ClaimWorkspace,
    cwd: Path,
    key: str,
    active: ActiveWorktree,
    state: RunState,
    forge: Forge | None,
    diagnostics: dict[str, Any],
) -> RecoveryResult:
    validation_failure, old_labels = _validate_release_review_active(
        active, request, forge
    )
    if validation_failure is not None:
        return validation_failure
    if not request.apply:
        return RecoveryResult(
            True,
            request.issue_number,
            "would_release_to_review",
            "verified local claim; worktree and artifacts will be retained; label will transition to status:blocked-human-review",
            diagnostics,
        )
    return _apply_recovery(
        request,
        workspace,
        cwd,
        key,
        active,
        state,
        forge=forge,
        old_labels=old_labels,
    )


def _recover(
    request: RecoveryRequest,
    workspace: ClaimWorkspace,
    cwd: Path,
    forge: Forge | None = None,
) -> RecoveryResult:
    if request.restart_blocked:
        return restart_blocked(request, workspace, cwd, forge)
    if request.confirm_external_stopped:
        return recover_external(request, workspace, cwd)
    state = load_run_state_readonly(workspace.run_state_path)
    active_result = _find_active_or_held(request, state, workspace, forge)
    if isinstance(active_result, RecoveryResult):
        return active_result
    key, active = active_result

    diagnostics, problem = inspect_claim(
        active, state, workspace, cwd, restore_marker=request.restore_marker
    )
    if problem:
        return RecoveryResult(False, request.issue_number, "held", problem, diagnostics)
    if request.release_to_review:
        return _recover_release_to_review(
            request, workspace, cwd, key, active, state, forge, diagnostics
        )

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


def _apply_operation_side_effects(
    request: RecoveryRequest,
    target: Path,
    active: ActiveWorktree,
    forge: Forge | None,
    old_labels: tuple[str, ...] | None = None,
) -> None:
    if request.release_to_review:
        assert active.claim.claim_id is not None
        if forge is None:
            raise ValueError("Forge is required for release-to-review")
        if old_labels is None:
            _issue, old_labels = verify_release_review_target(
                forge, request.issue_number, active.claim.claim_id
            )
        # Note: Label transition occurs before saving receipt; if saving receipt fails,
        # retry is idempotent since verify_release_review_target permits status:blocked-human-review.
        apply_release_review_label(forge, request.issue_number, old_labels)
    elif request.restore_marker:
        assert active.claim.claim_id is not None
        write_claim_marker(
            target,
            claim_id=active.claim.claim_id,
            branch=active.core.branch,
            base_sha=active.claim.base_sha,
            branch_created=False,
        )


def _apply_recovery(
    request: RecoveryRequest,
    workspace: ClaimWorkspace,
    cwd: Path,
    key: str,
    active: ActiveWorktree,
    state: RunState,
    *,
    forge: Forge | None = None,
    old_labels: tuple[str, ...] | None = None,
) -> RecoveryResult:
    operation = "restore-marker" if request.restore_marker else "release"
    target = registered_claim_path(active, workspace.run_state_path)
    with (
        FileLock(claim_lock_path(target), timeout=request.timeout_seconds)
        if active.core.worktree_path
        else nullcontext()
    ):
        diagnostics, problem = inspect_claim(
            active, state, workspace, cwd, restore_marker=request.restore_marker
        )
        if problem:
            return RecoveryResult(
                False, request.issue_number, "held", problem, diagnostics
            )
        _apply_operation_side_effects(request, target, active, forge, old_labels)
        _save_recovery(request, workspace, key, active, operation)
    status = (
        "released_to_review"
        if request.release_to_review
        else ("marker_restored" if request.restore_marker else "released")
    )
    message = (
        "status transitioned to status:blocked-human-review; active claim released; worktree retained"
        if request.release_to_review
        else "worktree, branch and GitHub state retained"
    )
    return RecoveryResult(
        True,
        request.issue_number,
        status,
        message,
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
    record_recovery_in_raw(
        raw,
        key,
        active,
        repository_id=workspace.repository_identity,
        issue_number=request.issue_number,
        operation=operation,
        reason=request.reason,
    )
    # Preserve all other records and extensions exactly; no retention pruning.
    write_json_atomic(workspace.run_state_path, raw)


def recover_claim(
    request: RecoveryRequest,
    *,
    cwd: str | Path | None = None,
    forge: Forge | None = None,
) -> RecoveryResult:
    try:
        request.validate()
        workspace = resolve_claim_workspace(cwd, explicit_state_path=request.state_path)
        current = Path(cwd or Path.cwd()).resolve()
        if not request.apply:
            return _recover(request, workspace, current, forge)
        with run_state_lock(workspace.lock_path, timeout=request.timeout_seconds):
            return _recover(request, workspace, current, forge)
    except (OSError, ValueError, RuntimeError) as error:
        return RecoveryResult(False, request.issue_number, "held", str(error))
