from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from orchestune.ledger.active_records import (
    ActiveCompletionJournal,
    ActiveWorktreeCore,
    ClaimInfo,
)
from orchestune.ledger.run_state import (
    ActiveWorktree,
    ActiveWorktreeLifecycle,
    lifecycle,
    load_run_state,
    recovered_owner_token_digest,
)

FIXTURES = Path(__file__).parent / "fixtures" / "active_worktree_compat"


def _active(
    issue_number: int,
    *,
    claim: ClaimInfo | None = None,
    completion: ActiveCompletionJournal | None = None,
) -> ActiveWorktree:
    return ActiveWorktree(
        core=ActiveWorktreeCore(
            issue_number=issue_number,
            branch=f"task/{issue_number}",
            worktree_path=f"worktrees/task-{issue_number}",
            declared_footprint=(),
        ),
        claim=ClaimInfo() if claim is None else claim,
        completion=ActiveCompletionJournal() if completion is None else completion,
    )


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

    assert active.launch.launch_phase == "launched"
    assert active.launch.pid is not None
    assert active.completion.completion_id is not None
    assert lifecycle(active) is ActiveWorktreeLifecycle.COMPLETING


def test_handoff_ready_completion_precedes_launch() -> None:
    state = load_run_state(FIXTURES / "states-input.json")
    base = state.active_worktrees["102"]
    active = replace(
        base,
        completion=replace(
            base.completion,
            completion_id="completion-102",
            completion_stage="handed_off",
            completion_handoff_ready=True,
        ),
    )

    assert lifecycle(active) is ActiveWorktreeLifecycle.HANDOFF_READY


def test_launched_worktree_is_classified_as_running() -> None:
    state = load_run_state(FIXTURES / "states-input.json")
    base = state.active_worktrees["102"]
    active = replace(
        base,
        launch=replace(base.launch, launch_phase="launched", external_id="worker-102"),
    )

    assert lifecycle(active) is ActiveWorktreeLifecycle.RUNNING


def test_recovery_sentinel_precedes_claim_fallback() -> None:
    state = load_run_state(FIXTURES / "recovery-sentinel-normalized.json")
    active = state.active_worktrees["14107"]

    assert active.claim.claim_stage == "completed"
    assert lifecycle(active) is ActiveWorktreeLifecycle.RECOVERY_REQUIRED


def test_unverifiable_owner_digest_precedes_interactive_claim() -> None:
    state = load_run_state(FIXTURES / "states-input.json")
    reserved = state.active_worktrees["101"]
    active = replace(
        reserved,
        claim=replace(
            reserved.claim,
            claim_stage="completed",
            owner_token_digest=recovered_owner_token_digest(
                reserved.core.issue_number, reserved.claim.claim_id
            ),
        ),
    )

    assert lifecycle(active) is ActiveWorktreeLifecycle.RECOVERY_REQUIRED


def test_launch_precedes_recovery_sentinel_and_claim_fallback() -> None:
    state = load_run_state(FIXTURES / "recovery-sentinel-normalized.json")
    base = state.active_worktrees["14107"]
    active = replace(base, launch=replace(base.launch, launch_phase="launching"))

    assert lifecycle(active) is ActiveWorktreeLifecycle.LAUNCHING


def test_completed_interactive_claim_is_classified_as_claimed() -> None:
    state = load_run_state(FIXTURES / "states-input.json")
    base = state.active_worktrees["101"]
    active = replace(
        base,
        claim=replace(base.claim, claim_stage="completed", owner_token_digest="digest"),
    )

    assert lifecycle(active) is ActiveWorktreeLifecycle.CLAIMED


def test_interactive_owner_without_claim_stage_falls_back_to_reservation() -> None:
    active = _active(44, claim=ClaimInfo(owner_kind="interactive", claim_id="claim-44"))

    assert lifecycle(active) is ActiveWorktreeLifecycle.RESERVED


def test_handoff_ready_is_a_candidate_without_receipt_evidence() -> None:
    active = _active(
        42,
        completion=ActiveCompletionJournal(
            completion_id="completion-42",
            completion_stage="handed_off",
            completion_handoff_ready=True,
        ),
    )

    assert active.completion.completion_comment_id is None
    assert active.completion.completion_comment_url is None
    assert lifecycle(active) is ActiveWorktreeLifecycle.HANDOFF_READY


def test_unmarked_record_falls_back_to_reservation() -> None:
    active = _active(43)

    assert lifecycle(active) is ActiveWorktreeLifecycle.RESERVED
