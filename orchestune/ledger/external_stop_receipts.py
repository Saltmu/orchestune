"""Strict, generation-bound operator stop evidence shared by recovery and GC."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from math import isfinite
from typing import Any

from orchestune.ledger.active_codec import encode_active_worktree
from orchestune.ledger.run_state import ActiveWorktree, RunState

IDENTITY_FIELDS = (
    "repository_id",
    "issue_number",
    "claim_id",
    "owner_token_digest",
    "claimed_at",
    "branch",
    "launch_attempt_id",
    "external_id",
    "started_at",
)


def _identity(snapshot: dict[str, Any]) -> dict[str, Any]:
    identity = {name: snapshot[name] for name in IDENTITY_FIELDS}
    for name in ("repository_id", "claim_id", "branch", "external_id"):
        if not isinstance(identity[name], str) or not identity[name].strip():
            raise ValueError("external execution identity invalid")
    issue = identity["issue_number"]
    if not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0:
        raise ValueError("external execution identity invalid")
    for name in ("owner_token_digest", "launch_attempt_id"):
        value = identity[name]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("external execution identity invalid")
    for name in ("claimed_at", "started_at"):
        value = identity[name]
        if value is not None:
            if type(value) not in (int, float) or not isfinite(value):
                raise ValueError("external execution identity invalid")
            identity[name] = float(value)
    return identity


def execution_identity(active: ActiveWorktree) -> dict[str, Any]:
    return _identity(encode_active_worktree(active))


def same_execution(first: ActiveWorktree, second: ActiveWorktree) -> bool:
    """Compare fixed fields, including local/PR-derived identities, without codec leakage."""
    left, right = encode_active_worktree(first), encode_active_worktree(second)
    return all(left[n] == right[n] for n in IDENTITY_FIELDS)


def identity_key(identity: dict[str, Any]) -> str:
    canonical = json.dumps(
        identity, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return (
        "external-stop-confirmation::" + hashlib.sha256(canonical.encode()).hexdigest()
    )


def confirmation_key(active: ActiveWorktree) -> str:
    return identity_key(execution_identity(active))


def confirmation_record(active: ActiveWorktree, reason: str) -> dict[str, Any]:
    identity = execution_identity(active)
    snapshot = encode_active_worktree(active)
    snapshot.setdefault("completion_policy_config", None)
    return {
        "schema_version": 1,
        "operation": "external-stop-confirmation",
        "source": "operator",
        "confirmed_external_stopped": True,
        "reason": reason.strip(),
        "recorded_at": datetime.now(UTC).isoformat(),
        **{
            k: identity[k]
            for k in (
                "repository_id",
                "issue_number",
                "claim_id",
                "launch_attempt_id",
                "external_id",
            )
        },
        "execution_identity": identity,
        "active": snapshot,
        "worktree_action": "retain",
    }


def valid_confirmation(key: str, record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    try:
        identity = record["execution_identity"]
        if not isinstance(identity, dict) or set(identity) != set(IDENTITY_FIELDS):
            return False
        if _identity(identity) != identity or identity_key(_identity(identity)) != key:
            return False
        snapshot = record["active"]
        if not isinstance(snapshot, dict) or _identity(snapshot) != identity:
            return False
        recorded = datetime.fromisoformat(record["recorded_at"])
        return (
            isinstance(record["schema_version"], int)
            and not isinstance(record["schema_version"], bool)
            and record["schema_version"] == 1
            and record["operation"] == "external-stop-confirmation"
            and record["source"] == "operator"
            and record["confirmed_external_stopped"] is True
            and isinstance(record["reason"], str)
            and bool(record["reason"].strip())
            and recorded.tzinfo is not None
            and recorded.utcoffset() == UTC.utcoffset(recorded)
            and record["worktree_action"] == "retain"
            and all(
                isinstance(record[name], type(identity[name]))
                and record[name] == identity[name]
                for name in (
                    "repository_id",
                    "issue_number",
                    "claim_id",
                    "launch_attempt_id",
                    "external_id",
                )
            )
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def matching_confirmation(
    state: RunState, active: ActiveWorktree, repository_id: str
) -> bool:
    try:
        identity = execution_identity(active)
        key = identity_key(identity)
    except (KeyError, ValueError, TypeError, OverflowError):
        return False
    record = state.recovery_receipts.get(key)
    return (
        identity["repository_id"] == repository_id
        and isinstance(record, dict)
        and valid_confirmation(key, record)
        and record["execution_identity"] == identity
    )
