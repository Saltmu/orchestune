"""Explicit local blocked restart preserves immutable history and real work."""

import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from orchestune.complete.contracts import CompleteRequest
from orchestune.complete.service import complete_task
from orchestune.infra.process_utils import run_state_lock
from orchestune.ledger.completion_records import CompletionReplayReceipt
from orchestune.ledger.run_state import load_run_state_readonly, save_run_state
from orchestune.recovery.contracts import RecoveryRequest
from orchestune.recovery.service import recover_claim
from orchestune.worktree_ops.claim_marker import claim_marker_path, read_claim_marker
from tests.complete_lifecycle_test_support import PublicationForge

pytest_plugins = ["tests.test_local_claim_identity"]


class RestartForge(PublicationForge):
    def __init__(self):
        super().__init__()
        self.body = "## Task\n"

    def get_issue(self, number):
        return SimpleNamespace(number=number, body=self.body)

    def update_issue_body(self, number, body):
        self.body = body


@pytest.fixture
def blocked_claim(local_claim):
    workspace, active, worktree = local_claim
    forge = RestartForge()
    forge.labels = {"status:in-progress"}
    request = CompleteRequest.blocked(
        7, "blocked", claim_id=active.claim.claim_id, worktree_root=worktree
    )
    result = complete_task(request, forge=forge)
    assert result.success, result.failure
    forge.labels = {"status:in-progress"}
    return workspace, active, worktree, forge, request


def restart_request(active, **kwargs):
    return RecoveryRequest(
        7,
        claim_id=active.claim.claim_id,
        reason="operator resolved blocker",
        restart_blocked=True,
        **kwargs,
    )


def test_preview_restart_retry_and_historical_replay(blocked_claim):
    workspace, active, worktree, forge, old_request = blocked_claim
    before = workspace.run_state_path.read_bytes()
    marker = claim_marker_path(worktree).read_bytes()
    preview = recover_claim(
        restart_request(active), cwd=workspace.repository_root, forge=forge
    )
    assert preview.success and preview.action == "would_restart_blocked"
    assert workspace.run_state_path.read_bytes() == before
    assert claim_marker_path(worktree).read_bytes() == marker
    (worktree / "file").write_text("retained work")
    request = restart_request(active, apply=True)
    result = recover_claim(request, cwd=workspace.repository_root, forge=forge)
    assert result.success, result.reason
    state = load_run_state_readonly(workspace.run_state_path)
    current = state.active_worktrees["7"]
    assert current.claim.claim_id != active.claim.claim_id
    assert read_claim_marker(worktree)["claim_id"] == current.claim.claim_id
    assert current.completion.completion_id is None
    assert current.core == active.core
    assert (worktree / "file").read_text() == "retained work"
    original = json.loads(before)
    for field in (
        "completion_journal",
        "completion_reservations",
        "completion_replay_receipts",
    ):
        assert (
            json.loads(workspace.run_state_path.read_text())[field] == original[field]
        )
    assert (
        recover_claim(request, cwd=workspace.repository_root, forge=forge).action
        == "already_restarted_blocked"
    )
    receipt = next(iter(state.completion_replay_receipts.values()))
    assert isinstance(receipt, CompletionReplayReceipt)
    replay = complete_task(
        replace(old_request, completion_id=receipt.record.completion_id), forge=forge
    )
    assert replay.success and replay.result == "blocked"


@pytest.mark.parametrize(
    "problem",
    [
        "blocked-label",
        "closed",
        "dispatch",
        "running",
        "missing-marker",
        "missing-receipt",
    ],
)
def test_restart_rejects_unsafe_or_unapproved_state(blocked_claim, problem):
    workspace, active, worktree, forge, _ = blocked_claim
    state = load_run_state_readonly(workspace.run_state_path)
    current = state.active_worktrees["7"]
    if problem == "blocked-label":
        forge.labels = {"status:blocked"}
    elif problem == "closed":
        forge.state = "CLOSED"
    elif problem == "dispatch":
        current.claim = replace(current.claim, owner_kind="dispatch")
    elif problem == "running":
        current.launch = replace(current.launch, pid=os.getpid())
    elif problem == "missing-marker":
        claim_marker_path(worktree).unlink()
    else:
        state.completion_replay_receipts.clear()
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)
    before = workspace.run_state_path.read_bytes()
    result = recover_claim(
        restart_request(active, apply=True), cwd=workspace.repository_root, forge=forge
    )
    assert not result.success
    assert workspace.run_state_path.read_bytes() == before


def test_restart_write_failure_preserves_marker_and_state(blocked_claim, monkeypatch):
    from orchestune.recovery import blocked_restart

    workspace, active, worktree, forge, _ = blocked_claim
    before = workspace.run_state_path.read_bytes()
    marker = claim_marker_path(worktree).read_bytes()
    monkeypatch.setattr(
        blocked_restart,
        "write_json_atomic",
        lambda *_: (_ for _ in ()).throw(OSError("disk full")),
    )
    result = recover_claim(
        restart_request(active, apply=True), cwd=workspace.repository_root, forge=forge
    )
    assert not result.success
    assert workspace.run_state_path.read_bytes() == before
    assert claim_marker_path(worktree).read_bytes() == marker


