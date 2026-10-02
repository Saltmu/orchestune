"""Byte-level contract frozen from the #1122 post-merge main baseline."""

from __future__ import annotations

import json
import os
from dataclasses import fields, replace
from pathlib import Path

import pytest

from orchestune.infra.process_utils import run_state_lock
from orchestune.ledger.active_codec import encode_active_worktree
from orchestune.ledger.active_records import ActiveWorktreeCore
from orchestune.ledger.run_state import (
    ActiveWorktree,
    RunState,
    load_run_state,
    save_run_state,
)

FIXTURES = Path(__file__).parent / "fixtures" / "active_worktree_compat"
BASELINE_NOW = 1_700_000_000.0
pytestmark = pytest.mark.uses_run_state_lock_assertion
ACTIVE_WORKTREE_FIELDS = (
    "issue_number",
    "branch",
    "worktree_path",
    "pid",
    "started_at",
    "declared_footprint",
    "recompute_count",
    "forced_serial",
    "external_id",
    "external_url",
    "base_branch",
    "estimated_tokens",
    "token_estimate_recorded",
    "profile",
    "model",
    "reasoning_effort",
    "selection_reason",
    "launch_attempt_id",
    "launch_phase",
    "owner_kind",
    "claim_id",
    "claim_stage",
    "base_ref",
    "base_sha",
    "reservation_kind",
    "repository_id",
    "claimed_at",
    "owner_token_digest",
    "completion_id",
    "completion_result",
    "completion_stage",
    "completion_payload",
    "completion_comment_id",
    "completion_comment_url",
    "completion_handoff_ready",
    "completion_policy_config",
)


def test_post_1122_persisted_field_order_is_frozen_after_cutover() -> None:
    state = load_run_state(FIXTURES / "states-input.json")
    names = tuple(encode_active_worktree(state.active_worktrees["103"]))

    assert len(names) == 36
    assert names == ACTIVE_WORKTREE_FIELDS
    assert tuple(field.name for field in fields(ActiveWorktree)) == (
        "core",
        "launch",
        "claim",
        "completion",
    )


def test_normalized_run_state_bytes_match_post_1122_golden(tmp_path: Path) -> None:
    path = tmp_path / "run_state.json"
    path.write_bytes((FIXTURES / "states-input.json").read_bytes())
    state = load_run_state(path)

    with run_state_lock(path.with_suffix(".lock")):
        save_run_state(state, path, now=BASELINE_NOW)

    actual = path.read_bytes()
    assert actual == (FIXTURES / "states-normalized.json").read_bytes()
    assert not actual.endswith(b"\n")

    records = json.loads(actual)["active_worktrees"]
    assert records["101"]["declared_footprint"] == ["src/z.py", "src/a.py"]
    assert records["102"]["token_estimate_recorded"] is True
    assert "completion_policy_config" in records["103"]
    assert "completion_policy_config" not in records["101"]


def test_independent_dispatch_claim_and_completion_facts_survive_load() -> None:
    state = load_run_state(FIXTURES / "states-input.json")

    reserved = state.active_worktrees["101"]
    assert reserved.claim.claim_stage == "reserved"
    assert reserved.launch.pid is None
    assert reserved.launch.started_at is None

    dispatch_with_claim = state.active_worktrees["102"]
    assert dispatch_with_claim.claim.owner_kind == "dispatch"
    assert dispatch_with_claim.claim.claim_id == "claim-102"
    assert dispatch_with_claim.claim.claim_stage == "completed"
    assert dispatch_with_claim.launch.launch_phase == "launching"

    completing = state.active_worktrees["103"]
    assert completing.launch.pid == 31337
    assert completing.completion.completion_id == "completion-103"
    assert completing.completion.completion_stage == "posting"

    legacy_handoff = state.active_worktrees["104"]
    assert legacy_handoff.completion.completion_stage == "handed_off_to_gc"
    assert legacy_handoff.completion.completion_handoff_ready is True

    handoff = state.active_worktrees["105"]
    assert handoff.completion.completion_stage == "handed_off"
    assert handoff.completion.completion_handoff_ready is True


def test_live_pid_and_completion_can_be_saved_together(tmp_path: Path) -> None:
    path = tmp_path / "run_state.json"
    path.write_bytes((FIXTURES / "states-input.json").read_bytes())
    state = load_run_state(path)
    active = state.active_worktrees["103"]
    active.launch = replace(active.launch, pid=os.getpid())

    with run_state_lock(path.with_suffix(".lock")):
        save_run_state(state, path, now=BASELINE_NOW)

    persisted = load_run_state(path).active_worktrees["103"]
    assert persisted.launch.pid == os.getpid()
    assert persisted.completion.completion_id == "completion-103"
    assert persisted.completion.completion_stage == "posting"


def test_recovery_sentinel_bytes_match_post_1122_golden(tmp_path: Path) -> None:
    path = tmp_path / "run_state.json"
    active = ActiveWorktree(
        core=ActiveWorktreeCore(
            issue_number=14107,
            branch="dispatch/14107",
            worktree_path="worktrees/dispatch-14107",
            declared_footprint=(),
        )
    )
    state = RunState(active_worktrees={"14107": active})

    with run_state_lock(path.with_suffix(".lock")):
        save_run_state(state, path, now=BASELINE_NOW)

    assert active.claim.claim_id == "recovered-14107"
    assert active.claim.claim_stage == "completed"

    assert (
        path.read_bytes()
        == (FIXTURES / "recovery-sentinel-normalized.json").read_bytes()
    )
    recovered = load_run_state(path).active_worktrees["14107"]
    expected = json.loads(
        (FIXTURES / "recovery-sentinel-normalized.json").read_text(encoding="utf-8")
    )["active_worktrees"]["14107"]
    assert recovered.claim.claim_id == "recovered-14107"
    assert recovered.claim.claim_stage == "completed"
    assert recovered.claim.owner_token_digest == expected["owner_token_digest"]
    assert active.claim.owner_token_digest == expected["owner_token_digest"]


@pytest.mark.parametrize(
    ("issue_number", "field", "value", "expected_detail"),
    [
        ("102", "owner_kind", "unknown", "ownership or lifecycle value is unknown"),
        (
            "104",
            "completion_handoff_ready",
            "true",
            "completion_handoff_ready must be a boolean",
        ),
    ],
)
def test_invalid_state_error_messages_match_post_1122_baseline(
    tmp_path: Path,
    issue_number: str,
    field: str,
    value: str,
    expected_detail: str,
) -> None:
    raw = json.loads((FIXTURES / "states-input.json").read_text(encoding="utf-8"))
    raw["active_worktrees"][issue_number][field] = value
    path = tmp_path / "run_state.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError) as error:
        load_run_state(path)

    assert str(error.value) == (
        f"active_worktrees[{issue_number}] schema error: {expected_detail}"
    )


def test_save_still_requires_the_matching_run_state_lock(tmp_path: Path) -> None:
    path = tmp_path / "run_state.json"

    with pytest.raises(RuntimeError) as error:
        save_run_state(RunState(), path)

    assert (
        str(error.value) == f"run_state lock must be held: {path.with_suffix('.lock')}"
    )
