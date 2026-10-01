"""Contracts for the dispatch launch owner API (#1130)."""

from __future__ import annotations

from dataclasses import fields

import pytest

from orchestune.dispatch.launch_state import (
    build_launch_record,
    launch_info_with_selection,
    launch_info_with_token_estimate,
    with_launch,
    with_launch_phase,
)
from orchestune.ledger.active_records import (
    ActiveCompletionJournal,
    ActiveWorktreeCore,
    ClaimInfo,
    LaunchInfo,
)
from orchestune.targets.contracts import ExecutionSelection
from tests.dispatch_test_support import make_test_active_worktree


def _core() -> ActiveWorktreeCore:
    return ActiveWorktreeCore(
        issue_number=7,
        branch="claude/issue-7-task",
        worktree_path="worktrees/claude-issue-7-task",
        declared_footprint=("a.py",),
        base_branch="origin/parent/issue-1",
    )


def _held_record():
    return make_test_active_worktree(
        7,
        pid=11,
        launch_phase="launching",
        launch_attempt_id="attempt-7",
        owner_kind="dispatch",
        claim_id="claim-7",
        claim_stage="completed",
        reservation_kind="repository",
        owner_token_digest="digest-7",
        completion_id="completion-7",
        completion_stage="posting",
        completion_payload={"result": "done"},
    )


def test_build_launch_record_keeps_each_owner_record() -> None:
    claim = ClaimInfo(claim_id="claim-7", claim_stage="completed")
    completion = ActiveCompletionJournal(completion_policy_config={"source": "x"})
    launch = LaunchInfo(pid=5, started_at=2.0, launch_phase="launched")

    active = build_launch_record(
        core=_core(), launch=launch, claim=claim, completion=completion
    )

    assert active.core == _core()
    assert active.launch == launch
    assert active.claim == claim
    assert active.completion == completion


def test_build_launch_record_defaults_to_unclaimed_without_completion() -> None:
    active = build_launch_record(core=_core(), launch=LaunchInfo())

    assert active.claim == ClaimInfo()
    assert active.completion == ActiveCompletionJournal()


def test_with_launch_replaces_only_launch_owned_fields() -> None:
    active = _held_record()
    launch = LaunchInfo(
        pid=None, external_id="ext-1", launch_attempt_id="attempt-8", recompute_count=3
    )

    updated = with_launch(active, launch)

    assert updated.launch == launch
    assert updated.core == active.core
    assert updated.claim == active.claim
    assert updated.completion == active.completion
    assert active.launch.launch_attempt_id == "attempt-7"


def test_with_launch_rejects_other_records() -> None:
    with pytest.raises(TypeError, match="LaunchInfo"):
        with_launch(_held_record(), ClaimInfo())  # type: ignore[arg-type]


@pytest.mark.parametrize("phase", ["launching", "failed", None])
def test_with_launch_phase_updates_phase_and_returns_a_copy(phase) -> None:
    active = _held_record()

    updated = with_launch_phase(active, phase)

    assert updated is not active
    assert updated.launch.launch_phase == phase
    assert active.launch.launch_phase == "launching"
    expected = {
        f.name: getattr(active.launch, f.name)
        for f in fields(LaunchInfo)
        if f.name != "launch_phase"
    }
    assert {name: getattr(updated.launch, name) for name in expected} == expected
    assert updated.claim == active.claim
    assert updated.completion == active.completion


def test_selection_fields_come_from_the_execution_selection() -> None:
    selection = ExecutionSelection(
        profile="luna-xhigh", model="m", reasoning_effort="high", reason="tier"
    )

    launch = launch_info_with_selection(
        LaunchInfo(pid=3), selection, fallback_profile="ignored"
    )

    assert (
        launch.pid,
        launch.profile,
        launch.model,
        launch.reasoning_effort,
        launch.selection_reason,
    ) == (3, "luna-xhigh", "m", "high", "tier")


def test_selection_absent_keeps_only_the_fallback_profile() -> None:
    launch = launch_info_with_selection(
        LaunchInfo(model="stale", selection_reason="stale"),
        None,
        fallback_profile="declared",
    )

    assert (
        launch.profile,
        launch.model,
        launch.reasoning_effort,
        launch.selection_reason,
    ) == ("declared", None, None, None)


@pytest.mark.parametrize("tokens", [1200, None])
def test_token_estimate_is_recorded_even_when_unknown(tokens) -> None:
    launch = launch_info_with_token_estimate(LaunchInfo(pid=3), tokens)

    assert launch.estimated_tokens == tokens
    assert launch.token_estimate_recorded is True
    assert launch.pid == 3
