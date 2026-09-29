from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from orchestune.ledger.run_state import (
    ActiveWorktree,
    ActiveWorktreeLifecycle,
    lifecycle,
    load_run_state,
    recovered_owner_token_digest,
)

FIXTURES = Path(__file__).parent / "fixtures" / "active_worktree_compat"


@pytest.mark.parametrize(
    ("issue_number", "expected"),
    [
        ("101", ActiveWorktreeLifecycle.RESERVED),
        ("102", ActiveWorktreeLifecycle.LAUNCHING),
        ("103", ActiveWorktreeLifecycle.COMPLETING),
        ("104", ActiveWorktreeLifecycle.HANDOFF_READY),
        ("105", ActiveWorktreeLifecycle.HANDOFF_READY),
    ],
)
def test_classifies_every_frozen_baseline_state(
    issue_number: str, expected: ActiveWorktreeLifecycle
) -> None:
    state = load_run_state(FIXTURES / "states-input.json")

    assert lifecycle(state.active_worktrees[issue_number]) is expected


def test_completion_state_precedes_launch_and_live_process() -> None:
    state = load_run_state(FIXTURES / "states-input.json")
    active = state.active_worktrees["103"]

    assert active.launch_phase == "launched"
    assert active.pid is not None
    assert active.completion_id is not None
    assert lifecycle(active) is ActiveWorktreeLifecycle.COMPLETING


def test_handoff_ready_completion_precedes_launch() -> None:
    state = load_run_state(FIXTURES / "states-input.json")
    active = replace(
        state.active_worktrees["102"],
        completion_id="completion-102",
        completion_stage="handed_off",
        completion_handoff_ready=True,
    )

    assert lifecycle(active) is ActiveWorktreeLifecycle.HANDOFF_READY


def test_launched_worktree_is_classified_as_running() -> None:
    state = load_run_state(FIXTURES / "states-input.json")
    active = replace(
        state.active_worktrees["102"],
        launch_phase="launched",
        external_id="worker-102",
    )

    assert lifecycle(active) is ActiveWorktreeLifecycle.RUNNING


def test_recovery_sentinel_precedes_claim_fallback() -> None:
    state = load_run_state(FIXTURES / "recovery-sentinel-normalized.json")
    active = state.active_worktrees["14107"]

    assert active.claim_stage == "completed"
    assert lifecycle(active) is ActiveWorktreeLifecycle.RECOVERY_REQUIRED


def test_unverifiable_owner_digest_precedes_interactive_claim() -> None:
    state = load_run_state(FIXTURES / "states-input.json")
    reserved = state.active_worktrees["101"]
    active = replace(
        reserved,
        claim_stage="completed",
        owner_token_digest=recovered_owner_token_digest(
            reserved.issue_number, reserved.claim_id
        ),
    )

    assert lifecycle(active) is ActiveWorktreeLifecycle.RECOVERY_REQUIRED


def test_launch_precedes_recovery_sentinel_and_claim_fallback() -> None:
    state = load_run_state(FIXTURES / "recovery-sentinel-normalized.json")
    active = replace(state.active_worktrees["14107"], launch_phase="launching")

    assert lifecycle(active) is ActiveWorktreeLifecycle.LAUNCHING


def test_completed_interactive_claim_is_classified_as_claimed() -> None:
    state = load_run_state(FIXTURES / "states-input.json")
    active = replace(
        state.active_worktrees["101"],
        claim_stage="completed",
        owner_token_digest="digest",
    )

    assert lifecycle(active) is ActiveWorktreeLifecycle.CLAIMED


def test_interactive_owner_without_claim_stage_falls_back_to_reservation() -> None:
    active = ActiveWorktree(
        issue_number=44,
        branch="task/44",
        worktree_path="worktrees/task-44",
        pid=None,
        started_at=None,
        declared_footprint=(),
        owner_kind="interactive",
        claim_id="claim-44",
    )

    assert lifecycle(active) is ActiveWorktreeLifecycle.RESERVED


def test_handoff_ready_is_a_candidate_without_receipt_evidence() -> None:
    active = ActiveWorktree(
        issue_number=42,
        branch="task/42",
        worktree_path="worktrees/task-42",
        pid=None,
        started_at=None,
        declared_footprint=(),
        completion_id="completion-42",
        completion_stage="handed_off",
        completion_handoff_ready=True,
    )

    assert active.completion_comment_id is None
    assert active.completion_comment_url is None
    assert lifecycle(active) is ActiveWorktreeLifecycle.HANDOFF_READY


def test_unmarked_record_falls_back_to_reservation() -> None:
    active = ActiveWorktree(
        issue_number=43,
        branch="task/43",
        worktree_path="worktrees/task-43",
        pid=None,
        started_at=None,
        declared_footprint=(),
    )

    assert lifecycle(active) is ActiveWorktreeLifecycle.RESERVED
