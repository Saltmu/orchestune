"""External recovery preserves work and atomically distinguishes stop/release."""

import os
from dataclasses import replace

import pytest

from orchestune.infra.process_utils import run_state_lock
from orchestune.ledger.run_state import (
    attempt_was_released,
    claim_was_released,
    load_run_state_readonly,
    save_run_state,
)
from orchestune.recovery.contracts import RecoveryRequest
from orchestune.recovery.service import recover_claim
from tests.dispatch_test_support import replace_flat

pytest_plugins = ["tests.test_local_claim_identity"]


@pytest.fixture
def external_claim(local_claim, monkeypatch):
    from orchestune.recovery import external_execution

    workspace, active, worktree = local_claim
    active = replace_flat(
        active, external_id="run", launch_attempt_id="attempt", launch_phase="launched"
    )
    state = load_run_state_readonly(workspace.run_state_path)
    state.active_worktrees["7"] = active
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)
    monkeypatch.setattr(
        external_execution,
        "read_runtime",
        lambda *_: ("unknown", "provider_unavailable"),
    )
    request = RecoveryRequest(
        7,
        claim_id=active.claim.claim_id,
        external_id="run",
        launch_attempt_id="attempt",
        confirm_external_stopped=True,
        reason="URL: operator verified terminal state",
    )
    return workspace, active, worktree, request


def test_preview_atomic_release_and_idempotent_replay(external_claim):
    workspace, active, worktree, request = external_claim
    before = workspace.run_state_path.read_bytes()
    result = recover_claim(request, cwd=workspace.repository_root)
    assert result.success and result.action == "would_external_stop_confirm_release"
    assert result.diagnostics["stop_evidence_source"] == "operator"
    assert workspace.run_state_path.read_bytes() == before
    result = recover_claim(replace(request, apply=True), cwd=workspace.repository_root)
    assert result.success and result.action == "external_stop_confirmed_released"
    state = load_run_state_readonly(workspace.run_state_path)
    assert not state.active_worktrees and worktree.exists()
    assert claim_was_released(state, 7, active.claim.claim_id)
    assert attempt_was_released(state, 7, "attempt")
    assert len(state.recovery_receipts) == 2
    before = workspace.run_state_path.read_bytes()
    replay = recover_claim(
        replace(request, apply=True, reason="resend"), cwd=workspace.repository_root
    )
    assert replay.action == "already_external_stop_confirmed_released"
    assert workspace.run_state_path.read_bytes() == before


@pytest.mark.parametrize(
    "changes",
    [
        {"claim_id": None},
        {"external_id": None},
        {"reason": "  "},
        {"restore_marker": True},
        {"launch_attempt_id": None},
        {"external_id": "wrong"},
        {"claim_id": "wrong"},
    ],
)
def test_preview_rejects_invalid_arguments_or_generation(external_claim, changes):
    workspace, _, _, request = external_claim
    before = workspace.run_state_path.read_bytes()
    assert not recover_claim(
        replace(request, **changes), cwd=workspace.repository_root
    ).success
    assert workspace.run_state_path.read_bytes() == before


@pytest.mark.parametrize("phase", ["prepared", "launching", "unknown"])
def test_unresolved_launch_is_never_released(external_claim, phase):
    workspace, active, _, request = external_claim
    state = load_run_state_readonly(workspace.run_state_path)
    state.active_worktrees["7"] = replace_flat(active, launch_phase=phase)
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)
    assert not recover_claim(request, cwd=workspace.repository_root).success


@pytest.mark.parametrize(
    "runtime,source,success",
    [
        ("running", None, False),
        ("stopped", "provider", True),
        ("unknown", "operator", True),
    ],
)
def test_fresh_provider_state_wins(
    external_claim, monkeypatch, runtime, source, success
):
    from orchestune.recovery import external_execution

    workspace, _, _, request = external_claim
    monkeypatch.setattr(
        external_execution, "read_runtime", lambda *_: (runtime, "observed")
    )
    result = recover_claim(replace(request, apply=True), cwd=workspace.repository_root)
    assert result.success == success
    assert result.diagnostics.get("stop_evidence_source") == source
    if success:
        receipts = load_run_state_readonly(workspace.run_state_path).recovery_receipts
        confirmation = next(
            r
            for r in receipts.values()
            if r["operation"] == "external-stop-confirmation"
        )
        assert confirmation["source"] == "operator"


