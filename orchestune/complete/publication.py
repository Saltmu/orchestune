"""One locked, resumable publication transaction for a fixed completion payload."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orchestune.complete.contracts import (
    CompleteFailureReason,
    CompleteRequest,
    CompleteResult,
    CompleteStage,
    DownstreamPolicyRecord,
)
from orchestune.complete.journal import (
    CompletionJournalError,
    CompletionJournalRecord,
    CompletionReplayReceipt,
    _save_or_raise,
)
from orchestune.complete.posting import PostingRequest, post_issue_outcome
from orchestune.complete.replay import result_from_record
from orchestune.complete.status_labels import transition_completion_status_label
from orchestune.infra.process_utils import assert_run_state_lock_held
from orchestune.labels import StatusLabel
from orchestune.ledger.run_state import load_run_state_readonly
from orchestune.ledger.status_labels import PRIMARY_STATUS_LABELS
from orchestune.outcome_record import parse_from_comments


@dataclass(frozen=True)
class PublicationContext:
    request: CompleteRequest
    state_path: Path
    worktree: Path | None
    forge: Any
    active: Any | None = None


def _save(
    context: PublicationContext,
    record: CompletionJournalRecord,
    *,
    handoff: bool = False,
) -> None:
    state = load_run_state_readonly(context.state_path)
    state.completion_journal[record.journal_key] = record.to_dict()
    active = state.active_worktrees.get(str(record.issue_number))
    if active is not None:
        if active.claim_id != record.generation_id:
            raise CompletionJournalError(
                CompleteFailureReason.GENERATION_MISMATCH,
                "Claim changed during publication",
            )
        active.completion_id = record.completion_id
        active.completion_result = record.result
        active.completion_stage = record.stage.value
        active.completion_payload = record.outcome_payload
        if record.posting_evidence:
            active.completion_comment_id = record.posting_evidence["comment_id"]
            active.completion_comment_url = record.posting_evidence["comment_url"]
        active.completion_handoff_ready = handoff
    if handoff:
        state.completion_replay_receipts[record.receipt_key] = CompletionReplayReceipt(
            record
        ).to_dict()
    _save_or_raise(state, context.state_path)


def generation_matches(
    context: PublicationContext, record: CompletionJournalRecord
) -> bool:
    state = load_run_state_readonly(context.state_path)
    reservation = state.completion_reservations.get(record.reservation_key)
    if (
        not reservation
        or reservation.get("generation_id") != record.generation_id
        or reservation.get("completion_id") != record.completion_id
    ):
        return False
    active = state.active_worktrees.get(str(record.issue_number))
    if context.active is not None:
        return (
            active is not None
            and active.repository_id == record.repository_id
            and active.claim_id == record.generation_id
            and active.owner_token_digest == record.owner_token_digest
        )
    return active is None


def _confirm_labels(
    context: PublicationContext, record: CompletionJournalRecord
) -> CompletionJournalRecord:
    labels = transition_completion_status_label(
        context.forge,
        record.issue_number,
        record.target_label,
        generation_matches=lambda: generation_matches(context, record),
    )
    if not labels.confirmed:
        raise CompletionJournalError(
            labels.failure_reason or CompleteFailureReason.LABEL_STATE_UNKNOWN,
            f"Label transition failed: {labels.status.value}",
        )
    evidence = {
        "status": labels.status.value,
        "target_label": labels.target_label,
        "observed_labels": list(labels.observed_labels),
    }
    record = replace(
        record, stage=CompleteStage.LABEL_CONFIRMED, label_evidence=evidence
    )
    _save(context, record)
    return record


def publish_reserved_completion_locked(
    context: PublicationContext, record: CompletionJournalRecord
) -> CompleteResult:
    """Publish under an already-held common lock; saved fixed bodies survive retries."""
    assert_run_state_lock_held(context.state_path.with_suffix(".lock"))
    if not generation_matches(context, record):
        raise CompletionJournalError(
            CompleteFailureReason.GENERATION_MISMATCH,
            "Completion generation no longer matches",
        )
    outcome = parse_from_comments([{"body": record.outcome_payload["body"]}])
    if outcome is None:
        raise CompletionJournalError(
            CompleteFailureReason.INVALID_COMPLETION_STATE, "Fixed outcome is malformed"
        )
    if record.stage is CompleteStage.RESERVED:
        labels = context.forge.get_issue_labels(record.issue_number)
        allowed = {
            *PRIMARY_STATUS_LABELS,
            record.target_label,
            StatusLabel.FORCE_SERIAL,
        }
        if any(
            label.startswith("status:") and label not in allowed for label in labels
        ):
            raise CompletionJournalError(
                CompleteFailureReason.LABEL_CONFLICT,
                "Protected status prevents outcome publication",
            )
        posted = post_issue_outcome(
            PostingRequest(record.issue_number, outcome), forge=context.forge
        )
        record = record.advance(
            CompleteStage.OUTCOME_POSTED,
            posting_evidence={
                "comment_id": posted.comment_id,
                "comment_url": posted.comment_url,
            },
        )
        _save(context, record)
    if record.stage in {CompleteStage.OUTCOME_POSTED, CompleteStage.LABEL_CONFIRMED}:
        record = _confirm_labels(context, record)
    if record.stage is CompleteStage.LABEL_CONFIRMED:
        record = record.advance(CompleteStage.HANDED_OFF)
        _save(context, record, handoff=True)
    return result_from_record(record, context.request)


def update_downstream_policy_locked(
    state_path: Path, record: CompletionJournalRecord, policy: DownstreamPolicyRecord
) -> CompletionJournalRecord:
    """Insert/update durable downstream context without dropping immutable replay data."""
    assert_run_state_lock_held(state_path.with_suffix(".lock"))
    state = load_run_state_readonly(state_path)
    current = CompletionJournalRecord.from_dict(
        state.completion_journal[record.journal_key]
    )
    policies = {p.policy_kind: p for p in current.downstream_policy_records}
    policies[policy.policy_kind] = policy
    updated = replace(current, downstream_policy_records=tuple(policies.values()))
    state.completion_journal[updated.journal_key] = updated.to_dict()
    if updated.receipt_key in state.completion_replay_receipts:
        state.completion_replay_receipts[updated.receipt_key] = updated.to_dict()
    _save_or_raise(state, state_path)
    return updated
