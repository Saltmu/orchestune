"""Label-confirmed completion with fixed payloads and immutable replay receipts."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from orchestune.claim.local_identity import validate_local_claim
from orchestune.claim.ownership import owner_token_digest
from orchestune.claim.workspace import resolve_claim_workspace
from orchestune.complete.ci_evidence import (
    CiEvidenceError,
    run_local_ci_if_needed,
    validate_ci_evidence,
)
from orchestune.complete.contracts import (
    CompleteFailure,
    CompleteFailureReason,
    CompleteRequest,
    CompleteResult,
    CompleteStage,
    DownstreamPolicyRecord,
)
from orchestune.complete.journal import (
    CompletionJournalError,
    CompletionJournalRecord,
    _save_or_raise,
    completion_journal_lock,
    reserve_completion_locked,
)
from orchestune.complete.not_needed_policy import not_needed_policies
from orchestune.complete.policy import evaluate_publication_policy
from orchestune.complete.policy_actions import escalate_token_limit_locked
from orchestune.complete.posting import (
    OutcomePostingError,
)
from orchestune.complete.preflight import _fetch_pr, evaluate_complete_preflight
from orchestune.complete.publication import (
    PublicationContext,
    publish_reserved_completion_locked,
)
from orchestune.complete.replay import find_replay
from orchestune.complete.unclaimed import complete_unclaimed, validate_unclaimed
from orchestune.forge import GitHubForge
from orchestune.infra.git_cli import run_git
from orchestune.labels import StatusLabel
from orchestune.ledger.active_codec import encode_active_worktree
from orchestune.ledger.run_state import load_run_state_readonly


def _failure(
    request: CompleteRequest,
    stage: CompleteStage,
    reason: CompleteFailureReason,
    message: str,
    completion_id: str | None = None,
) -> CompleteResult:
    return CompleteResult.failure_result(
        request.issue_number,
        request.result,
        stage,
        CompleteFailure(reason, message, issue_number=request.issue_number),
        claim_id=request.claim_id,
        owner_kind=request.owner_kind,
        completion_id=completion_id or request.completion_id,
    )


def _head_sha(worktree: Path) -> str | None:
    result = run_git(["rev-parse", "HEAD"], cwd=worktree, check=False)
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def _validate_claim_context(
    active: Any | None,
    repository: str,
    worktree: Path,
    state_path: Path,
    expected_claim_id: str | None,
) -> None:
    if active is None:
        return
    if active.claim.repository_id != repository:
        raise CompletionJournalError(
            CompleteFailureReason.REPOSITORY_IDENTITY_MISMATCH,
            "Claim belongs to another repository",
        )
    claimed_path = Path(active.core.worktree_path)
    if not claimed_path.is_absolute():
        claimed_path = state_path.parent / claimed_path
    if claimed_path.resolve() != worktree.resolve():
        raise CompletionJournalError(
            CompleteFailureReason.INVALID_REQUEST,
            "Completion must run from the claimed worktree",
        )
    try:
        validate_local_claim(
            active, expected_claim_id, cwd=worktree, state_path=state_path
        )
    except ValueError as error:
        raise CompletionJournalError(
            CompleteFailureReason.GENERATION_MISMATCH, str(error)
        ) from error


def _check(request: CompleteRequest, state: Any, worktree: Path, forge: Any) -> None:
    active = state.active_worktrees.get(str(request.issue_number))
    if active is not None and request.claim_id != active.claim.claim_id:
        raise CompletionJournalError(
            CompleteFailureReason.GENERATION_MISMATCH, "Claim generation differs"
        )
    preflight = evaluate_complete_preflight(
        request,
        worktree_path=worktree,
        forge=forge,
        run_state=state,
        expected_base_ref=(active.claim.base_ref if active else None),
    )
    if not preflight.accepted:
        raise CompletionJournalError(
            preflight.failure_reason or CompleteFailureReason.INVALID_REQUEST,
            preflight.reason or "Preflight rejected",
        )


def _pending(
    request: CompleteRequest, state: Any, repository: str, generation: str
) -> CompletionJournalRecord | None:
    records = [
        raw
        for raw in state.completion_journal.values()
        if raw.get("repository_id") == repository
        and raw.get("issue_number") == request.issue_number
        and raw.get("generation_id") == generation
    ]
    if not records:
        return None
    if len(records) != 1:
        raise CompletionJournalError(
            CompleteFailureReason.CONCURRENT_COMPLETION, "Multiple pending completions"
        )
    record = CompletionJournalRecord.from_dict(records[0])
    if (
        request.completion_id is not None
        and request.completion_id != record.completion_id
    ):
        raise CompletionJournalError(
            CompleteFailureReason.CONCURRENT_COMPLETION,
            "A different completion is reserved",
        )
    if record.request_fingerprint != request.request_fingerprint:
        raise CompletionJournalError(
            CompleteFailureReason.REQUEST_FINGERPRINT_MISMATCH,
            "Reserved request payload differs",
        )
    return record


def _downstream_policies(
    request: CompleteRequest,
    active: Any,
    repository: str,
    completion_id: str,
    policy: dict[str, Any],
) -> tuple[DownstreamPolicyRecord, ...]:
    return not_needed_policies(
        request,
        active,
        repository,
        active.claim.claim_id,
        completion_id,
        policy["context"],
    )


def _ensure_validated_head(
    request: CompleteRequest, head_sha: str | None, policy: dict[str, Any]
) -> None:
    if request.result != "done":
        return
    ci = policy.get("validation", {}).get("ci") or {}
    if head_sha is None or ci.get("head_sha") != head_sha:
        raise CompletionJournalError(
            CompleteFailureReason.REQUEST_FINGERPRINT_MISMATCH,
            "HEAD changed after CI validation",
        )


def _initial_issue_evidence(forge: Any, issue: int) -> dict[str, Any]:
    try:
        return {
            "labels": list(forge.get_issue_labels(issue)),
            "state": forge.get_issue_state(issue),
        }
    except Exception as error:
        raise CompletionJournalError(
            CompleteFailureReason.EVIDENCE_MISSING,
            "Initial Issue evidence is unavailable",
        ) from error


def _new_record(
    request: CompleteRequest,
    active: Any,
    repository: str,
    worktree: Path,
    forge: Any,
    policy: dict[str, Any],
) -> CompletionJournalRecord:
    completion_id = request.completion_id or f"completion-{uuid4().hex}"
    outcome = replace(
        request.to_outcome_record(),
        claim_id=active.claim.claim_id,
        completion_id=completion_id,
        head_sha=_head_sha(worktree),
    )
    _ensure_validated_head(request, outcome.head_sha, policy)
    policy = {
        **policy,
        "initial_issue": _initial_issue_evidence(forge, request.issue_number),
        "context": {"active": encode_active_worktree(active)},
    }
    target = {
        "done": StatusLabel.DONE,
        "blocked": StatusLabel.BLOCKED,
        "not-needed": StatusLabel.NOT_NEEDED,
    }[request.result]
    policies = _downstream_policies(request, active, repository, completion_id, policy)
    return CompletionJournalRecord(
        repository,
        request.issue_number,
        active.claim.claim_id,
        completion_id,
        active.claim.owner_token_digest
        or owner_token_digest(active.claim.claim_id or ""),
        request.request_fingerprint,
        request.result,
        target,
        {
            "issue": request.issue_number,
            "result": request.result,
            "body": outcome.render(),
            "head_sha": outcome.head_sha,
        },
        CompleteStage.RESERVED,
        prepublication_policy_evidence=policy,
        downstream_policy_records=policies,
    )


def _reject_policy(
    context: PublicationContext, record: CompletionJournalRecord
) -> None:
    policy = record.prepublication_policy_evidence or {}
    if policy.get("decision") == "allowed":
        return
    if policy.get("decision") == "exceeded":
        escalation = escalate_token_limit_locked(
            context.forge, record.issue_number, record.completion_id, policy
        )
        record = replace(
            record, prepublication_policy_evidence={**policy, "escalation": escalation}
        )
        state = load_run_state_readonly(context.state_path)
        state.completion_journal[record.journal_key] = record.to_dict()
        _save_or_raise(state, context.state_path)
    raise CompletionJournalError(
        CompleteFailureReason.PUBLICATION_POLICY_FAILED,
        f"Done publication held: token usage {policy.get('decision', 'unknown')}",
    )


def _validation_policy(
    request: CompleteRequest,
    forge: Any,
    policy: dict[str, Any],
    ci: dict[str, Any] | None,
) -> dict[str, Any]:
    if request.result != "done":
        return policy
    pr_number = request.to_outcome_record().pr
    assert pr_number is not None
    pr, supported, error = _fetch_pr(forge, pr_number)
    if not supported or pr is None or error:
        raise CompletionJournalError(
            CompleteFailureReason.EVIDENCE_MISSING, "PR evidence is unavailable"
        )
    return {
        **policy,
        "validation": {
            "ci": ci,
            "pr": {
                name: getattr(pr, name, None)
                for name in (
                    "number",
                    "head_ref",
                    "base_ref",
                    "changed_files",
                    "head_sha",
                    "is_ci_passing",
                )
            },
        },
    }


@dataclass
class _Progress:
    stage: CompleteStage = CompleteStage.INITIALIZING
    completion_id: str | None = None
    state_path: Path | None = None
    on_progress: Callable[[str, CompleteStage], None] | None = None

    def report(self, record: CompletionJournalRecord) -> None:
        changed = (self.completion_id, self.stage) != (
            record.completion_id,
            record.stage,
        )
        self.completion_id, self.stage = record.completion_id, record.stage
        if changed and self.on_progress is not None:
            self.on_progress(record.completion_id, record.stage)


def _preview(request: CompleteRequest) -> CompleteResult:
    return CompleteResult.preview_result(
        request.issue_number,
        request.result,
        claim_id=request.claim_id,
        owner_kind=request.owner_kind,
        pr=request.to_outcome_record().pr,
        outcome_record=request.to_outcome_record(),
        completion_id=request.completion_id,
    )


def _policy_for_request(
    request: CompleteRequest, state: Any, repository: str, worktree: Path, forge: Any
) -> dict[str, Any]:
    active = state.active_worktrees[str(request.issue_number)]
    pending = _pending(request, state, repository, active.claim.claim_id or "")
    ci = None
    if request.result == "done":
        ci = (
            validate_ci_evidence(request)
            if pending is not None
            else run_local_ci_if_needed(request)
        ).to_dict()
    policy = (
        evaluate_publication_policy(active, worktree, forge)
        if request.result == "done"
        else {"decision": "allowed"}
    )
    return cast(
        dict[str, Any],
        json.loads(json.dumps(_validation_policy(request, forge, policy, ci))),
    )


def _select_record(
    context: PublicationContext,
    state: Any,
    repository: str,
    policy: dict[str, Any],
    progress: _Progress,
) -> CompletionJournalRecord:
    request, active = context.request, context.active
    assert active is not None
    assert context.worktree is not None
    _validate_claim_context(
        active, repository, context.worktree, context.state_path, request.claim_id
    )
    record = _pending(request, state, repository, active.claim.claim_id or "")
    if record is None:
        assert context.worktree is not None
        record = _new_record(
            request, active, repository, context.worktree, context.forge, policy
        )
        progress.report(record)
        record = reserve_completion_locked(
            record, owner_token=request.owner_token or "", state_path=context.state_path
        )
    elif context.worktree is not None and record.outcome_payload.get(
        "head_sha"
    ) != _head_sha(context.worktree):
        raise CompletionJournalError(
            CompleteFailureReason.REQUEST_FINGERPRINT_MISMATCH,
            "HEAD changed since completion was reserved",
        )
    previous = record.prepublication_policy_evidence or {}
    _ensure_validated_head(request, record.outcome_payload.get("head_sha"), policy)
    if request.result == "done" and not _same_validation(previous, policy):
        raise CompletionJournalError(
            CompleteFailureReason.REQUEST_FINGERPRINT_MISMATCH,
            "CI or PR evidence changed since reservation",
        )
    if previous.get("decision") == "unknown":
        record = replace(record, prepublication_policy_evidence={**previous, **policy})
        state = load_run_state_readonly(context.state_path)
        state.completion_journal[record.journal_key] = record.to_dict()
        _save_or_raise(state, context.state_path)
    progress.report(record)
    return record


def _same_validation(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    old = json.loads(json.dumps(previous.get("validation")))
    new = json.loads(json.dumps(current.get("validation")))
    if isinstance(old, dict) and isinstance(new, dict):
        old_pr, new_pr = old.get("pr", {}), new.get("pr", {})
        old_pr.pop("state", None)
        new_pr.pop("state", None)
        # Legacy journals predate head_sha; payload HEAD and CI bind that head.
        if "head_sha" not in old_pr:
            new_pr.pop("head_sha", None)
    return bool(old == new)


def _publish_request(
    request: CompleteRequest,
    repository: str,
    state_path: Path,
    worktree: Path,
    forge: Any,
    policy: dict[str, Any],
    progress: _Progress,
) -> CompleteResult:
    with completion_journal_lock(state_path, 30):
        state = load_run_state_readonly(state_path)
        replay = find_replay(request, state, repository)
        if replay is not None:
            return replay
        active = state.active_worktrees.get(str(request.issue_number))
        if active is None:
            raise CompletionJournalError(
                CompleteFailureReason.CLAIM_NOT_FOUND,
                "Claim disappeared before publication",
            )
        _validate_claim_context(
            active,
            repository,
            worktree,
            state_path,
            request.claim_id,
        )
        _check(request, state, worktree, forge)
        context = PublicationContext(request, state_path, worktree, forge, active)
        record = _select_record(context, state, repository, policy, progress)
        _reject_policy(context, record)
        return publish_reserved_completion_locked(context, record)


def _request_worktree(request: CompleteRequest, workspace: Any) -> Path:
    return Path(
        getattr(workspace, "repository_root", request.worktree_root or Path.cwd())
    ).resolve()


def _complete(
    request: CompleteRequest, forge: Any | None, progress: _Progress
) -> CompleteResult:
    request.validate()
    workspace = resolve_claim_workspace(
        cwd=request.worktree_root,
        explicit_state_path=request.state_path,
        explicit_worktree_root=request.worktree_root,
    )
    state_path = Path(request.state_path or workspace.run_state_path)
    progress.state_path = state_path
    worktree = _request_worktree(request, workspace)
    state = load_run_state_readonly(state_path)
    replay = find_replay(request, state, workspace.repository_identity)
    if replay is not None:
        return replay
    forge = forge or GitHubForge(timeout_seconds=30)
    progress.stage = CompleteStage.PREFLIGHT_VALIDATING
    if str(request.issue_number) not in state.active_worktrees:
        if request.dry_run:
            validate_unclaimed(
                request, state, workspace.repository_identity, forge, state_path
            )
            return _preview(request)
        return complete_unclaimed(
            request, workspace.repository_identity, state_path, forge, progress.report
        )
    _validate_claim_context(
        state.active_worktrees.get(str(request.issue_number)),
        workspace.repository_identity,
        worktree,
        state_path,
        request.claim_id,
    )
    _check(request, state, worktree, forge)
    if request.dry_run:
        return _preview(request)
    progress.stage = CompleteStage.EVIDENCE_VERIFYING
    policy = _policy_for_request(
        request, state, workspace.repository_identity, worktree, forge
    )
    return _publish_request(
        request,
        workspace.repository_identity,
        state_path,
        worktree,
        forge,
        policy,
        progress,
    )


def _failure_stage(progress: _Progress) -> CompleteStage:
    if progress.state_path is not None and progress.completion_id is not None:
        try:
            state = load_run_state_readonly(progress.state_path)
            for raw in state.completion_journal.values():
                if raw.get("completion_id") == progress.completion_id:
                    stage = CompleteStage(raw["stage"])
                    return (
                        CompleteStage.LABEL_CONFIRMED
                        if stage is CompleteStage.HANDED_OFF
                        else stage
                    )
        except (ValueError, OSError):
            pass
    return progress.stage


def complete_task(
    request: CompleteRequest,
    *,
    forge: Any | None = None,
    on_progress: Callable[[str, CompleteStage], None] | None = None,
) -> CompleteResult:
    """Replay first, validate CI outside the lock, then publish one frozen transaction."""
    progress = _Progress(completion_id=request.completion_id, on_progress=on_progress)
    try:
        return _complete(request, forge, progress)
    except CompletionJournalError as error:
        reason, message = error.reason, str(error)
    except CiEvidenceError as error:
        message = str(error)
        reason = CompleteFailureReason.EVIDENCE_MISSING
    except OutcomePostingError as error:
        message = str(error)
        reason = CompleteFailureReason.FORGE_POST_FAILED
    except Exception as error:
        message = str(error)
        reason = CompleteFailureReason.INVALID_COMPLETION_STATE
    return _failure(
        request, _failure_stage(progress), reason, message, progress.completion_id
    )
