"""GC needs fresh generation-bound evidence even at its final boundaries."""

from unittest.mock import Mock

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.external_execution import hold_if_not_stopped
from orchestune.infra.process_utils import run_state_lock
from orchestune.ledger.external_stop_receipts import (
    confirmation_key,
    confirmation_record,
)
from orchestune.ledger.run_state import RunState, save_run_state
from tests.dispatch_test_support import replace_flat

pytest_plugins = ["tests.test_local_claim_identity"]


@pytest.mark.parametrize(
    "runtime,held", [("unknown", False), ("stopped", False), ("running", True)]
)
def test_only_unknown_uses_operator_receipt(local_claim, fake_forge, runtime, held):
    workspace, active, _ = local_claim
    active = replace_flat(active, external_id="run")
    state = RunState(
        recovery_receipts={
            confirmation_key(active): confirmation_record(active, "stopped")
        }
    )
    config = DispatcherConfig(
        parent_issue_number=0,
        forge=fake_forge,
        events_log_path=workspace.repository_root / "events.jsonl",
    )
    result = hold_if_not_stopped(
        active,
        config,
        "completion",
        state=state,
        repository_id=workspace.repository_identity,
        observe=lambda *_: runtime,
    )
    assert (result is not None) == held
    result = hold_if_not_stopped(
        active,
        config,
        "completion",
        state=state,
        repository_id="other",
        observe=lambda *_: "unknown",
    )
    assert result is not None


def test_direct_collection_without_config_holds_external(local_claim, monkeypatch):
    from orchestune.dispatch import runtime_reader as external_execution
    from orchestune.dispatch.gc.collection import _inspect_or_hold

    workspace, active, _ = local_claim
    active = replace_flat(active, external_id="run")
    monkeypatch.setattr(
        external_execution,
        "read_runtime",
        lambda *_: ("unknown", "provider_unavailable"),
    )
    # Existing handoff conditions still win for entries without a completed journal.
    plan = _inspect_or_hold(active, workspace, Mock())
    assert plan.action == "hold"


def test_final_guard_reads_new_receipts_and_rejects_changed_identity(
    local_claim, monkeypatch, fake_forge
):
    from orchestune.dispatch.gc.external_guard import fresh_external_hold

    workspace, active, _ = local_claim
    monkeypatch.chdir(workspace.repository_root)
    active = replace_flat(active, external_id="run")
    saved = RunState(
        active_worktrees={"7": active},
        recovery_receipts={
            confirmation_key(active): confirmation_record(active, "stopped")
        },
    )
    with run_state_lock(workspace.lock_path):
        save_run_state(saved, workspace.run_state_path)
    config = DispatcherConfig(
        parent_issue_number=0,
        forge=fake_forge,
        run_state_path=workspace.run_state_path,
        events_log_path=workspace.repository_root / "events.jsonl",
    )
    assert fresh_external_hold(active, config, "stale") is None
    saved.active_worktrees["7"] = replace_flat(active, started_at=22.0)
    with run_state_lock(workspace.lock_path):
        save_run_state(saved, workspace.run_state_path)
    assert fresh_external_hold(active, config, "stale") is not None


@pytest.fixture
def completed_external(tmp_path, monkeypatch):
    from orchestune.dispatch import runtime_reader as external_execution
    from tests.test_dispatch_gc_handoff_integration import (
        _create_repo,
        _forge,
        _make_active,
        _write_state,
    )

    repo, worktree, branch = _create_repo(tmp_path)
    active, comment = _make_active(repo, worktree, branch)
    active = replace_flat(
        active, external_id="run", launch_attempt_id="attempt", launch_phase="launched"
    )
    from orchestune.claim.workspace import resolve_claim_workspace

    _write_state(repo, active)
    workspace = resolve_claim_workspace(repo)
    forge = _forge(comment, branch, active.claim.base_sha)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        external_execution,
        "read_runtime",
        lambda *_: ("unknown", "provider_unavailable"),
    )
    return workspace, active, worktree, forge


def test_confirmation_enables_gc_without_bypassing_handoff_evidence(completed_external):
    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.dispatch.gc_service import run_handoff_gc
    from orchestune.ledger.run_state import load_run_state_readonly
    from orchestune.recovery.contracts import RecoveryRequest
    from orchestune.recovery.service import recover_claim

    workspace, active, worktree, forge = completed_external
    request = RecoveryRequest(
        250,
        active.claim.claim_id,
        "verified stop",
        apply=True,
        external_id="run",
        launch_attempt_id="attempt",
        confirm_external_stopped=True,
    )
    assert (
        recover_claim(request, cwd=workspace.repository_root).action
        == "external_stop_confirmed_active_retained"
    )
    before = workspace.run_state_path.read_bytes()
    preview = run_handoff_gc(
        GcRequest(apply=False, state_path=workspace.run_state_path),
        forge_factory=lambda: forge,
    )
    assert preview.items[0].action == "would_release"
    assert workspace.run_state_path.read_bytes() == before and worktree.exists()
    result = run_handoff_gc(
        GcRequest(apply=True, state_path=workspace.run_state_path),
        forge_factory=lambda: forge,
    )
    assert result.exit_code == 0 and result.items[0].action == "released"
    saved = load_run_state_readonly(workspace.run_state_path)
    assert saved.recovery_receipts and "250" not in saved.active_worktrees
    assert not worktree.exists()
    assert (
        recover_claim(request, cwd=workspace.repository_root).action
        == "already_external_stop_confirmed_active_absent"
    )


