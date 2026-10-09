"""Explicit codec between flat ledger records and the nested ActiveWorktree."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields
from typing import Any

from orchestune.ledger.active_records import (
    ActiveCompletionJournal,
    ActiveWorktree,
    ActiveWorktreeCore,
    ClaimInfo,
    LaunchInfo,
    _thaw_json,
)

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
    # #1270: appended after the frozen schema; omitted from the JSON while null so
    # records written before launch attribution keep their exact encoding.
    "launch_target",
    "launch_log_path",
    "launch_log_offset",
)

#: Keys omitted from the encoded record while their value is null.
_OMIT_WHEN_NULL = frozenset(
    {
        "completion_policy_config",
        "launch_target",
        "launch_log_path",
        "launch_log_offset",
    }
)


def _group_values(value: Mapping[str, Any], record_type: type) -> dict[str, Any]:
    names = {record_field.name for record_field in fields(record_type)}
    return {
        name: value[name]
        for name in _ACTIVE_FIELD_NAMES
        if name in names and name in value
    }


def decode_active_worktree(value: Mapping[str, Any]) -> ActiveWorktree:
    """Decode a validated flat ledger record into the nested ActiveWorktree.

    Schema validation and compatibility defaults belong to ``run_state``. This
    function only distributes normalized flat values onto the owner subrecords;
    keys absent from ``value`` take the subrecord defaults.
    """
    if not isinstance(value, Mapping):
        raise TypeError("active worktree record must be a mapping")
    core_values = _group_values(value, ActiveWorktreeCore)
    if "declared_footprint" in core_values:
        core_values["declared_footprint"] = tuple(core_values["declared_footprint"])
    return ActiveWorktree(
        core=ActiveWorktreeCore(**core_values),
        launch=LaunchInfo(**_group_values(value, LaunchInfo), _legacy=True),
        claim=ClaimInfo(**_group_values(value, ClaimInfo), _legacy=True),
        completion=ActiveCompletionJournal(
            **_group_values(value, ActiveCompletionJournal), _legacy=True
        ),
    )


def encode_active_worktree(active: ActiveWorktree) -> dict[str, Any]:
    """Encode the nested ActiveWorktree as the established flat JSON object.

    Iteration follows the frozen ``_ACTIVE_FIELD_NAMES`` schema order.
    Footprints and nested JSON values are copied back to ordinary list/dict
    containers, and a null ``completion_policy_config`` retains its historical
    omission.
    """
    if not isinstance(active, ActiveWorktree):
        raise TypeError("active must be ActiveWorktree")
    records = (active.core, active.launch, active.claim, active.completion)
    flat = {
        record_field.name: getattr(record, record_field.name)
        for record in records
        for record_field in fields(record)
    }
    encoded: dict[str, Any] = {}
    for name in _ACTIVE_FIELD_NAMES:
        value = flat[name]
        if name in _OMIT_WHEN_NULL and value is None:
            continue
        if name == "declared_footprint":
            encoded[name] = list(value)
        else:
            encoded[name] = _thaw_json(value)
    return encoded
