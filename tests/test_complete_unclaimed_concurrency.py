"""Shared-lock races with claim and worktree-free completion owners."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_complete_unclaimed import unclaimed as _unclaimed_fixture

from orchestune.claim.contracts import ClaimRequest
from orchestune.claim.ownership import owner_token_digest
from orchestune.claim.service import claim_task
from orchestune.complete.contracts import CompleteFailureReason
from orchestune.complete.service import complete_task
from orchestune.ledger.run_state import (
    ActiveWorktree,
    load_run_state_readonly,
    save_run_state,
)

unclaimed = _unclaimed_fixture


@pytest.mark.parametrize("same_owner", [True, False])
def test_simultaneous_not_needed_owners_share_one_reserved_payload(
    unclaimed, same_owner
):
    request, forge = unclaimed
    start = Barrier(2)
    attempts = 0

    def inject(operation, after):
        nonlocal attempts
        if operation == "post" and not after:
            attempts += 1
            if attempts == 1:
                raise OSError("interrupted owner")

    forge.inject = inject

    def run(owner):
        start.wait(timeout=5)
        return complete_task(replace(request, owner_token=owner), forge=forge)

    with ThreadPoolExecutor(2) as executor:
        results = list(
            executor.map(run, ["owner-a", "owner-a" if same_owner else "owner-b"])
        )
    assert len({r.completion_id for r in results}) == 1
    assert attempts == (2 if same_owner else 1)
    assert sum(r.success for r in results) == int(same_owner)
    if not same_owner:
        assert any(
            r.failure.reason == CompleteFailureReason.OWNER_TOKEN_MISMATCH
            for r in results
        )
    state = load_run_state_readonly(request.state_path)
    assert len(state.completion_reservations) == 1 and not state.active_worktrees


def _claim_setup(request, monkeypatch):
    workspace = SimpleNamespace(
        run_state_path=request.state_path,
        lock_path=request.state_path.with_suffix(".lock"),
        repository_identity="repo",
    )
    monkeypatch.setattr(
        "orchestune.claim.service.resolve_claim_workspace", lambda *_, **__: workspace
    )
    preflight = SimpleNamespace(subtask_id="task-1111", base_ref="main")
    issue = SimpleNamespace(number=1111)
    monkeypatch.setattr(
        "orchestune.claim.service._validate_preflight_and_conflict",
        lambda *_: (preflight, issue, None),
    )
    return ClaimRequest(
        1111,
        owner_token="claim-owner",
        state_path=request.state_path,
        timeout_seconds=10,
    )


def test_claim_wins_before_not_needed_without_post_or_completion_token(
    unclaimed, monkeypatch
):
    request, forge = unclaimed
    claim = _claim_setup(request, monkeypatch)
    before_lock, release = Event(), Event()
    from orchestune.complete.unclaimed import complete_unclaimed

    def delayed(*args):
        # complete has observed an unclaimed snapshot, but has not locked yet.
        before_lock.set()
        assert release.wait(timeout=5)
        return complete_unclaimed(*args)

    monkeypatch.setattr("orchestune.complete.service.complete_unclaimed", delayed)

    def apply(workspace, req, issue, preflight, branch, base, state, forge, token, **_):
        state.active_worktrees["1111"] = ActiveWorktree(
            1111,
            branch,
            str(request.worktree_root),
            None,
            None,
            (),
            claim_id="claim-winner",
            owner_token_digest=owner_token_digest(token),
            repository_id="repo",
        )
        save_run_state(state, request.state_path)
        return SimpleNamespace(success=True)

    monkeypatch.setattr("orchestune.claim.service._apply_claim_side_effects", apply)
    with ThreadPoolExecutor(2) as executor:
        loser = executor.submit(complete_task, request, forge=forge)
        assert before_lock.wait(timeout=5)
        winner = executor.submit(claim_task, claim, forge=forge)
        assert winner.result(timeout=10).success
        release.set()
        denied = loser.result(timeout=10)
        assert not denied.success
        assert denied.failure.reason == CompleteFailureReason.CONCURRENT_COMPLETION
    assert not forge.comments
    assert not (
        request.state_path.parent / ".orchestune" / "completion-tokens"
    ).exists()


def test_not_needed_reservation_wins_claim_rechecks_inside_lock(unclaimed, monkeypatch):
    request, forge = unclaimed
    claim = _claim_setup(request, monkeypatch)
    reserved, release = Event(), Event()
    apply = Mock()
    monkeypatch.setattr("orchestune.claim.service._apply_claim_side_effects", apply)

    def inject(operation, after):
        if operation == "post" and not after:
            reserved.set()
            assert release.wait(timeout=5)
            raise OSError("leave reservation pending")

    forge.inject = inject
    with ThreadPoolExecutor(2) as executor:
        completion = executor.submit(complete_task, request, forge=forge)
        assert reserved.wait(timeout=5)
        claimant = executor.submit(claim_task, claim, forge=forge)
        release.set()
        assert not completion.result(timeout=10).success
        assert not claimant.result(timeout=10).success
    apply.assert_not_called()
    assert not load_run_state_readonly(request.state_path).active_worktrees


@pytest.mark.parametrize("same_owner", [True, False])
def test_simultaneous_successful_requests_publish_only_one_generation(
    unclaimed, same_owner
):
    request, forge = unclaimed
    start = Barrier(2)

    def run(owner):
        start.wait(timeout=5)
        return complete_task(replace(request, owner_token=owner), forge=forge)

    with ThreadPoolExecutor(2) as executor:
        results = list(
            executor.map(run, ["owner-a", "owner-a" if same_owner else "owner-b"])
        )
    assert sum(r.success for r in results) == 1
    assert len(forge.comments) == 1
    state = load_run_state_readonly(request.state_path)
    assert len(state.completion_reservations) == 1
    assert (
        len(
            list(
                request.state_path.parent.glob(".orchestune/completion-tokens/*.token")
            )
        )
        == 1
    )
