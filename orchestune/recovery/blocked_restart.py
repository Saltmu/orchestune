"""Human-authorized restart of completed blocked local claim generations."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from orchestune.claim.inspection import inspect_claim
from orchestune.claim.issue_metadata import update_issue_claim_metadata
from orchestune.claim.local_identity import (
    registered_claim_path,
    validate_claim_worktree,
)
from orchestune.claim.ownership import with_claim
from orchestune.claim.workspace import ClaimWorkspace
from orchestune.complete.journal import with_completion
from orchestune.forge import GitHubForge
from orchestune.infra.json_state import write_json_atomic
from orchestune.infra.process_utils import FileLock
from orchestune.labels import StatusLabel
from orchestune.ledger.active_codec import (
    decode_active_worktree,
    encode_active_worktree,
)
from orchestune.ledger.active_records import ActiveCompletionJournal
from orchestune.ledger.completion_reservations import completion_handoff_matches_active
from orchestune.ledger.run_state import (
    ActiveWorktree,
    RunState,
    load_run_state_readonly,
)
from orchestune.ledger.status_labels import (
    PRIMARY_STATUS_LABELS,
    TERMINAL_ESCALATION_LABELS,
)
from orchestune.recovery.contracts import RecoveryRequest, RecoveryResult
from orchestune.worktree_ops.claim_marker import (
    claim_lock_path,
    read_claim_marker,
    write_claim_marker,
)


def _receipt_key(request: RecoveryRequest, workspace: ClaimWorkspace) -> str:
    return f"{workspace.repository_identity}::{request.issue_number}::{request.claim_id}::restart-blocked"


def _approved_issue(forge: Any, issue_number: int) -> Any:
    issue = forge.get_issue(issue_number)
    labels = set(forge.get_issue_labels(issue_number))
    if issue is None or forge.get_issue_state(issue_number).upper() != "OPEN":
        raise ValueError("Restart requires an open Issue")
    if labels & set(PRIMARY_STATUS_LABELS) != {StatusLabel.IN_PROGRESS} or labels & set(
        TERMINAL_ESCALATION_LABELS
    ):
        raise ValueError(
            "Human must resolve the blocker and set status:in-progress first"
        )
    return issue


def _publish_metadata(forge: Any, active: ActiveWorktree) -> None:
    issue = forge.get_issue(active.core.issue_number)
    if issue is None:
        raise ValueError("Restart Issue metadata is unavailable")
    body = update_issue_claim_metadata(
        issue.body,
        active.claim.owner_kind,
        active.claim.claim_id or "",
        active.claim.reservation_kind,
    )
    if body != issue.body:
        _approved_issue(forge, active.core.issue_number)
        forge.update_issue_body(issue.number, body)


def _selected(
    request: RecoveryRequest, state: RunState, workspace: ClaimWorkspace, cwd: Path
) -> ActiveWorktree:
    active = state.active_worktrees.get(str(request.issue_number))
    if active is None or active.claim.claim_id != request.claim_id:
        raise ValueError(
            f"Claim generation changed or active claim is absent; for an eligible orphan PR, preview orchestune claim {request.issue_number} --adopt-pr <PR> --no-apply"
        )
    if active.claim.owner_kind != "interactive" or active.launch.external_id:
        raise ValueError("Restart supports only interactive local claims")
    _, problem = inspect_claim(active, state, workspace, cwd, restore_marker=False)
    if problem:
        raise ValueError(problem)
    validate_claim_worktree(active, workspace.run_state_path)
    if (
        active.completion.completion_result != "blocked"
        or not completion_handoff_matches_active(state, active)
    ):
        raise ValueError("Restart requires a verified handed-off blocked completion")
    return active


def _replayed(
    request: RecoveryRequest,
    state: RunState,
    workspace: ClaimWorkspace,
    cwd: Path,
    forge: Any,
) -> RecoveryResult | None:
    receipt = state.recovery_receipts.get(_receipt_key(request, workspace))
    if receipt is None:
        return None
    if receipt.get("phase") not in {"prepared", "restarted"}:
        raise ValueError("Invalid restart receipt phase")
    if any(
        receipt.get(name) != expected
        for name, expected in (
            ("schema_version", 1),
            ("repository_id", workspace.repository_identity),
            ("issue_number", request.issue_number),
            ("claim_id", request.claim_id),
            ("operation", "restart-blocked"),
        )
    ):
        raise ValueError("Invalid restart receipt identity")
    active = state.active_worktrees.get(str(request.issue_number))
    if receipt.get("phase") == "prepared" and active is not None:
        return _resume_prepared(request, receipt, active, state, workspace, cwd, forge)
    if (
        receipt.get("reason") != request.reason
        or active is None
        or active.claim.claim_id != receipt.get("new_claim_id")
    ):
        raise ValueError("Restart receipt differs or claim generation changed")
    validate_claim_worktree(active, workspace.run_state_path)
    if request.apply:
        _publish_metadata(forge, active)
    return RecoveryResult(
        True,
        request.issue_number,
        "already_restarted_blocked",
        "Saved restart receipt",
        {"claim_id": active.claim.claim_id, "worktree": active.core.worktree_path},
    )


def _resume_prepared(
    request: RecoveryRequest,
    receipt: dict[str, Any],
    active: ActiveWorktree,
    state: RunState,
    workspace: ClaimWorkspace,
    cwd: Path,
    forge: Any,
) -> RecoveryResult | None:
    if (
        receipt.get("reason") != request.reason
        or active.claim.claim_id != request.claim_id
        or active.claim.owner_kind != "interactive"
        or active.launch.external_id
        or active.completion.completion_result != "blocked"
        or not completion_handoff_matches_active(state, active)
    ):
        raise ValueError("Prepared restart source differs")
    target = validate_claim_worktree(
        active, workspace.run_state_path, require_marker=False
    )
    marker = read_claim_marker(target)
    if (
        not marker
        or marker.get("claim_id") not in {request.claim_id, receipt.get("new_claim_id")}
        or marker.get("branch") != active.core.branch
        or marker.get("base_sha") != active.claim.base_sha
    ):
        raise ValueError("Prepared restart marker differs")
    _, problem = inspect_claim(active, state, workspace, cwd, restore_marker=False)
    if problem not in {None, "claim marker belongs to another generation"}:
        raise ValueError(problem)
    raw = json.loads(workspace.run_state_path.read_text(encoding="utf-8"))
    if raw["active_worktrees"][str(request.issue_number)] != receipt.get("active"):
        raise ValueError("Prepared restart active snapshot differs")
    _planned_replacement(active, receipt)
    _approved_issue(forge, request.issue_number)
    if not request.apply:
        return RecoveryResult(
            True,
            request.issue_number,
            "would_resume_blocked_restart",
            "Prepared restart will be reconciled without losing work",
        )
    with FileLock(claim_lock_path(target), timeout=request.timeout_seconds):
        _write_marker(target, active, bool(marker.get("branch_created", False)))
    return None


def _replacement(active: ActiveWorktree) -> ActiveWorktree:
    restarted = with_claim(
        active,
        replace(
            active.claim,
            claim_id=f"claim-{uuid4().hex}",
            claimed_at=datetime.now(UTC).timestamp(),
        ),
    )
    return with_completion(
        restarted,
        ActiveCompletionJournal(
            completion_policy_config=active.completion.completion_policy_config
        ),
    )


def _write_marker(target: Path, active: ActiveWorktree, branch_created: bool) -> None:
    write_claim_marker(
        target,
        claim_id=active.claim.claim_id or "",
        branch=active.core.branch,
        base_sha=active.claim.base_sha,
        branch_created=branch_created,
    )


def _persist(path: Path, raw: dict[str, Any]) -> None:
    try:
        write_json_atomic(path, raw)
    except OSError:
        if json.loads(path.read_text(encoding="utf-8")) != raw:
            raise


def _save_restart(
    request: RecoveryRequest, workspace: ClaimWorkspace, active: ActiveWorktree
) -> ActiveWorktree:
    target = registered_claim_path(active, workspace.run_state_path)
    marker = read_claim_marker(target)
    assert marker is not None
    raw = json.loads(workspace.run_state_path.read_text(encoding="utf-8"))
    original = dict(raw["active_worktrees"][str(request.issue_number)])
    pending = raw.get("recovery_receipts", {}).get(_receipt_key(request, workspace))
    replacement = (
        _planned_replacement(active, pending) if pending else _replacement(active)
    )
    raw.setdefault("recovery_receipts", {})[_receipt_key(request, workspace)] = {
        "schema_version": 1,
        "repository_id": workspace.repository_identity,
        "issue_number": request.issue_number,
        "claim_id": request.claim_id,
        "operation": "restart-blocked",
        "phase": "prepared",
        "reason": request.reason,
        "new_claim_id": replacement.claim.claim_id,
        "recorded_at": pending["recorded_at"]
        if pending
        else datetime.now(UTC).isoformat(),
        "worktree_action": "retain",
        "active": original,
        "replacement": encode_active_worktree(replacement),
    }
    _persist(workspace.run_state_path, raw)
    _write_marker(target, replacement, bool(marker.get("branch_created", False)))
    raw["active_worktrees"][str(request.issue_number)].update(
        encode_active_worktree(replacement)
    )
    raw["recovery_receipts"][_receipt_key(request, workspace)]["phase"] = "restarted"
    try:
        _persist(workspace.run_state_path, raw)
    except OSError:
        persisted = load_run_state_readonly(workspace.run_state_path)
        current = persisted.active_worktrees.get(str(request.issue_number))
        if current is None or current.claim.claim_id != replacement.claim.claim_id:
            _write_marker(target, active, bool(marker.get("branch_created", False)))
            raise
    return replacement


def _planned_replacement(
    active: ActiveWorktree, receipt: dict[str, Any]
) -> ActiveWorktree:
    try:
        replacement = decode_active_worktree(receipt["replacement"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Invalid prepared restart replacement") from error
    expected = with_completion(
        with_claim(
            active,
            replace(
                active.claim,
                claim_id=replacement.claim.claim_id,
                claimed_at=replacement.claim.claimed_at,
            ),
        ),
        ActiveCompletionJournal(
            completion_policy_config=active.completion.completion_policy_config
        ),
    )
    if (
        replacement != expected
        or not replacement.claim.claim_id
        or replacement.claim.claim_id == active.claim.claim_id
        or replacement.claim.claim_id != receipt.get("new_claim_id")
    ):
        raise ValueError("Prepared restart replacement differs")
    return replacement


def restart_blocked(
    request: RecoveryRequest, workspace: ClaimWorkspace, cwd: Path, forge: Any = None
) -> RecoveryResult:
    forge = forge or GitHubForge(timeout_seconds=30)
    state = load_run_state_readonly(workspace.run_state_path)
    replay = _replayed(request, state, workspace, cwd, forge)
    if replay is not None:
        return replay
    active = _selected(request, state, workspace, cwd)
    _approved_issue(forge, request.issue_number)
    diagnostics = {
        "claim_id": active.claim.claim_id,
        "worktree": active.core.worktree_path,
        "completion_id": active.completion.completion_id,
    }
    if not request.apply:
        return RecoveryResult(
            True,
            request.issue_number,
            "would_restart_blocked",
            "Old Outcome and worktree retained; new claim generation will be created",
            diagnostics,
        )
    with FileLock(
        claim_lock_path(registered_claim_path(active, workspace.run_state_path)),
        timeout=request.timeout_seconds,
    ):
        state = load_run_state_readonly(workspace.run_state_path)
        active = _selected(request, state, workspace, cwd)
        _approved_issue(forge, request.issue_number)
        replacement = _save_restart(request, workspace, active)
        _publish_metadata(forge, replacement)
    return RecoveryResult(
        True,
        request.issue_number,
        "restarted_blocked",
        "Old Outcome retained; continue in the same worktree with the new claim generation",
        {**diagnostics, "new_claim_id": replacement.claim.claim_id},
    )
