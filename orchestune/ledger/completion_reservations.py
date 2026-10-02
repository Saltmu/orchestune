"""Conservative completion exclusion shared by writers and dependency readers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestune.ledger.run_state import load_run_state_readonly

_IDENTITY = (
    "repository_id",
    "issue_number",
    "generation_id",
    "completion_id",
    "owner_token_digest",
    "request_fingerprint",
    "result",
    "target_label",
    "outcome_payload",
)


def _records(state: Any, issue_number: int) -> list[dict[str, Any]]:
    raw = getattr(state, "completion_reservations", {})
    if not isinstance(raw, dict):
        raise ValueError("Invalid completion reservations")
    records = []
    for key, value in raw.items():
        if not isinstance(value, dict):
            raise ValueError("Invalid completion reservation")
        if value.get("issue_number") == issue_number:
            if key != f"{value.get('repository_id')}::{issue_number}":
                raise ValueError("Invalid reservation key")
            records.append(value)
    return records


def completion_record(state: Any, issue_number: int) -> dict[str, Any] | None:
    """Resolve an Issue reservation and require its immutable journal identity."""
    records = _records(state, issue_number)
    if not records:
        return None
    if len(records) != 1:
        raise ValueError("Ambiguous completion reservation")
    reservation = records[0]
    if any(name not in reservation for name in _IDENTITY):
        raise ValueError("Incomplete completion reservation")
    key = "::".join(str(reservation[name]) for name in _IDENTITY[:4])
    record = getattr(state, "completion_journal", {}).get(key)
    if not isinstance(record, dict) or any(
        record.get(name) != reservation[name] for name in _IDENTITY
    ):
        raise ValueError("Completion journal identity mismatch")
    return record


def completion_reservation_status(state: Any, issue_number: int) -> str:
    """Return legacy, pending, handed_off, or invalid; unknown proof never releases."""
    try:
        record = completion_record(state, issue_number)
        if record is None:
            return "legacy"
        stage = record.get("stage")
        if stage in {"reserved", "outcome_posted", "label_confirmed"}:
            return "pending"
        if stage != "handed_off":
            return "invalid"
        posting, labels = record.get("posting_evidence"), record.get("label_evidence")
        if (
            not isinstance(posting, dict)
            or not posting.get("comment_id")
            or not posting.get("comment_url")
        ):
            return "invalid"
        if (
            not isinstance(labels, dict)
            or labels.get("status") != "confirmed"
            or record["target_label"] not in labels.get("observed_labels", [])
        ):
            return "invalid"
        key = "::".join(str(record[name]) for name in _IDENTITY[:4])
        receipt = getattr(state, "completion_replay_receipts", {}).get(key)
        core = {k: v for k, v in record.items() if k != "downstream_policy_records"}
        if (
            not isinstance(receipt, dict)
            or {k: v for k, v in receipt.items() if k != "downstream_policy_records"}
            != core
        ):
            return "invalid"
        return "handed_off"
    except (KeyError, TypeError, ValueError):
        return "invalid"


def completion_mutation_blocked(state: Any, issue_number: int) -> bool:
    return completion_reservation_status(state, issue_number) in {"pending", "invalid"}


def completion_mutation_blocked_fresh(
    state: Any, issue_number: int, state_path: Path | str
) -> bool:
    """Check current transaction data and persisted reservation after its lock boundary."""
    if completion_mutation_blocked(state, issue_number):
        return True
    try:
        return completion_mutation_blocked(
            load_run_state_readonly(state_path), issue_number
        )
    except (OSError, ValueError):
        return True


def dependency_completion_blocked(state: Any, issue_number: int) -> bool:
    if completion_mutation_blocked(state, issue_number):
        return True
    try:
        record = completion_record(state, issue_number)
        if record is None:
            return False
        policies = record.get("downstream_policy_records", {})
        if not isinstance(policies, dict):
            return True
        return any(
            not isinstance(policy, dict)
            or (
                policy.get("policy_kind") == "not-needed-review"
                and policy.get("status") != "applied"
            )
            for policy in policies.values()
        )
    except (TypeError, ValueError):
        return True


def completion_handoff_matches_active(state: Any, active: Any) -> bool:
    if completion_reservation_status(state, active.core.issue_number) != "handed_off":
        return False
    record = completion_record(state, active.core.issue_number)
    assert record is not None
    posting = record["posting_evidence"]
    return all(
        (
            record["repository_id"] == active.claim.repository_id,
            record["generation_id"] == active.claim.claim_id,
            record["completion_id"] == active.completion.completion_id,
            record["result"] == active.completion.completion_result,
            record["owner_token_digest"] == active.claim.owner_token_digest,
            posting["comment_id"] == active.completion.completion_comment_id,
            posting["comment_url"] == active.completion.completion_comment_url,
        )
    )


def completion_subject_mutation_blocked(
    state: Any, subject_id: str | None, state_path: Path | str
) -> bool:
    return bool(
        subject_id
        and subject_id.isdigit()
        and completion_mutation_blocked_fresh(state, int(subject_id), state_path)
    )


def dependency_completion_blocked_fresh(
    state: Any, issue_number: int, state_path: Path | str
) -> bool:
    if dependency_completion_blocked(state, issue_number):
        return True
    try:
        return dependency_completion_blocked(
            load_run_state_readonly(state_path), issue_number
        )
    except (OSError, ValueError, TypeError):
        return True