def test_atomic_save_failure_keeps_everything(external_claim, monkeypatch):
    from orchestune.recovery import external_service

    workspace, _, _, request = external_claim
    before = workspace.run_state_path.read_bytes()
    monkeypatch.setattr(
        external_service,
        "write_json_atomic",
        lambda *_: (_ for _ in ()).throw(OSError("full")),
    )
    assert not recover_claim(
        replace(request, apply=True), cwd=workspace.repository_root
    ).success
    assert workspace.run_state_path.read_bytes() == before


def test_live_pid_is_held(external_claim):
    workspace, active, _, request = external_claim
    state = load_run_state_readonly(workspace.run_state_path)
    state.active_worktrees["7"] = replace_flat(active, pid=os.getpid())
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)
    assert not recover_claim(request, cwd=workspace.repository_root).success


def test_pending_completion_only_confirms_and_replay_keeps_first_record(external_claim):
    workspace, active, _, request = external_claim
    state = load_run_state_readonly(workspace.run_state_path)
    state.active_worktrees["7"] = replace_flat(active, completion_id="pending")
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)
    result = recover_claim(replace(request, apply=True), cwd=workspace.repository_root)
    assert result.action == "external_stop_confirmed_active_retained"
    state = load_run_state_readonly(workspace.run_state_path)
    assert "7" in state.active_worktrees and len(state.recovery_receipts) == 1
    assert not claim_was_released(state, 7, active.claim.claim_id)
    assert not attempt_was_released(state, 7, "attempt")
    before = workspace.run_state_path.read_bytes()
    result = recover_claim(
        replace(request, apply=True, reason="resend"), cwd=workspace.repository_root
    )
    assert result.action == "already_external_stop_confirmed_active_retained"
    assert workspace.run_state_path.read_bytes() == before


def _persist(workspace, active):
    state = load_run_state_readonly(workspace.run_state_path)
    state.active_worktrees["7"] = active
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)


@pytest.mark.parametrize(
    "change",
    ["claim", "attempt", "start", "owner", "pid", "marker", "completion", "runtime"],
)
def test_final_observation_revalidates_all_local_facts(
    external_claim, monkeypatch, change
):
    from orchestune.recovery import external_execution
    from orchestune.worktree_ops.claim_marker import write_claim_marker

    workspace, active, worktree, request = external_claim
    calls = 0

    def observe(*_):
        nonlocal calls
        calls += 1
        if calls == 2:
            changes = {
                "claim": {"claim_id": "new"},
                "attempt": {"launch_attempt_id": "new"},
                "start": {"started_at": 99.0},
                "owner": {"owner_token_digest": "new"},
                "pid": {"pid": os.getpid()},
                "completion": {"completion_id": "pending"},
            }
            if change in changes:
                _persist(workspace, replace_flat(active, **changes[change]))
            elif change == "marker":
                write_claim_marker(
                    worktree,
                    claim_id="new",
                    branch=active.core.branch,
                    base_sha=active.claim.base_sha,
                    branch_created=False,
                )
            elif change == "runtime":
                return "running", "provider_observed"
        return "unknown", "provider_unavailable"

    monkeypatch.setattr(external_execution, "read_runtime", observe)
    result = recover_claim(replace(request, apply=True), cwd=workspace.repository_root)
    state = load_run_state_readonly(workspace.run_state_path)
    assert "7" in state.active_worktrees
    if change == "completion":
        assert (
            result.success
            and result.action == "external_stop_confirmed_active_retained"
        )
    else:
        assert not result.success and not state.recovery_receipts


