"""Operator recovery retains real worktrees and unrelated durable state."""

import json
import os
from dataclasses import replace

from orchestune.infra.process_utils import run_state_lock
from orchestune.ledger.run_state import (
    TaskReclaimRecord,
    load_run_state_readonly,
    save_run_state,
)
from orchestune.worktree_ops.claim_marker import claim_marker_path
from tests.dispatch_test_support import replace_flat

pytest_plugins = ["tests.test_local_claim_identity"]


def test_preview_release_and_replay_preserve_work(local_claim):
    from orchestune.recovery.contracts import RecoveryRequest
    from orchestune.recovery.service import recover_claim

    workspace, active, worktree = local_claim
    (worktree / "file").write_text("unsaved work")
    state = load_run_state_readonly(workspace.run_state_path)
    state.active_worktrees["8"] = replace_flat(active, issue_number=8, claim_id="other")
    state.task_reclaim_counts[7] = TaskReclaimRecord()
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)
    before = workspace.run_state_path.read_bytes()
    preview = recover_claim(RecoveryRequest(7), cwd=workspace.repository_root)
    assert preview.success and preview.action == "would_release"
    assert workspace.run_state_path.read_bytes() == before
    request = RecoveryRequest(
        7, claim_id=active.claim.claim_id, reason="worker stopped", apply=True
    )
    result = recover_claim(request, cwd=workspace.repository_root)
    assert result.success and result.action == "released"
    state = load_run_state_readonly(workspace.run_state_path)
    assert "7" not in state.active_worktrees and "8" in state.active_worktrees
    assert 7 in state.task_reclaim_counts
    assert state.recovery_receipts
    assert (worktree / "file").read_text() == "unsaved work"
    assert (
        recover_claim(request, cwd=workspace.repository_root).action
        == "already_released"
    )


def test_changed_generation_and_live_process_hold(local_claim):
    from orchestune.recovery.contracts import RecoveryRequest
    from orchestune.recovery.service import recover_claim

    workspace, active, worktree = local_claim
    request = RecoveryRequest(
        7, claim_id="old-generation", reason="stopped", apply=True
    )
    assert not recover_claim(request, cwd=workspace.repository_root).success
    active.launch = replace(active.launch, pid=os.getpid())
    state = load_run_state_readonly(workspace.run_state_path)
    state.active_worktrees["7"] = active
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)
    request = replace(request, claim_id=active.claim.claim_id)
    assert not recover_claim(request, cwd=workspace.repository_root).success


def test_missing_marker_can_be_restored_or_released(local_claim):
    from orchestune.recovery.contracts import RecoveryRequest
    from orchestune.recovery.service import recover_claim

    workspace, active, worktree = local_claim
    claim_marker_path(worktree).unlink()
    request = RecoveryRequest(
        7,
        claim_id=active.claim.claim_id,
        reason="missing marker",
        apply=True,
        restore_marker=True,
    )
    assert (
        recover_claim(request, cwd=workspace.repository_root).action
        == "marker_restored"
    )
    assert claim_marker_path(worktree).exists()


def test_pending_publication_is_held(local_claim):
    from orchestune.recovery.contracts import RecoveryRequest
    from orchestune.recovery.service import recover_claim

    workspace, active, _ = local_claim
    state = load_run_state_readonly(workspace.run_state_path)
    state.active_worktrees["7"].completion = replace(
        state.active_worktrees["7"].completion, completion_id="pending"
    )
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)
    result = recover_claim(
        RecoveryRequest(
            7, claim_id=active.claim.claim_id, reason="stopped", apply=True
        ),
        cwd=workspace.repository_root,
    )
    assert not result.success and "completion" in result.reason


def test_atomic_release_preserves_extensions_and_retries(local_claim, monkeypatch):
    from orchestune.recovery import service
    from orchestune.recovery.contracts import RecoveryRequest

    workspace, active, _ = local_claim
    raw = json.loads(workspace.run_state_path.read_text())
    raw["operator_extension"] = {"opaque": [1, 2, 3]}
    workspace.run_state_path.write_text(json.dumps(raw))
    before = workspace.run_state_path.read_bytes()
    request = RecoveryRequest(
        7, claim_id=active.claim.claim_id, reason="stopped", apply=True
    )
    write = service.write_json_atomic
    monkeypatch.setattr(
        service,
        "write_json_atomic",
        lambda *_: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert not service.recover_claim(request, cwd=workspace.repository_root).success
    assert workspace.run_state_path.read_bytes() == before
    monkeypatch.setattr(service, "write_json_atomic", write)
    assert service.recover_claim(request, cwd=workspace.repository_root).success
    assert (
        json.loads(workspace.run_state_path.read_text())["operator_extension"]
        == raw["operator_extension"]
    )


def test_dispatch_release_and_old_preview_hold(local_claim):
    from orchestune.ledger.run_state import claim_was_released
    from orchestune.recovery.contracts import RecoveryRequest
    from orchestune.recovery.service import recover_claim

    workspace, active, _ = local_claim
    active.claim = replace(active.claim, owner_kind="dispatch")
    state = load_run_state_readonly(workspace.run_state_path)
    state.active_worktrees["7"] = active
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)
    assert recover_claim(RecoveryRequest(7), cwd=workspace.repository_root).success
    request = RecoveryRequest(
        7, claim_id=active.claim.claim_id, reason="stopped", apply=True
    )
    assert recover_claim(request, cwd=workspace.repository_root).success
    state = load_run_state_readonly(workspace.run_state_path)
    assert claim_was_released(state, 7, active.claim.claim_id)
    state.active_worktrees["7"] = replace_flat(active, claim_id="new-generation")
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)
    assert not recover_claim(request, cwd=workspace.repository_root).success


def test_unprepared_claim_reports_no_worktree(local_claim):
    from orchestune.recovery.contracts import RecoveryRequest
    from orchestune.recovery.service import recover_claim

    workspace, active, _ = local_claim
    state = load_run_state_readonly(workspace.run_state_path)
    state.active_worktrees["7"] = replace_flat(
        active, worktree_path="", claim_stage="reserved"
    )
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)
    result = recover_claim(RecoveryRequest(7), cwd=workspace.repository_root)
    assert result.success
    assert result.diagnostics["worktree_status"] == "absent"


def test_release_receipt_keeps_the_flat_active_snapshot_shape(local_claim):
    """The persisted receipt stays the pre-cutover flat ``asdict`` shape."""
    from orchestune.ledger.active_codec import _ACTIVE_FIELD_NAMES
    from orchestune.recovery.service import _legacy_active_snapshot

    _, active, _ = local_claim

    snapshot = _legacy_active_snapshot(active)

    # #1270: launch attribution is omitted while null, like before its introduction.
    attribution = {"launch_target", "launch_log_path", "launch_log_offset"}
    assert tuple(snapshot) == tuple(
        name for name in _ACTIVE_FIELD_NAMES if name not in attribution
    )
    assert snapshot["completion_policy_config"] is None
    assert snapshot["claim_id"] == active.claim.claim_id
    assert snapshot["declared_footprint"] == list(active.core.declared_footprint)
