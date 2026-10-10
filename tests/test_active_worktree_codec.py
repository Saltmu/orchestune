"""Flat ledger codec contracts for ActiveWorktree."""

from __future__ import annotations

import json
from dataclasses import fields, replace
from pathlib import Path

from orchestune.ledger.active_codec import (
    _ACTIVE_FIELD_NAMES,
    decode_active_worktree,
    encode_active_worktree,
)
from orchestune.ledger.active_records import (
    ActiveCompletionJournal,
    ActiveWorktree,
    ActiveWorktreeCore,
    ClaimInfo,
    LaunchInfo,
)
from orchestune.ledger.run_state import load_run_state

FIXTURES = Path(__file__).parent / "fixtures" / "active_worktree_compat"


def test_codec_field_order_covers_every_subrecord_field_exactly_once() -> None:
    record_names = [
        item.name
        for record_type in (
            ActiveWorktreeCore,
            LaunchInfo,
            ClaimInfo,
            ActiveCompletionJournal,
        )
        for item in fields(record_type)
    ]

    assert len(_ACTIVE_FIELD_NAMES) == len(set(_ACTIVE_FIELD_NAMES)) == 39
    assert sorted(_ACTIVE_FIELD_NAMES) == sorted(record_names)
    assert _ACTIVE_FIELD_NAMES[:6] == (
        "issue_number",
        "branch",
        "worktree_path",
        "pid",
        "started_at",
        "declared_footprint",
    )


def test_flat_codec_matches_frozen_normalized_records_and_field_order() -> None:
    state = load_run_state(FIXTURES / "states-input.json")
    expected = json.loads(
        (FIXTURES / "states-normalized.json").read_text(encoding="utf-8")
    )["active_worktrees"]

    for key, active in state.active_worktrees.items():
        actual = encode_active_worktree(active)

        assert list(actual) == list(expected[key])
        assert actual == expected[key]


def test_flat_codec_round_trip_keeps_dict_and_list_json_values() -> None:
    original = _with_payload(
        load_run_state(FIXTURES / "states-input.json").active_worktrees["103"],
        {"events": [{"labels": ["queued"]}]},
    )

    restored = decode_active_worktree(encode_active_worktree(original))

    assert encode_active_worktree(restored) == encode_active_worktree(original)
    assert restored == original
    assert isinstance(restored.core.declared_footprint, tuple)
    assert isinstance(encode_active_worktree(restored)["completion_payload"], dict)


def test_encoded_nested_json_values_are_detached_from_the_subrecords() -> None:
    active = _with_payload(
        load_run_state(FIXTURES / "states-input.json").active_worktrees["103"],
        {"events": [{"labels": ["queued"]}]},
    )
    encoded = encode_active_worktree(active)
    assert active.completion.completion_payload is not None
    encoded["completion_payload"]["events"][0]["labels"].append("changed")

    assert active.completion.completion_payload["events"][0]["labels"] == ("queued",)


def test_decode_applies_subrecord_defaults_for_absent_flat_keys() -> None:
    active = decode_active_worktree(
        {
            "issue_number": 7,
            "branch": "task/7",
            "worktree_path": "w",
            "declared_footprint": ["a.py"],
        }
    )

    assert active.core.base_branch == "origin/main"
    assert active.core.declared_footprint == ("a.py",)
    assert active.launch == LaunchInfo()
    assert active.claim == ClaimInfo()
    assert active.completion == ActiveCompletionJournal()
    assert "completion_policy_config" not in encode_active_worktree(active)


def _with_payload(active: ActiveWorktree, payload: dict) -> ActiveWorktree:
    return replace(
        active, completion=replace(active.completion, completion_payload=payload)
    )


def test_launch_attribution_fields_round_trip_and_are_omitted_when_absent() -> None:
    base = decode_active_worktree(
        {
            "issue_number": 7,
            "branch": "task/7",
            "worktree_path": "w",
            "declared_footprint": ["a.py"],
        }
    )
    encoded = encode_active_worktree(base)
    for name in ("launch_target", "launch_log_path", "launch_log_offset"):
        assert name not in encoded

    attributed = replace(
        base,
        launch=replace(
            base.launch,
            launch_target="claude-cli",
            launch_log_path="logs/task-7.log",
            launch_log_offset=12,
        ),
    )
    encoded = encode_active_worktree(attributed)
    assert encoded["launch_target"] == "claude-cli"
    assert encoded["launch_log_path"] == "logs/task-7.log"
    assert encoded["launch_log_offset"] == 12
    assert decode_active_worktree(encoded) == attributed
