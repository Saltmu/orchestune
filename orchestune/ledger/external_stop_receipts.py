"""Strict, generation-bound operator stop evidence shared by recovery and GC."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from orchestune.ledger.active_codec import encode_active_worktree
from orchestune.ledger.active_records import ActiveWorktree
from orchestune.ledger.recovery_receipts import (
    EXTERNAL_STOP_OPERATION,
    ExternalStopConfirmation,
    RecoveryActiveSnapshot,
    RecoveryExecutionIdentity,
)
from orchestune.ledger.run_state import RunState


def execution_identity(active: ActiveWorktree) -> RecoveryExecutionIdentity:
    """Extract the fixed execution identity from the typed active record."""
    claim, launch = active.claim, active.launch
    if not claim.repository_id or not claim.claim_id or not active.core.branch:
        raise ValueError("external execution identity invalid")
    external_id = launch.external_id
    if not external_id:
        raise ValueError("external execution identity invalid")
    return RecoveryExecutionIdentity(
        repository_id=claim.repository_id,
        issue_number=active.core.issue_number,
        claim_id=claim.claim_id,
        owner_token_digest=claim.owner_token_digest,
        claimed_at=claim.claimed_at,
        branch=active.core.branch,
        launch_attempt_id=launch.launch_attempt_id,
        external_id=external_id,
        started_at=active.launch.started_at,
    )


def same_execution(first: ActiveWorktree, second: ActiveWorktree) -> bool:
    return all(
        left == right
        for left, right in (
            (first.claim.repository_id, second.claim.repository_id),
            (first.core.issue_number, second.core.issue_number),
            (first.claim.claim_id, second.claim.claim_id),
            (first.claim.owner_token_digest, second.claim.owner_token_digest),
            (first.claim.claimed_at, second.claim.claimed_at),
            (first.core.branch, second.core.branch),
            (first.launch.launch_attempt_id, second.launch.launch_attempt_id),
            (first.launch.external_id, second.launch.external_id),
            (first.launch.started_at, second.launch.started_at),
        )
    )


def identity_key(identity: RecoveryExecutionIdentity) -> str:
    canonical = json.dumps(
        identity.to_json(), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return (
        "external-stop-confirmation::" + hashlib.sha256(canonical.encode()).hexdigest()
    )


def confirmation_key(active: ActiveWorktree) -> str:
    return identity_key(execution_identity(active))


def confirmation_record(
    active: ActiveWorktree, reason: str
) -> ExternalStopConfirmation:
    identity = execution_identity(active)
    snapshot = encode_active_worktree(active)
    snapshot.setdefault("completion_policy_config", None)
    return ExternalStopConfirmation(
        schema_version=1,
        repository_id=identity.repository_id,
        issue_number=identity.issue_number,
        claim_id=identity.claim_id,
        operation=EXTERNAL_STOP_OPERATION,
        source="operator",
        confirmed_external_stopped=True,
        reason=reason.strip(),
        recorded_at=datetime.now(UTC).isoformat(),
        launch_attempt_id=identity.launch_attempt_id,
        external_id=identity.external_id,
        execution_identity=identity,
        active=RecoveryActiveSnapshot.from_json(snapshot),
        worktree_action="retain",
    )


def valid_confirmation(key: str, record: object) -> bool:
    if not isinstance(record, ExternalStopConfirmation):
        return False
    identity = record.execution_identity
    try:
        recorded = datetime.fromisoformat(record.recorded_at)
        validated_identity = RecoveryExecutionIdentity.from_json(identity.to_json())
        active_identity = execution_identity(record.active.value)
    except (KeyError, ValueError, TypeError, OverflowError):
        return False
    return (
        isinstance(record.schema_version, int)
        and not isinstance(record.schema_version, bool)
        and isinstance(record.issue_number, int)
        and not isinstance(record.issue_number, bool)
        and isinstance(record.repository_id, str)
        and isinstance(record.claim_id, str)
        and isinstance(record.reason, str)
        and isinstance(record.external_id, str)
        and (
            record.launch_attempt_id is None
            or isinstance(record.launch_attempt_id, str)
        )
        and key == identity_key(identity)
        and validated_identity == identity
        and record.schema_version == 1
        and record.operation == EXTERNAL_STOP_OPERATION
        and record.source == "operator"
        and record.confirmed_external_stopped is True
        and bool(record.reason.strip())
        and bool(record.external_id.strip())
        and bool(record.recorded_at)
        and recorded.tzinfo is not None
        and recorded.utcoffset() == UTC.utcoffset(recorded)
        and record.worktree_action == "retain"
        and record.active.value.claim.claim_id == identity.claim_id
        and record.active.value.core.issue_number == identity.issue_number
        and active_identity == identity
        and record.repository_id == identity.repository_id
        and record.issue_number == identity.issue_number
        and record.claim_id == identity.claim_id
        and record.launch_attempt_id == identity.launch_attempt_id
        and record.external_id == identity.external_id
    )


def matching_confirmation(
    state: RunState, active: ActiveWorktree, repository_id: str
) -> bool:
    try:
        identity = execution_identity(active)
        key = identity_key(identity)
    except (KeyError, ValueError, TypeError, OverflowError):
        return False
    record = state.recovery_receipts.get(key)
    if identity.repository_id != repository_id or not valid_confirmation(key, record):
        return False
    return (
        isinstance(record, ExternalStopConfirmation)
        and record.execution_identity == identity
    )
