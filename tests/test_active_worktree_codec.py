"""Flat ledger codec contracts for ActiveWorktree."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from orchestune.ledger.active_codec import (
    decode_active_worktree,
    encode_active_worktree,
)
from orchestune.ledger.run_state import load_run_state

FIXTURES = Path(__file__).parent / "fixtures" / "active_worktree_compat"


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
    original = replace(
        load_run_state(FIXTURES / "states-input.json").active_worktrees["103"],
        completion_payload={"events": [{"labels": ["queued"]}]},
    )

    restored = decode_active_worktree(encode_active_worktree(original))

    assert encode_active_worktree(restored) == encode_active_worktree(original)
    assert isinstance(restored.completion_payload, dict)
    assert isinstance(restored.completion_payload["events"], list)
    assert isinstance(restored.declared_footprint, tuple)


def test_encoded_nested_json_values_are_detached_from_the_flat_dto() -> None:
    active = replace(
        load_run_state(FIXTURES / "states-input.json").active_worktrees["103"],
        completion_payload={"events": [{"labels": ["queued"]}]},
    )
    encoded = encode_active_worktree(active)
    assert active.completion_payload is not None
    encoded["completion_payload"]["events"][0]["labels"].append("changed")

    assert "changed" not in active.completion_payload["events"][0]["labels"]
