"""Typed recovery receipt JSON compatibility and safety contracts."""

import json

import pytest

from orchestune.ledger.completion_records import InvalidLedgerRecord
from orchestune.ledger.recovery_receipts import (
    ExternalStopConfirmation,
    GcReleaseReceipt,
    decode_recovery_receipt,
)
from orchestune.ledger.run_state import (
    RunState,
    attempt_was_released,
    claim_was_released,
    load_run_state,
)
from tests.dispatch_test_support import save_locked_run_state as save_run_state


def _serialized_current_active(**overrides):
    active = {
        "issue_number": 10,
        "branch": "claude/issue-10-x",
        "worktree_path": "worktrees/claude-issue-10-x",
        "pid": 4_212_345,
        "started_at": 1700000000.0,
        "declared_footprint": [],
        "owner_kind": "dispatch",
        "claim_id": "claim-10",
        "claim_stage": "completed",
        "base_ref": "origin/main",
        "base_sha": "a" * 40,
        "reservation_kind": "footprint",
        "repository_id": "Saltmu/orchestune",
        "claimed_at": 1700000001.0,
        "owner_token_digest": "sha256:abc123",
    }
    active.update(overrides)
    return active


def test_recovery_receipts_decode_to_typed_values_and_preserve_json(tmp_path):
    raw = {
        "schema_version": 1,
        "operation": "external-stop-confirmation",
        "source": "operator",
        "confirmed_external_stopped": True,
        "reason": "operator verified",
        "recorded_at": "2026-11-01T00:00:00+00:00",
        "repository_id": "Saltmu/orchestune",
        "issue_number": 10,
        "claim_id": "claim-10",
        "launch_attempt_id": "attempt-10",
        "external_id": "external-10",
        "execution_identity": {
            "repository_id": "Saltmu/orchestune",
            "issue_number": 10,
            "claim_id": "claim-10",
            "owner_token_digest": "sha256:abc123",
            "claimed_at": 1700000001.0,
            "branch": "claude/issue-10-x",
            "launch_attempt_id": "attempt-10",
            "external_id": "external-10",
            "started_at": 1700000000.0,
        },
        "active": _serialized_current_active(
            launch_attempt_id="attempt-10", external_id="external-10"
        ),
        "worktree_action": "retain",
        "extension": {"preserve": [1, None, 3]},
    }
    key = "external-stop-confirmation::fixture"
    path = tmp_path / "run_state.json"
    path.write_text(json.dumps({"recovery_receipts": {key: raw}}), encoding="utf-8")

    state = load_run_state(path)
    assert isinstance(state.recovery_receipts[key], ExternalStopConfirmation)
    save_run_state(state, path)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["recovery_receipts"][key] == raw


def test_unknown_recovery_operation_is_invalid_and_round_trips(tmp_path):
    raw = {"schema_version": 1, "operation": "future-operation", "x": None}
    path = tmp_path / "run_state.json"
    path.write_text(
        json.dumps({"recovery_receipts": {"future": raw}}), encoding="utf-8"
    )

    state = load_run_state(path)
    value = state.recovery_receipts["future"]
    assert isinstance(value, InvalidLedgerRecord)
    assert value.raw_json == raw
    save_run_state(state, path)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["recovery_receipts"]["future"] == raw


def test_gc_release_reclaim_record_is_typed_and_preserves_array_order(tmp_path):
    raw = {
        "schema_version": 1,
        "operation": "gc-release",
        "repository_id": "Saltmu/orchestune",
        "issue_number": 10,
        "claim_id": "claim-10",
        "completion_id": "completion-10",
        "completion_result": "blocked",
        "branch": "claude/issue-10-x",
        "worktree_path": "worktrees/claude-issue-10-x",
        "base_sha": "a" * 40,
        "worktree_action": "retain",
        "stop_confirmed": True,
        "recorded_at": "2026-11-01T00:00:00+00:00",
        "active": _serialized_current_active(launch_attempt_id="attempt-10"),
        "reclaim": {
            "new_claim_id": "claim-next",
            "phase": "active_saved",
            "recorded_at": "2026-11-01T00:01:00+00:00",
            "superseded_claim_ids": ["claim-a", "claim-b"],
        },
        "extension": {"explicit_null": None},
    }
    path = tmp_path / "run_state.json"
    path.write_text(json.dumps({"recovery_receipts": {"gc": raw}}), encoding="utf-8")

    state = load_run_state(path)
    record = state.recovery_receipts["gc"]
    assert isinstance(record, GcReleaseReceipt)
    assert record.reclaim is not None
    assert record.reclaim.superseded_claim_ids == ("claim-a", "claim-b")
    save_run_state(state, path)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["recovery_receipts"]["gc"] == raw


def test_invalid_release_identity_still_prevents_generation_resurrection():
    state = RunState(
        recovery_receipts={
            "malformed": {
                "operation": "release",
                "issue_number": 10,
                "claim_id": "claim-10",
                "active": {"launch_attempt_id": "attempt-10"},
            }
        }
    )
    assert claim_was_released(state, 10, "claim-10")
    assert attempt_was_released(state, 10, "attempt-10")
    assert not claim_was_released(state, 10, "claim-other")


def test_recovery_codec_rejects_structurally_invalid_map_entries():
    with pytest.raises(TypeError, match="entries must be objects"):
        decode_recovery_receipt("not-an-object")
