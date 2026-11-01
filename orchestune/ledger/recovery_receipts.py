"""Durable operator-style release receipts shared by recovery and GC (#1526).

Both ``orchestune recover`` and GC's closed-target release record the same
receipt so ``claim_was_released`` keeps recovery from resurrecting a released
claim generation. The helpers only build data; callers own locking and writes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from orchestune.ledger.active_codec import encode_active_worktree
from orchestune.ledger.run_state import ActiveWorktree

RELEASE_OPERATION = "release"


def recovery_receipt_key(
    repository_id: str, issue_number: int, claim_id: str | None, operation: str
) -> str:
    return f"{repository_id}::{issue_number}::{claim_id}::{operation}"


def legacy_active_snapshot(active: ActiveWorktree) -> dict[str, Any]:
    """Flat receipt snapshot; unlike the ledger, it keeps a null policy config."""
    snapshot = encode_active_worktree(active)
    snapshot.setdefault("completion_policy_config", None)
    return snapshot


def build_recovery_receipt(
    *,
    repository_id: str,
    issue_number: int,
    active: ActiveWorktree,
    operation: str,
    reason: str | None,
) -> tuple[str, dict[str, Any]]:
    """Return the receipt key and body for a retained-worktree recovery."""
    key = recovery_receipt_key(
        repository_id, issue_number, active.claim.claim_id, operation
    )
    return key, {
        "schema_version": 1,
        "repository_id": repository_id,
        "issue_number": issue_number,
        "claim_id": active.claim.claim_id,
        "operation": operation,
        "reason": reason,
        "recorded_at": datetime.now(UTC).isoformat(),
        "worktree_action": "retain",
        "active": legacy_active_snapshot(active),
    }


def record_recovery_in_raw(
    raw: dict[str, Any],
    key: str,
    active: ActiveWorktree,
    *,
    repository_id: str,
    issue_number: int,
    operation: str,
    reason: str | None,
) -> None:
    """Apply a recovery to raw run_state data, preserving unrelated records.

    A ``release`` also removes the active entry. Raises ``KeyError`` when that
    entry vanished, so a caller never records a release it did not perform.
    """
    if operation == RELEASE_OPERATION:
        del raw["active_worktrees"][key]
    receipt_key, receipt = build_recovery_receipt(
        repository_id=repository_id,
        issue_number=issue_number,
        active=active,
        operation=operation,
        reason=reason,
    )
    raw.setdefault("recovery_receipts", {})[receipt_key] = receipt


__all__ = [
    "RELEASE_OPERATION",
    "build_recovery_receipt",
    "legacy_active_snapshot",
    "record_recovery_in_raw",
    "recovery_receipt_key",
]
