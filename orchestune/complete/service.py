"""Completion workflow composing preflight, CI evidence, journal, and posting."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orchestune.claim.workspace import resolve_claim_workspace
from orchestune.complete.ci_evidence import CiEvidenceError, run_local_ci_if_needed
from orchestune.complete.contracts import (
    CompleteFailure,
    CompleteFailureReason,
    CompleteRequest,
    CompleteResult,
    CompleteStage,
)
from orchestune.complete.journal import (
    CompletionJournal,
    CompletionJournalError,
    mark_handoff_ready,
    reserve_completion,
)
from orchestune.complete.posting import (
    OutcomePostingError,
    PostingRequest,
    PostingResult,
    post_issue_outcome,
)
from orchestune.complete.preflight import evaluate_complete_preflight
from orchestune.dispatch.state import load_run_state
from orchestune.forge import GitHubForge
from orchestune.infra.git_cli import run_git


@dataclass(frozen=True)
class _CompletionContext:
    request: CompleteRequest
    state_path: Path
    worktree: Path
    forge: Any
    active: Any | None


def _failure(
    request: CompleteRequest,
    stage: CompleteStage,
    reason: CompleteFailureReason,
    message: str,
) -> CompleteResult:
    return CompleteResult.failure_result(
        request.issue_number,
        request.result,
        stage,
        CompleteFailure(reason, message, issue_number=request.issue_number),
        claim_id=request.claim_id,
        owner_kind=request.owner_kind,
    )


def _head_sha(worktree: Path) -> str | None:
    result = run_git(["rev-parse", "HEAD"], cwd=worktree, check=False)
    return result.stdout.strip() or None if result.returncode == 0 else None


def _prepare(
    request: CompleteRequest, forge: Any | None
) -> tuple[_CompletionContext | None, CompleteResult | None]:
    try:
        request.validate()
    except ValueError as exc:
        return None, _failure(
            request,
            CompleteStage.INITIALIZING,
            CompleteFailureReason.INVALID_REQUEST,
            str(exc),
        )

    workspace = resolve_claim_workspace(
        explicit_state_path=request.state_path,
        explicit_worktree_root=request.worktree_root,
    )
    state_path = Path(request.state_path or workspace.run_state_path)
    state = load_run_state(state_path)
    active = state.active_worktrees.get(str(request.issue_number))
    context = _CompletionContext(
        request=request,
        state_path=state_path,
        worktree=Path(request.worktree_root or Path.cwd()).resolve(),
        forge=forge or GitHubForge(),
        active=active,
    )
    failure = _preflight_failure(context, state)
    return (None, failure) if failure is not None else (context, None)


def _preflight_failure(
    context: _CompletionContext, state: Any
) -> CompleteResult | None:
    preflight = evaluate_complete_preflight(
        context.request,
        worktree_path=context.worktree,
        forge=context.forge,
        run_state=state,
        expected_base_ref=getattr(
            state.active_worktrees.get(str(context.request.issue_number)),
            "base_ref",
            None,
        ),
    )
    if preflight.accepted:
        return None
    return _failure(
        context.request,
        CompleteStage.PREFLIGHT_VALIDATING,
        preflight.failure_reason or CompleteFailureReason.INVALID_REQUEST,
        preflight.reason or "Completion preflight was rejected",
    )


def _preview(context: _CompletionContext) -> CompleteResult:
    record = context.request.to_outcome_record()
    return CompleteResult.success_result(
        context.request.issue_number,
        context.request.result,
        claim_id=context.request.claim_id,
        owner_kind=context.request.owner_kind,
        pr=record.pr,
        outcome_record=record,
    )


def _run_ci(context: _CompletionContext) -> CompleteResult | None:
    if context.request.result != "done":
        return None
    try:
        run_local_ci_if_needed(context.request)
    except CiEvidenceError as exc:
        return _failure(
            context.request,
            CompleteStage.EVIDENCE_VERIFYING,
            CompleteFailureReason.EVIDENCE_MISSING,
            str(exc),
        )
    return None


def _reserve(
    context: _CompletionContext,
) -> tuple[CompletionJournal | None, CompleteResult | None]:
    request = context.request
    if context.active is None or not request.claim_id or not request.owner_token:
        return None, _failure(
            request,
            CompleteStage.JOURNALING,
            CompleteFailureReason.CLAIM_NOT_FOUND,
            "A claimed task and owner token are required for completion",
        )
    try:
        journal = reserve_completion(
            issue_number=request.issue_number,
            claim_id=request.claim_id,
            owner_token=request.owner_token,
            result=request.result,
            payload={"outcome": request.to_outcome_record().render()},
            state_path=context.state_path,
        )
    except CompletionJournalError as exc:
        return None, _failure(request, CompleteStage.JOURNALING, exc.reason, str(exc))
    return journal, None


def _record(context: _CompletionContext, journal: CompletionJournal) -> Any:
    return replace(
        context.request.to_outcome_record(),
        claim_id=journal.claim_id,
        head_sha=_head_sha(context.worktree),
        completion_id=journal.completion_id,
    )


def _forge_runner(forge: Any) -> Any:
    """Adapt the Forge command runner without widening the Forge public contract."""
    return object.__getattribute__(forge, "_run")


def _post(
    context: _CompletionContext, record: Any
) -> tuple[PostingResult | None, CompleteResult | None]:
    try:
        posted = post_issue_outcome(
            PostingRequest(context.request.issue_number, record),
            runner=_forge_runner(context.forge),
        )
    except OutcomePostingError as exc:
        return None, _failure(
            context.request,
            CompleteStage.POSTING,
            CompleteFailureReason.FORGE_POST_FAILED,
            str(exc),
        )
    return posted, None


def _recheck(context: _CompletionContext) -> CompleteResult | None:
    """Revalidate after network I/O and before journal handoff lock reacquisition."""
    return _preflight_failure(context, load_run_state(context.state_path))


def _handoff(
    context: _CompletionContext,
    journal: CompletionJournal,
    record: Any,
    posted: PostingResult,
) -> CompleteResult:
    try:
        mark_handoff_ready(
            journal,
            comment_id=posted.comment_id,
            comment_url=posted.comment_url,
            payload={"outcome": record.render()},
            state_path=context.state_path,
        )
    except CompletionJournalError as exc:
        return _failure(context.request, CompleteStage.POSTING, exc.reason, str(exc))
    return CompleteResult.success_result(
        context.request.issue_number,
        context.request.result,
        claim_id=journal.claim_id,
        owner_kind=context.request.owner_kind,
        pr=record.pr,
        outcome_record=record,
    )


def complete_task(
    request: CompleteRequest, *, forge: Any | None = None
) -> CompleteResult:
    """Complete one owned task without ever posting outside its Issue."""
    context, failure = _prepare(request, forge)
    if failure is not None:
        return failure
    assert context is not None
    if request.dry_run:
        return _preview(context)
    if (failure := _run_ci(context)) is not None:
        return failure
    journal, failure = _reserve(context)
    if failure is not None:
        return failure
    assert journal is not None
    record = _record(context, journal)
    posted, failure = _post(context, record)
    if failure is not None:
        return failure
    assert posted is not None
    if (failure := _recheck(context)) is not None:
        return failure
    return _handoff(context, journal, record, posted)
