"""Durable completion journaling (#1001).

Persists a completion attempt's ``completion_id``, result, posting evidence
(comment id/url, payload) and handoff-readiness onto the same ``ActiveWorktree``
ledger entry that ``orchestune claim`` already owns, under the same
``run_state.json`` file lock. Each public function acquires that lock only for
the duration of its own read-modify-write — never across CI runs or network
calls — so ownership survives long-running work without holding the lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from orchestune.claim.ownership import owner_token_digest
from orchestune.complete.contracts import CompleteFailureReason, CompleteStage
from orchestune.dispatch.state import load_run_state, save_run_state
from orchestune.infra.process_utils import FileLockContentionError, run_state_lock


@dataclass(frozen=True)
class CompletionJournal:
    """Durable identity and evidence for one completion attempt."""

    issue_number: int
    claim_id: str
    completion_id: str
    result: str
    stage: str
    payload: dict[str, Any] | None = None
    comment_id: str | None = None
    comment_url: str | None = None
    handoff_ready: bool = False


class CompletionJournalError(RuntimeError):
    """Raised when a completion-journal transition is rejected or fails to persist."""

    def __init__(self, reason: CompleteFailureReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _new_completion_id() -> str:
    return f"completion-{uuid4().hex}"


def _has_posting_evidence(comment_id: str | None, comment_url: str | None) -> bool:
    return bool(comment_id and comment_id.strip()) and bool(
        comment_url and comment_url.strip()
    )


def _lock_path_for(state_path: Path) -> Path:
    # `save_run_state` always asserts the lock derived from `state_path` (never a
    # caller-supplied override), so the acquired lock must match it exactly.
    return Path(state_path).with_suffix(".lock")


def _to_journal(active: Any) -> CompletionJournal:
    assert active.completion_id is not None
    assert active.completion_result is not None
    assert active.completion_stage is not None
    return CompletionJournal(
        issue_number=active.issue_number,
        claim_id=active.claim_id,
        completion_id=active.completion_id,
        result=active.completion_result,
        stage=active.completion_stage,
        payload=dict(active.completion_payload)
        if active.completion_payload is not None
        else None,
        comment_id=active.completion_comment_id,
        comment_url=active.completion_comment_url,
        handoff_ready=active.completion_handoff_ready,
    )


def _save_or_raise(run_state: Any, state_path: Path) -> None:
    try:
        save_run_state(run_state, state_path)
    except Exception as e:
        raise CompletionJournalError(
            CompleteFailureReason.STATE_SAVE_FAILED,
            f"Failed to persist completion journal: {e}",
        ) from e


def _acquire_run_state_lock(lock_path: Path, timeout_seconds: float) -> Any:
    try:
        cm = run_state_lock(lock_path, timeout=timeout_seconds)
        cm.__enter__()
        return cm
    except (FileLockContentionError, RuntimeError) as e:
        raise CompletionJournalError(
            CompleteFailureReason.STATE_LOCK_FAILED,
            f"Could not acquire run_state lock: {e}",
        ) from e


def _resume_existing_reservation(
    active: Any,
    issue_number: int,
    completion_id: str | None,
    result: str,
    payload: dict[str, Any] | None,
    run_state: Any,
    state_path: Path,
) -> CompletionJournal:
    """Idempotently resume an already-reserved completion, or reject the attempt."""
    if completion_id is not None and completion_id != active.completion_id:
        raise CompletionJournalError(
            CompleteFailureReason.CONCURRENT_COMPLETION,
            f"A different completion ({active.completion_id!r}) is "
            f"already reserved for issue #{issue_number}",
        )
    if active.completion_result != result:
        raise CompletionJournalError(
            CompleteFailureReason.INVALID_STAGE_TRANSITION,
            f"Cannot change reserved completion result from "
            f"{active.completion_result!r} to {result!r}",
        )
    # A completion already handed off to GC is terminal: its posted evidence
    # (comment id/url) refers to a specific payload, so a retried reservation
    # must not silently rewrite it out from under that evidence.
    if payload is not None and not active.completion_handoff_ready:
        active.completion_payload = dict(payload)
        _save_or_raise(run_state, state_path)
    return _to_journal(active)


def reserve_completion(
    *,
    issue_number: int,
    claim_id: str,
    owner_token: str,
    result: str,
    completion_id: str | None = None,
    payload: dict[str, Any] | None = None,
    state_path: Path,
    timeout_seconds: float = 0.0,
) -> CompletionJournal:
    """Reserve (or idempotently resume) a completion under the claim's state lock.

    Rejects: the issue's claim no longer matching ``claim_id`` (re-claim),
    an owner-token mismatch, a different ``completion_id`` already reserved
    (concurrent completion), and reserving a different ``result`` than what
    is already reserved for this claim (double-complete / overwrite).
    """
    cm = _acquire_run_state_lock(_lock_path_for(state_path), timeout_seconds)
    try:
        run_state = load_run_state(state_path)
        active = run_state.active_worktrees.get(str(issue_number))
        if active is None or active.claim_id != claim_id:
            raise CompletionJournalError(
                CompleteFailureReason.CLAIM_NOT_FOUND,
                f"No active claim {claim_id!r} found for issue #{issue_number}",
            )
        if owner_token_digest(owner_token) != active.owner_token_digest:
            raise CompletionJournalError(
                CompleteFailureReason.OWNER_TOKEN_MISMATCH,
                "Owner token does not match the active claim",
            )

        if active.completion_id is not None:
            return _resume_existing_reservation(
                active,
                issue_number,
                completion_id,
                result,
                payload,
                run_state,
                state_path,
            )

        active.completion_id = completion_id or _new_completion_id()
        active.completion_result = result
        active.completion_stage = CompleteStage.JOURNALING.value
        if payload is not None:
            active.completion_payload = dict(payload)
        _save_or_raise(run_state, state_path)
        return _to_journal(active)
    finally:
        cm.__exit__(None, None, None)


def mark_handoff_ready(
    journal: CompletionJournal,
    *,
    comment_id: str | None = None,
    comment_url: str | None = None,
    payload: dict[str, Any] | None = None,
    state_path: Path,
    timeout_seconds: float = 0.0,
) -> CompletionJournal:
    """Persist posting evidence and mark the completion ready for GC handoff.

    Rejects a stale resume: the claim must still be ``journal.claim_id`` and the
    persisted completion must still match ``journal.completion_id``/``result`` —
    otherwise something changed underneath this attempt (a reclaim, or a
    different completion winning the reservation) since it was reserved.
    Rejects marking handoff-ready unless a durable Issue comment id and url are
    on record (freshly supplied here, or already persisted from a prior call) —
    otherwise GC could treat an unposted outcome as safely handed off.
    """
    cm = _acquire_run_state_lock(_lock_path_for(state_path), timeout_seconds)
    try:
        run_state = load_run_state(state_path)
        active = run_state.active_worktrees.get(str(journal.issue_number))
        if active is None or active.claim_id != journal.claim_id:
            raise CompletionJournalError(
                CompleteFailureReason.CLAIM_NOT_FOUND,
                f"No active claim {journal.claim_id!r} found for issue "
                f"#{journal.issue_number}",
            )
        if (
            active.completion_id != journal.completion_id
            or active.completion_result != journal.result
        ):
            raise CompletionJournalError(
                CompleteFailureReason.INVALID_STAGE_TRANSITION,
                "Completion reservation changed since it was reserved "
                "(stale resume)",
            )

        if comment_id is not None:
            active.completion_comment_id = comment_id
        if comment_url is not None:
            active.completion_comment_url = comment_url
        if payload is not None:
            active.completion_payload = dict(payload)

        if not _has_posting_evidence(
            active.completion_comment_id, active.completion_comment_url
        ):
            raise CompletionJournalError(
                CompleteFailureReason.EVIDENCE_MISSING,
                "Cannot mark handoff ready without a durably recorded Issue "
                "comment id and url",
            )

        active.completion_handoff_ready = True
        active.completion_stage = CompleteStage.HANDED_OFF_TO_GC.value
        _save_or_raise(run_state, state_path)
        return _to_journal(active)
    finally:
        cm.__exit__(None, None, None)


__all__ = [
    "CompletionJournal",
    "CompletionJournalError",
    "mark_handoff_ready",
    "reserve_completion",
]
