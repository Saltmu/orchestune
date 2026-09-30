"""Issue-level completion ownership without allocating a branch or worktree."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from orchestune.claim.ownership import new_owner_token, owner_token_digest
from orchestune.complete.contracts import (
    CompleteFailureReason,
    CompleteRequest,
    CompleteResult,
    CompleteStage,
)
from orchestune.complete.journal import (
    CompletionJournalError,
    CompletionJournalRecord,
    completion_journal_lock,
    reserve_completion_locked,
)
from orchestune.complete.not_needed_policy import not_needed_policies
from orchestune.complete.publication import (
    PublicationContext,
    publish_reserved_completion_locked,
)
from orchestune.complete.replay import find_replay
from orchestune.labels import StatusLabel
from orchestune.ledger.completion_reservations import (
    completion_record,
    completion_reservation_status,
)
from orchestune.ledger.run_state import load_run_state_readonly
from orchestune.ledger.status_labels import PRIMARY_STATUS_LABELS


def token_directory(state_path: Path) -> Path:
    return state_path.parent / ".orchestune" / "completion-tokens"


def _reject(reason: CompleteFailureReason, message: str) -> None:
    raise CompletionJournalError(reason, message)


def _validate_id(completion_id: str) -> None:
    try:
        value = UUID(completion_id.removeprefix("completion-"))
        if completion_id != f"completion-{value.hex}":
            raise ValueError("Noncanonical UUID")
    except ValueError:
        _reject(
            CompleteFailureReason.GENERATION_MISMATCH,
            "New unclaimed completion IDs must be completion-<UUID hex>; legacy IDs cannot be adopted",
        )


def _validate_new_id(request: CompleteRequest, state: Any) -> None:
    assert request.completion_id is not None
    _validate_id(request.completion_id)
    if any(
        raw.get("completion_id") == request.completion_id
        for raw in state.completion_journal.values()
    ):
        _reject(
            CompleteFailureReason.GENERATION_MISMATCH,
            "Completion ID is already used; specify its matching Issue or choose a fresh UUID",
        )


def _issue_evidence(forge: Any, issue_number: int, *, pending: bool) -> dict[str, Any]:
    try:
        labels = tuple(forge.get_issue_labels(issue_number))
        state = forge.get_issue_state(issue_number)
    except Exception as error:
        raise CompletionJournalError(
            CompleteFailureReason.EVIDENCE_MISSING, "Issue evidence is unavailable"
        ) from error
    allowed = {*PRIMARY_STATUS_LABELS, StatusLabel.FORCE_SERIAL}
    if pending:
        allowed.add(StatusLabel.NOT_NEEDED)
    if state != "OPEN" or any(
        label.startswith("status:") and label not in allowed for label in labels
    ):
        _reject(
            CompleteFailureReason.LABEL_CONFLICT,
            "Issue is closed or has a protected status",
        )
    if StatusLabel.DONE in labels or (not pending and StatusLabel.NOT_NEEDED in labels):
        _reject(
            CompleteFailureReason.GENERATION_MISMATCH,
            "Issue is already terminal; specify its saved completion ID",
        )
    return {"labels": list(labels), "state": state}


def _pending(
    request: CompleteRequest, state: Any, repository: str
) -> CompletionJournalRecord | None:
    try:
        raw = completion_record(state, request.issue_number)
    except ValueError as error:
        raise CompletionJournalError(
            CompleteFailureReason.INVALID_COMPLETION_STATE, str(error)
        ) from error
    if raw is None:
        return None
    record = CompletionJournalRecord.from_dict(raw)
    if record.repository_id != repository:
        _reject(
            CompleteFailureReason.REPOSITORY_IDENTITY_MISMATCH,
            "Reservation belongs to another repository",
        )
    if record.stage is CompleteStage.HANDED_OFF:
        if completion_reservation_status(state, request.issue_number) != "handed_off":
            _reject(
                CompleteFailureReason.INVALID_COMPLETION_STATE,
                "Completed reservation lacks matching durable proof",
            )
        return None
    _validate_pending_identity(request, record)
    return record


def _validate_pending_identity(
    request: CompleteRequest, record: CompletionJournalRecord
) -> None:
    if not record.generation_id.startswith("unclaimed-"):
        _reject(
            CompleteFailureReason.GENERATION_MISMATCH,
            "A claimed completion cannot resume without its claim",
        )
    _validate_id(record.completion_id)
    if (
        record.generation_id
        != f"unclaimed-{record.completion_id.removeprefix('completion-')}"
    ):
        _reject(
            CompleteFailureReason.GENERATION_MISMATCH,
            "Unclaimed generation differs from completion identity",
        )
    if (
        request.completion_id is not None
        and request.completion_id != record.completion_id
    ):
        _reject(
            CompleteFailureReason.GENERATION_MISMATCH,
            "A different completion ID is reserved",
        )
    if record.request_fingerprint != request.request_fingerprint:
        _reject(
            CompleteFailureReason.REQUEST_FINGERPRINT_MISMATCH,
            "Reserved request differs",
        )


def _authenticate(
    request: CompleteRequest, record: CompletionJournalRecord, state_path: Path
) -> str:
    if request.completion_id != record.completion_id:
        _reject(
            CompleteFailureReason.GENERATION_MISMATCH,
            f"Resume this local completion with --completion-id {record.completion_id}",
        )
    return ""


def _new_record(
    request: CompleteRequest,
    repository: str,
    completion_id: str,
    token: str,
    evidence: dict[str, Any],
) -> CompletionJournalRecord:
    generation = f"unclaimed-{completion_id.removeprefix('completion-')}"
    outcome = replace(request.to_outcome_record(), completion_id=completion_id)
    context = {"unclaimed": True, "repository_id": repository}
    return CompletionJournalRecord(
        repository,
        request.issue_number,
        generation,
        completion_id,
        owner_token_digest(token),
        request.request_fingerprint,
        request.result,
        StatusLabel.NOT_NEEDED,
        {
            "issue": request.issue_number,
            "result": request.result,
            "body": outcome.render(),
            "head_sha": None,
        },
        CompleteStage.RESERVED,
        prepublication_policy_evidence={
            "decision": "allowed",
            "initial_issue": evidence,
            "context": context,
        },
        downstream_policy_records=not_needed_policies(
            request, None, repository, generation, completion_id, context
        ),
    )


def validate_unclaimed(
    request: CompleteRequest, state: Any, repository: str, forge: Any, state_path: Path
) -> None:
    if request.result != "not-needed" or request.claim_id is not None:
        _reject(
            CompleteFailureReason.CLAIM_NOT_FOUND,
            "Unclaimed completion requires a new not-needed request",
        )
    if str(request.issue_number) in state.active_worktrees:
        _reject(
            CompleteFailureReason.CONCURRENT_COMPLETION,
            "Issue was claimed before completion reservation",
        )
    pending = _pending(request, state, repository)
    if pending is not None:
        _authenticate(request, pending, state_path)
    elif request.completion_id is not None:
        _validate_new_id(request, state)
    _issue_evidence(forge, request.issue_number, pending=pending is not None)


def complete_unclaimed(
    request: CompleteRequest,
    repository: str,
    state_path: Path,
    forge: Any,
    report: Callable[[CompletionJournalRecord], None],
) -> CompleteResult:
    """Serialize reservation, frozen POST, labels, and receipt against claim writers."""
    with completion_journal_lock(state_path, 30):
        state = load_run_state_readonly(state_path)
        replay = find_replay(request, state, repository)
        if replay is not None:
            return replay
        record = _pending(request, state, repository)
        if record is not None:
            report(record)
        validate_unclaimed(request, state, repository, forge, state_path)
        if record is None:
            completion_id = request.completion_id or f"completion-{uuid4().hex}"
            _validate_id(completion_id)
            token = new_owner_token().value
            evidence = _issue_evidence(forge, request.issue_number, pending=False)
            record = _new_record(request, repository, completion_id, token, evidence)
            report(record)
            record = reserve_completion_locked(
                record, owner_token=token, state_path=state_path
            )
        else:
            _authenticate(request, record, state_path)
            report(record)
        context = PublicationContext(request, state_path, None, forge)
        return publish_reserved_completion_locked(context, record)
