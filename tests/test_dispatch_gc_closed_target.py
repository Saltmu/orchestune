"""#1526: closed-target interactive claims leave the ledger with a release receipt."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.gc import closed_target
from orchestune.dispatch.gc.closed_target import (
    closed_target_candidates,
    decide_closed_target,
    release_closed_target_claims,
    release_in_state,
)
from orchestune.infra.process_utils import run_state_lock
from orchestune.ledger.run_state import (
    claim_was_released,
    load_run_state_readonly,
    save_run_state,
)
from orchestune.worktree_ops.claim_marker import claim_marker_path, write_claim_marker
from tests.dispatch_test_support import replace_flat

pytest_plugins = ["tests.test_local_claim_identity"]


class _StateForge:
    def __init__(self, state: str | Exception) -> None:
        self.state = state
        self.calls: list[int] = []

    def get_issue_state(self, issue_number: int | str) -> str:
        self.calls.append(int(issue_number))
        if isinstance(self.state, Exception):
            raise self.state
        return self.state


def _save(workspace: Any, state: Any) -> None:
    with run_state_lock(workspace.lock_path):
        save_run_state(state, workspace.run_state_path)


def _replace_active(workspace: Any, **values: Any) -> Any:
    state = load_run_state_readonly(workspace.run_state_path)
    state.active_worktrees["7"] = replace_flat(state.active_worktrees["7"], **values)
    _save(workspace, state)
    return state.active_worktrees["7"]


def _decide(workspace: Any, forge: Any) -> Any:
    state = load_run_state_readonly(workspace.run_state_path)
    return decide_closed_target(
        "7", state.active_worktrees["7"], state, workspace, forge
    )


def _config(workspace: Any, forge: Any, *, apply: bool = True) -> DispatcherConfig:
    return cast(
        DispatcherConfig,
        SimpleNamespace(
            worktree_root=workspace.worktree_root,
            run_state_path=workspace.run_state_path,
            resolved_forge=forge,
            apply=apply,
            window_seconds=3600.0,
        ),
    )


@pytest.mark.parametrize("target_state", ["CLOSED", "MERGED", "closed"])
def test_closed_or_merged_target_is_released(local_claim, target_state):
    workspace, _, _ = local_claim
    decision = _decide(workspace, _StateForge(target_state))
    assert decision.action == "release"
    assert decision.reason == "closed_target"
    assert decision.receipt_reason == f"closed-target:{target_state.upper()}"


def test_open_target_is_not_released(local_claim):
    workspace, _, _ = local_claim
    decision = _decide(workspace, _StateForge("OPEN"))
    assert (decision.action, decision.reason) == ("skip", "target_open")


def test_forge_failure_or_absence_holds(local_claim):
    workspace, _, _ = local_claim
    failed = _decide(workspace, _StateForge(RuntimeError("gh down")))
    assert failed.action == "hold" and failed.reason == "forge_error: gh down"
    assert _decide(workspace, None).reason == "forge_unavailable"


@pytest.mark.parametrize(
    "values",
    [
        {"completion_id": "c-1"},
        {"completion_stage": "publishing"},
        {"completion_handoff_ready": True},
        {"owner_kind": "dispatch"},
    ],
)
def test_completion_owned_or_dispatch_claims_are_not_candidates(local_claim, values):
    workspace, _, _ = local_claim
    _replace_active(workspace, **values)
    state = load_run_state_readonly(workspace.run_state_path)
    forge = _StateForge("MERGED")
    assert closed_target_candidates(state) == []
    assert _decide(workspace, forge).action == "skip"
    assert forge.calls == []


def test_pending_completion_reservation_is_not_a_candidate(local_claim, monkeypatch):
    workspace, _, _ = local_claim
    monkeypatch.setattr(
        closed_target, "completion_reservation_status", lambda *_: "pending"
    )
    state = load_run_state_readonly(workspace.run_state_path)
    assert closed_target_candidates(state) == []


def test_marker_of_another_generation_holds(local_claim):
    workspace, active, worktree = local_claim
    write_claim_marker(
        worktree,
        claim_id="other-generation",
        branch=active.core.branch,
        base_sha=active.claim.base_sha,
        branch_created=True,
    )
    decision = _decide(workspace, _StateForge("MERGED"))
    assert decision.action == "hold"
    assert decision.reason == "claim marker belongs to another generation"


def test_release_in_state_records_a_release_receipt(local_claim):
    workspace, active, _ = local_claim
    state = load_run_state_readonly(workspace.run_state_path)
    decision = _decide(workspace, _StateForge("MERGED"))
    release_in_state(state, decision, workspace.repository_identity)

    assert "7" not in state.active_worktrees
    (key, receipt), *_ = state.recovery_receipts.items()
    assert key == (
        f"{workspace.repository_identity}::7::{active.claim.claim_id}::release"
    )
    assert receipt["operation"] == "release"
    assert receipt["reason"] == "closed-target:MERGED"
    assert receipt["worktree_action"] == "retain"
    assert receipt["claim_id"] == active.claim.claim_id
    assert receipt["active"]["issue_number"] == 7
    assert claim_was_released(state, 7, active.claim.claim_id)


def test_dispatcher_release_persists_and_retains_the_worktree(local_claim):
    workspace, active, worktree = local_claim
    (worktree / "file").write_text("uncommitted work")
    config = _config(workspace, _StateForge("MERGED"))
    state = load_run_state_readonly(workspace.run_state_path)

    with run_state_lock(workspace.lock_path):
        events = release_closed_target_claims(state, config)

    assert [event.to_dict() for event in events] == [
        {
            "issue_number": 7,
            "claim_id": active.claim.claim_id,
            "action": "gc_released_interactive_closed_target",
            "target_state": "MERGED",
            "reason": "closed-target:MERGED",
        }
    ]
    assert "7" not in state.active_worktrees
    persisted = load_run_state_readonly(workspace.run_state_path)
    assert persisted.active_worktrees == {}
    assert claim_was_released(persisted, 7, active.claim.claim_id)
    assert (worktree / "file").read_text() == "uncommitted work"
    assert claim_marker_path(worktree).exists()


def test_dispatcher_preview_only_reports(local_claim):
    workspace, _, _ = local_claim
    before = workspace.run_state_path.read_bytes()
    state = load_run_state_readonly(workspace.run_state_path)

    events = release_closed_target_claims(
        state, _config(workspace, _StateForge("CLOSED"), apply=False)
    )

    assert [event.action for event in events] == [
        "gc_would_release_interactive_closed_target"
    ]
    assert "7" in state.active_worktrees
    assert workspace.run_state_path.read_bytes() == before


def test_dispatcher_forge_failure_is_a_hold_event(local_claim):
    workspace, _, _ = local_claim
    before = workspace.run_state_path.read_bytes()
    state = load_run_state_readonly(workspace.run_state_path)

    events = release_closed_target_claims(
        state, _config(workspace, _StateForge(RuntimeError("rate limited")))
    )

    assert [event.to_dict()["action"] for event in events] == [
        "gc_release_held_interactive_closed_target"
    ]
    assert "7" in state.active_worktrees
    assert workspace.run_state_path.read_bytes() == before


def test_dispatcher_persistence_failure_keeps_the_claim(local_claim, monkeypatch):
    workspace, _, _ = local_claim
    state = load_run_state_readonly(workspace.run_state_path)

    def fail(*_: Any, **__: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(closed_target, "save_run_state", fail)
    events = release_closed_target_claims(
        state, _config(workspace, _StateForge("MERGED"))
    )

    assert events[0].to_dict()["reason"] == "persistence_failed: disk full"
    assert "7" in state.active_worktrees
    assert state.recovery_receipts == {}


def test_dispatcher_without_candidates_does_not_touch_forge(local_claim):
    workspace, _, _ = local_claim
    _replace_active(workspace, owner_kind="dispatch")
    forge = _StateForge("MERGED")
    state = load_run_state_readonly(workspace.run_state_path)
    assert release_closed_target_claims(state, _config(workspace, forge)) == []
    assert forge.calls == []


def test_recovery_receipt_matches_operator_release_shape(local_claim):
    """GC and ``orchestune recover`` write the same receipt fields."""
    from orchestune.recovery.contracts import RecoveryRequest
    from orchestune.recovery.service import recover_claim

    workspace, active, _ = local_claim
    state = load_run_state_readonly(workspace.run_state_path)
    release_in_state(
        state, _decide(workspace, _StateForge("CLOSED")), workspace.repository_identity
    )
    (gc_receipt,) = state.recovery_receipts.values()

    result = recover_claim(
        RecoveryRequest(
            7, claim_id=active.claim.claim_id, reason="operator", apply=True
        ),
        cwd=workspace.repository_root,
    )
    assert result.action == "released"
    raw = json.loads(Path(workspace.run_state_path).read_text())
    (operator_receipt,) = raw["recovery_receipts"].values()
    assert set(gc_receipt) == set(operator_receipt)
    assert gc_receipt["active"] == operator_receipt["active"]


def test_gc_phase_releases_merged_pr_claim_before_an_overlapping_launch(local_claim):
    """#1497 reproduction: GC runs before launch in the same Dispatcher cycle.

    ``tests/test_dispatch_cycle.py`` stubs workspace resolution, so this test runs
    the real GC phase and ``claim_task`` against a real Git repository instead.
    """
    import time

    from orchestune.claim.contracts import (
        ClaimFailureReason,
        ClaimRequest,
        OwnerKind,
    )
    from orchestune.claim.service import claim_task
    from orchestune.dispatch.phase_gc import run_gc_phase
    from tests.claim_helpers import MockForge, _make_issue

    workspace, active, worktree = local_claim
    _replace_active(workspace, declared_footprint=("orchestune/foo.py",))
    forge = MockForge({8: _make_issue(number=8)})
    forge.issue_states[7] = "MERGED"
    config = DispatcherConfig(
        parent_issue_number=100,
        run_state_path=workspace.run_state_path,
        worktree_root=workspace.worktree_root,
        events_log_path=workspace.repository_root / "events.jsonl",
        forge=forge,
        apply=True,
    )
    request = ClaimRequest(
        issue_number=8,
        owner_kind=OwnerKind.DISPATCH,
        state_path=workspace.run_state_path,
        worktree_root=workspace.worktree_root,
        dry_run=True,
    )

    blocked = claim_task(request, forge=forge, cwd=workspace.repository_root)
    assert blocked.failure is not None
    assert blocked.failure.reason == ClaimFailureReason.CLAIM_CONFLICT
    assert blocked.failure.conflicting_issue_number == 7

    with run_state_lock(workspace.lock_path):
        run_state = load_run_state_readonly(workspace.run_state_path)
        result = run_gc_phase(run_state, {}, config, [], [], now=time.time())

    assert [event.action for event in result.completion_events] == [
        "gc_released_interactive_closed_target"
    ]
    claimed = claim_task(request, forge=forge, cwd=workspace.repository_root)
    assert claimed.success, claimed.failure
    persisted = load_run_state_readonly(workspace.run_state_path)
    assert claim_was_released(persisted, 7, active.claim.claim_id)
    assert worktree.exists()