@pytest.mark.parametrize("runtime", ["running", "unknown"])
def test_direct_collection_without_confirmation_never_removes(
    completed_external, monkeypatch, runtime
):
    from orchestune.dispatch import runtime_reader as external_execution
    from orchestune.dispatch.gc.collection import _apply_candidate
    from orchestune.dispatch.gc.handoff import GcRequest

    workspace, active, worktree, forge = completed_external
    monkeypatch.setattr(
        external_execution, "read_runtime", lambda *_: (runtime, "observed")
    )
    items, receipts = [], []
    before = workspace.run_state_path.read_bytes()
    with run_state_lock(workspace.lock_path):
        _apply_candidate(
            "250", active, GcRequest(apply=True), workspace, forge, items, receipts
        )
    assert items[0].action == "held" and not receipts
    assert worktree.exists() and workspace.run_state_path.read_bytes() == before


def test_provider_running_at_physical_boundary_wins_over_receipt(
    completed_external, monkeypatch
):
    from orchestune.dispatch import runtime_reader as external_execution
    from orchestune.dispatch.gc.collection import _apply_candidate
    from orchestune.dispatch.gc.handoff import GcRequest
    from orchestune.recovery.contracts import RecoveryRequest
    from orchestune.recovery.service import recover_claim

    workspace, active, worktree, forge = completed_external
    request = RecoveryRequest(
        250,
        active.claim.claim_id,
        "stopped",
        apply=True,
        external_id="run",
        launch_attempt_id="attempt",
        confirm_external_stopped=True,
    )
    assert recover_claim(request, cwd=workspace.repository_root).success
    observations = iter(["unknown", "running"])
    monkeypatch.setattr(
        external_execution, "read_runtime", lambda *_: (next(observations), "observed")
    )
    items, receipts = [], []
    with run_state_lock(workspace.lock_path):
        _apply_candidate(
            "250", active, GcRequest(apply=True), workspace, forge, items, receipts
        )
    assert items[0].reason == "external_execution_held"
    assert worktree.exists() and not receipts


def test_confirmed_collection_syncs_receipts_before_policy_save(
    completed_external, fake_forge, monkeypatch
):
    from orchestune.dispatch.gc import confirmed
    from orchestune.ledger.run_state import load_run_state_readonly

    workspace, active, _, _ = completed_external
    saved = load_run_state_readonly(workspace.run_state_path)
    saved.recovery_receipts[confirmation_key(active)] = confirmation_record(
        active, "stopped"
    )
    with run_state_lock(workspace.lock_path):
        save_run_state(saved, workspace.run_state_path)
    stale = load_run_state_readonly(workspace.run_state_path)
    stale.recovery_receipts.clear()
    config = DispatcherConfig(
        parent_issue_number=0,
        forge=fake_forge,
        apply=False,
        run_state_path=workspace.run_state_path,
        events_log_path=workspace.repository_root / "events.jsonl",
    )

    def policies(state, _):
        assert state.recovery_receipts == saved.recovery_receipts
        return []

    monkeypatch.setattr(confirmed, "process_completion_policies", policies)
    result = confirmed.collect_confirmed_completion(
        stale, config, "250", active, lambda _: None, None
    )
    assert result.completion_event["action"] == "completion_handoff_preview"


def test_timeout_uses_receipt_and_preserves_it_across_saves(
    local_claim, fake_forge, monkeypatch
):
    from orchestune.dispatch.gc.zombies import (
        ZombieOrTimeoutReclaim,
        _apply_zombie_or_timeout_reclaim,
    )
    from orchestune.ledger.run_state import load_run_state_readonly

    workspace, active, worktree = local_claim
    monkeypatch.chdir(workspace.repository_root)
    active = replace_flat(
        active, owner_kind="dispatch", external_id="run", launch_phase="launched"
    )
    saved = RunState(
        active_worktrees={"7": active},
        recovery_receipts={
            confirmation_key(active): confirmation_record(active, "stopped")
        },
    )
    with run_state_lock(workspace.lock_path):
        save_run_state(saved, workspace.run_state_path)
    config = DispatcherConfig(
        parent_issue_number=0,
        forge=fake_forge,
        apply=True,
        run_state_path=workspace.run_state_path,
        events_log_path=workspace.repository_root / "events.jsonl",
    )
    monkeypatch.setattr(
        "orchestune.dispatch.gc.zombies.backup_wip_commit", lambda *_: None
    )
    remove = Mock()
    monkeypatch.setattr("orchestune.dispatch.gc.zombies.remove_worktree", remove)
    reclaim = ZombieOrTimeoutReclaim(
        key="7",
        active=active,
        subtask_id="task",
        reason="timeout",
        is_timeout=True,
        process_alive=False,
        status_labels=("status:in-progress",),
        reclaim_count=1,
        now=100.0,
    )
    with run_state_lock(workspace.lock_path):
        event = _apply_zombie_or_timeout_reclaim(saved, reclaim, config)
    assert event["action"] == "gc_reclaimed"
    remove.assert_called_once_with(str(worktree))
    after = load_run_state_readonly(workspace.run_state_path)
    assert (
        not after.active_worktrees
        and after.recovery_receipts == saved.recovery_receipts
    )
