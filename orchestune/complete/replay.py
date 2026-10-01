"""Immutable replay receipt lookup before ownership and remote validation."""

from __future__ import annotations

from typing import Any

from orchestune.complete.contracts import (
    CompleteFailureReason,
    CompleteRequest,
    CompleteResult,
    CompleteStage,
)
from orchestune.complete.journal import (
    CompletionJournalError,
    CompletionJournalRecord,
    CompletionReplayReceipt,
)
from orchestune.outcome_record import parse_from_comments


def result_from_record(
    record: CompletionJournalRecord, request: CompleteRequest
) -> CompleteResult:
    outcome = parse_from_comments([{"body": record.outcome_payload["body"]}])
    if outcome is None:
        raise CompletionJournalError(
            CompleteFailureReason.INVALID_COMPLETION_STATE,
            "Saved outcome payload is not parseable",
        )
    return CompleteResult.success_result(
        record.issue_number,
        record.result,
        CompleteStage.HANDED_OFF,
        claim_id=record.generation_id,
        owner_kind=request.owner_kind,
        pr=outcome.pr,
        outcome_record=outcome,
        completion_id=record.completion_id,
    )


def find_replay(
    request: CompleteRequest, state: Any, repository_id: str
) -> CompleteResult | None:
    receipts = list(state.completion_replay_receipts.values())
    explicit = request.completion_id
    matches = [
        raw
        for raw in receipts
        if (
            raw.get("completion_id") == explicit
            if explicit
            else raw.get("repository_id") == repository_id
            and raw.get("issue_number") == request.issue_number
        )
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise CompletionJournalError(
            CompleteFailureReason.GENERATION_MISMATCH,
            "Ambiguous completion history; specify --completion-id",
        )
    record = CompletionReplayReceipt.from_dict(matches[0]).record
    if (
        record.repository_id != repository_id
        or record.issue_number != request.issue_number
    ):
        raise CompletionJournalError(
            CompleteFailureReason.REPOSITORY_IDENTITY_MISMATCH,
            "Completion ID belongs to a different repository or Issue",
        )
    if record.request_fingerprint != request.request_fingerprint:
        raise CompletionJournalError(
            CompleteFailureReason.REQUEST_FINGERPRINT_MISMATCH,
            "Saved completion request differs",
        )
    _validate_replay_generation(request, state, record)
    return result_from_record(record, request)


def _validate_replay_generation(
    request: CompleteRequest, state: Any, record: CompletionJournalRecord
) -> None:
    explicit = request.completion_id
    active = state.active_worktrees.get(str(request.issue_number))
    if not explicit and (
        record.generation_id.startswith("unclaimed-")
        or (
            request.result == "not-needed"
            and active is None
            and request.claim_id is None
        )
    ):
        raise CompletionJournalError(
            CompleteFailureReason.GENERATION_MISMATCH,
            f"Unclaimed history requires --completion-id {record.completion_id}; "
            "use a new completion-<UUID hex> for an explicitly new request after reopening",
        )
    active_claim_id = (
        active.claim.claim_id
        if getattr(active, "claim", None) is not None
        else getattr(active, "claim_id", None)
    )
    if not explicit and active is not None and active_claim_id != record.generation_id:
        raise CompletionJournalError(
            CompleteFailureReason.GENERATION_MISMATCH,
            "Task was reclaimed; specify the old completion ID to replay its saved result",
        )