def test_restart_preserves_unknown_extensions(blocked_claim):
    workspace, active, _, forge, _ = blocked_claim
    raw = json.loads(workspace.run_state_path.read_text())
    raw["operator_extension"] = {"keep": [1, 2]}
    workspace.run_state_path.write_text(json.dumps(raw))
    result = recover_claim(
        restart_request(active, apply=True), cwd=workspace.repository_root, forge=forge
    )
    assert result.success, result.reason
    assert (
        json.loads(workspace.run_state_path.read_text())["operator_extension"]
        == raw["operator_extension"]
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"restore_marker": True},
        {"confirm_external_stopped": True},
        {"claim_id": None},
        {"reason": " "},
    ],
)
def test_restart_requires_explicit_and_exclusive_request(changes):
    request = replace(
        RecoveryRequest(7, claim_id="claim", reason="resolved", restart_blocked=True),
        **changes,
    )
    with pytest.raises(ValueError):
        request.validate()


def test_metadata_failure_retries_without_another_rotation(blocked_claim, monkeypatch):
    workspace, active, _, forge, _ = blocked_claim
    original = forge.update_issue_body
    monkeypatch.setattr(
        forge,
        "update_issue_body",
        lambda *_: (_ for _ in ()).throw(RuntimeError("network unavailable")),
    )
    request = restart_request(active, apply=True)
    result = recover_claim(request, cwd=workspace.repository_root, forge=forge)
    assert not result.success
    current = load_run_state_readonly(workspace.run_state_path).active_worktrees["7"]
    assert current.claim.claim_id != active.claim.claim_id
    monkeypatch.setattr(forge, "update_issue_body", original)
    retry = recover_claim(request, cwd=workspace.repository_root, forge=forge)
    assert retry.success and retry.action == "already_restarted_blocked"
    assert (
        load_run_state_readonly(workspace.run_state_path)
        .active_worktrees["7"]
        .claim.claim_id
        == current.claim.claim_id
    )
    assert current.claim.claim_id in forge.body


def test_marker_write_failure_keeps_old_claim(blocked_claim, monkeypatch):
    from orchestune.worktree_ops import claim_marker

    workspace, active, worktree, forge, _ = blocked_claim
    marker = claim_marker_path(worktree).read_bytes()
    monkeypatch.setattr(
        claim_marker,
        "write_json_atomic",
        lambda *_: (_ for _ in ()).throw(OSError("marker unavailable")),
    )
    result = recover_claim(
        restart_request(active, apply=True), cwd=workspace.repository_root, forge=forge
    )
    assert not result.success
    state = load_run_state_readonly(workspace.run_state_path)
    assert state.active_worktrees["7"].claim.claim_id == active.claim.claim_id
    assert next(iter(state.recovery_receipts.values()))["phase"] == "prepared"
    assert claim_marker_path(worktree).read_bytes() == marker


def test_state_write_committed_before_error_is_reconciled(blocked_claim, monkeypatch):
    from orchestune.recovery import blocked_restart

    workspace, active, worktree, forge, _ = blocked_claim
    original = blocked_restart.write_json_atomic

    def committed_then_error(path, value):
        original(path, value)
        raise OSError("acknowledgement lost")

    monkeypatch.setattr(blocked_restart, "write_json_atomic", committed_then_error)
    result = recover_claim(
        restart_request(active, apply=True), cwd=workspace.repository_root, forge=forge
    )
    assert result.success, result.reason
    current = load_run_state_readonly(workspace.run_state_path).active_worktrees["7"]
    assert read_claim_marker(worktree)["claim_id"] == current.claim.claim_id
    assert current.claim.claim_id in forge.body


def test_restart_receipt_does_not_allow_stale_generation(blocked_claim):
    workspace, active, _, forge, _ = blocked_claim
    request = restart_request(active, apply=True)
    assert recover_claim(request, cwd=workspace.repository_root, forge=forge).success
    state = load_run_state_readonly(workspace.run_state_path)
    current = state.active_worktrees["7"]
    current.claim = replace(current.claim, claim_id="later-generation")
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)
    before = workspace.run_state_path.read_bytes()
    assert not recover_claim(
        request, cwd=workspace.repository_root, forge=forge
    ).success
    assert workspace.run_state_path.read_bytes() == before


def test_crash_after_marker_update_resumes_prepared_generation(
    blocked_claim, monkeypatch
):
    from orchestune.recovery import blocked_restart

    workspace, active, worktree, forge, _ = blocked_claim
    request = restart_request(active, apply=True)
    original = blocked_restart._persist
    writes = 0

    def interrupt_final_write(path, raw):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise KeyboardInterrupt("simulated process termination")
        original(path, raw)

    monkeypatch.setattr(blocked_restart, "_persist", interrupt_final_write)
    with pytest.raises(KeyboardInterrupt):
        recover_claim(request, cwd=workspace.repository_root, forge=forge)
    state = load_run_state_readonly(workspace.run_state_path)
    receipt = next(iter(state.recovery_receipts.values()))
    assert receipt["phase"] == "prepared"
    assert state.active_worktrees["7"].claim.claim_id == active.claim.claim_id
    assert read_claim_marker(worktree)["claim_id"] == receipt["new_claim_id"]
    before, marker = (
        workspace.run_state_path.read_bytes(),
        claim_marker_path(worktree).read_bytes(),
    )
    preview = recover_claim(
        replace(request, apply=False), cwd=workspace.repository_root, forge=forge
    )
    assert preview.success and preview.action == "would_resume_blocked_restart"
    assert workspace.run_state_path.read_bytes() == before
    assert claim_marker_path(worktree).read_bytes() == marker
    monkeypatch.setattr(blocked_restart, "_persist", original)
    resumed = recover_claim(request, cwd=workspace.repository_root, forge=forge)
    assert resumed.success, resumed.reason
    state = load_run_state_readonly(workspace.run_state_path)
    assert state.active_worktrees["7"].claim.claim_id == receipt["new_claim_id"]
    assert read_claim_marker(worktree)["claim_id"] == receipt["new_claim_id"]
