"""Compatibility codec for flat ActiveWorktree ledger records."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orchestune.ledger.active_records import ActiveWorktree, _thaw_json

# Frozen flat JSON schema order from the T01 post-#1122 compatibility baseline.
_ACTIVE_FIELD_NAMES = (
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


def decode_active_worktree(value: Mapping[str, Any]) -> ActiveWorktree:
    """Decode a validated flat ledger record into the transitional DTO.

    Schema validation and compatibility defaults belong to ``run_state``. This
    function only maps normalized flat values onto the legacy constructor.
    """
    if not isinstance(value, Mapping):
        raise TypeError("active worktree record must be a mapping")
    values = {name: value[name] for name in _ACTIVE_FIELD_NAMES if name in value}
    if "declared_footprint" in values:
        values["declared_footprint"] = tuple(values["declared_footprint"])
    return ActiveWorktree(**values)


def encode_active_worktree(active: ActiveWorktree) -> dict[str, Any]:
    """Encode the transitional DTO as the established flat JSON object.

    Iteration follows the frozen dataclass field order. Footprints and nested
    JSON values are copied back to ordinary list/dict containers, and a null
    ``completion_policy_config`` retains its historical omission.
    """
    if not isinstance(active, ActiveWorktree):
        raise TypeError("active must be ActiveWorktree")
    encoded: dict[str, Any] = {}
    for name in _ACTIVE_FIELD_NAMES:
        value = getattr(active, name)
        if name == "completion_policy_config" and value is None:
            continue
        if name == "declared_footprint":
            encoded[name] = list(value)
        else:
            encoded[name] = _thaw_json(value)
    return encoded