def test_apply_does_not_trust_preview(external_claim, monkeypatch):
    from orchestune.recovery import external_execution

    workspace, _, _, request = external_claim
    assert recover_claim(request, cwd=workspace.repository_root).success
    monkeypatch.setattr(
        external_execution, "read_runtime", lambda *_: ("running", "observed")
    )
    before = workspace.run_state_path.read_bytes()
    assert not recover_claim(
        replace(request, apply=True), cwd=workspace.repository_root
    ).success
    assert workspace.run_state_path.read_bytes() == before


def test_legacy_execution_requires_attempt_omission(external_claim):
    workspace, active, _, request = external_claim
    _persist(workspace, replace_flat(active, launch_attempt_id=None))
    assert not recover_claim(request, cwd=workspace.repository_root).success
    result = recover_claim(
        replace(request, launch_attempt_id=None, apply=True),
        cwd=workspace.repository_root,
    )
    assert result.success


def test_stop_confirmation_can_later_release_without_rewriting_first_record(
    external_claim,
):
    workspace, active, _, request = external_claim
    _persist(workspace, replace_flat(active, completion_id="pending"))
    applied = replace(request, apply=True)
    assert recover_claim(applied, cwd=workspace.repository_root).success
    first = load_run_state_readonly(workspace.run_state_path).recovery_receipts.copy()
    _persist(workspace, active)
    result = recover_claim(
        replace(applied, reason="second reason"), cwd=workspace.repository_root
    )
    assert result.action == "external_stop_confirmed_released"
    state = load_run_state_readonly(workspace.run_state_path)
    assert all(state.recovery_receipts[k] == v for k, v in first.items())


def test_gc_absent_is_not_claimed_as_recover_release(external_claim):
    workspace, active, _, request = external_claim
    _persist(workspace, replace_flat(active, completion_id="pending"))
    assert recover_claim(
        replace(request, apply=True), cwd=workspace.repository_root
    ).success
    state = load_run_state_readonly(workspace.run_state_path)
    state.active_worktrees.clear()
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)
    before = workspace.run_state_path.read_bytes()
    result = recover_claim(replace(request, apply=True), cwd=workspace.repository_root)
    assert result.action == "already_external_stop_confirmed_active_absent"
    assert workspace.run_state_path.read_bytes() == before


@pytest.mark.parametrize("case", ["invalid", "ambiguous", "new-active"])
def test_replay_rejects_corrupt_ambiguous_or_new_generation(external_claim, case):
    workspace, active, _, request = external_claim
    applied = replace(request, apply=True)
    assert recover_claim(applied, cwd=workspace.repository_root).success
    state = load_run_state_readonly(workspace.run_state_path)
    key = next(k for k in state.recovery_receipts if k.startswith("external-stop"))
    if case == "invalid":
        state.recovery_receipts[key]["source"] = "provider"
    elif case == "ambiguous":
        state.recovery_receipts["wrong-key"] = state.recovery_receipts[key].copy()
    else:
        state.active_worktrees["7"] = replace_flat(active, started_at=9.0)
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)
    before = workspace.run_state_path.read_bytes()
    result = recover_claim(applied, cwd=workspace.repository_root)
    assert not result.success
    assert workspace.run_state_path.read_bytes() == before


def test_existing_invalid_key_is_never_repaired(external_claim):
    from orchestune.ledger.external_stop_receipts import (
        confirmation_key,
        confirmation_record,
    )

    workspace, active, _, request = external_claim
    state = load_run_state_readonly(workspace.run_state_path)
    record = confirmation_record(active, "first")
    record["active"]["owner_token_digest"] = "wrong"
    state.recovery_receipts[confirmation_key(active)] = record
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)
    before = workspace.run_state_path.read_bytes()
    result = recover_claim(replace(request, apply=True), cwd=workspace.repository_root)
    assert not result.success and result.reason == "external_confirmation_invalid"
    assert workspace.run_state_path.read_bytes() == before
