"""Discover verified completions independently of Issue status and active entries."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from orchestune.claim.ownership import owner_token_digest
from orchestune.complete.contracts import CompleteStage, DownstreamPolicyRecord
from orchestune.complete.journal_models import CompletionJournalRecord
from orchestune.dispatch.cycle_events import TokenHoldCompletion
from orchestune.infra.private_tokens import _read_owner_token, _token_record_path
from orchestune.infra.process_utils import assert_run_state_lock_held
from orchestune.ledger.completion_reservations import (
    completion_handoff_matches_active,
    completion_record,
    completion_reservation_status,
)
from orchestune.ledger.run_state import RunState
from orchestune.outcome_record import OutcomeRecord, parse_from_comments


def confirmed_records(state: RunState) -> list[CompletionJournalRecord]:
    records = []
    for reservation in state.completion_reservations.values():
        issue = reservation["issue_number"]
        if completion_reservation_status(state, issue) != "handed_off":
            continue
        active = state.active_worktrees.get(str(issue))
        if active is not None and (
            not active.completion.completion_handoff_ready
            or active.completion.completion_stage != CompleteStage.HANDED_OFF.value
            or not completion_handoff_matches_active(state, active)
        ):
            continue
        raw = completion_record(state, issue)
        if raw is not None:
            records.append(CompletionJournalRecord.from_dict(raw))
    return records


def journal_outcome(record: CompletionJournalRecord) -> OutcomeRecord | None:
    body = record.outcome_payload.get("body", record.outcome_payload.get("outcome"))
    outcome = parse_from_comments([{"body": body}]) if isinstance(body, str) else None
    context = (record.prepublication_policy_evidence or {}).get("context", {})
    expected_claim = (
        None
        if (
            context.get("unclaimed") is True
            and record.result == "not-needed"
            and record.generation_id.startswith("unclaimed-")
        )
        else record.generation_id
    )
    if outcome is None or (
        outcome.issue,
        outcome.claim_id,
        outcome.completion_id,
        outcome.result,
    ) != (
        record.issue_number,
        expected_claim,
        record.completion_id,
        record.result,
    ):
        return None
    return outcome


def policy_kind(record: CompletionJournalRecord, outcome: OutcomeRecord) -> str | None:
    if record.result == "not-needed":
        context = (record.prepublication_policy_evidence or {}).get("context", {})
        active = context.get("active")
        # Missing context is never an independent review exemption.
        return (
            "not-needed-close"
            if isinstance(active, dict) and active.get("external_id") is None
            else "not-needed-review"
        )
    if record.result == "blocked" and outcome.reason in {
        "review-timeout",
        "base-branch-red",
    }:
        return outcome.reason
    return None


def ensure_policy(
    record: CompletionJournalRecord, kind: str
) -> CompletionJournalRecord:
    if any(p.policy_kind == kind for p in record.downstream_policy_records):
        return record
    policy = DownstreamPolicyRecord(
        record.repository_id,
        record.issue_number,
        record.generation_id,
        record.completion_id,
        kind,
        metadata={
            "context": (record.prepublication_policy_evidence or {}).get("context", {})
        },
    )
    return replace(
        record, downstream_policy_records=(*record.downstream_policy_records, policy)
    )


def update_record(state: RunState, record: CompletionJournalRecord) -> None:
    state.completion_journal[record.journal_key] = record.to_dict()
    state.completion_replay_receipts[record.receipt_key] = record.to_dict()


def update_policy(
    record: CompletionJournalRecord, policy: DownstreamPolicyRecord, **metadata: Any
) -> CompletionJournalRecord:
    updated = replace(policy, metadata={**policy.metadata, **metadata})
    return replace(
        record,
        downstream_policy_records=tuple(
            updated if p.policy_kind == policy.policy_kind else p
            for p in record.downstream_policy_records
        ),
    )


def policy_candidates(state: RunState) -> list[CompletionJournalRecord]:
    records = []
    for record in confirmed_records(state):
        outcome = journal_outcome(record)
        if str(record.issue_number) in state.active_worktrees or (
            outcome is not None
            and policy_kind(record, outcome) is not None
            and (
                not record.downstream_policy_records
                or any(p.status != "applied" for p in record.downstream_policy_records)
            )
        ):
            records.append(record)
    return records


def reclaim_completed_tokens(
    state: RunState, state_path: Path, repository_id: str
) -> list[TokenHoldCompletion]:
    """Reclaim a matching credential after receipt persistence and active release."""
    assert_run_state_lock_held(state_path.with_suffix(".lock"))
    events = []
    for raw in state.completion_replay_receipts.values():
        record = CompletionJournalRecord.from_dict(raw)
        active = state.active_worktrees.get(str(record.issue_number))
        if record.stage is not CompleteStage.HANDED_OFF:
            continue
        if record.repository_id != repository_id or (
            active and active.claim.claim_id == record.generation_id
        ):
            continue
        journal = state.completion_journal.get(record.journal_key)
        if journal != raw or (record.label_evidence or {}).get("status") != "confirmed":
            continue
        context = (record.prepublication_policy_evidence or {}).get("context", {})
        unclaimed = context.get("unclaimed") is True
        directory = (
            state_path.parent
            / ".orchestune"
            / ("completion-tokens" if unclaimed else "claim-tokens")
        )
        identifier = record.completion_id if unclaimed else record.generation_id
        try:
            token_path = _token_record_path(directory, identifier)
            if not token_path.exists() and not token_path.is_symlink():
                continue
            token = (
                _read_owner_token(directory, identifier)
                if not token_path.is_symlink()
                else None
            )
            if token is None or owner_token_digest(token) != record.owner_token_digest:
                raise ValueError("owner_token_mismatch")
            token_path.unlink()
        except (OSError, ValueError) as error:
            events.append(
                TokenHoldCompletion(
                    issue_number=record.issue_number,
                    reason=type(error).__name__,
                )
            )
    return events
