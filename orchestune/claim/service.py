"""Unified claim orchestration service integrating validation, reservation, worktree, and state."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Any

from orchestune.branch_naming import build_task_branch_name
from orchestune.claim.adopt_pr import (
    consume_adoption_receipt,
    inspect_adoption,
    revalidate_adoption,
    revalidate_if_adopted,
    verify_adopted_head,
)
from orchestune.claim.contracts import (
    ClaimFailure,
    ClaimFailureReason,
    ClaimOutcome,
    ClaimOverlapWarning,
    ClaimRequest,
    ClaimStage,
    OwnerKind,
    ReservationKind,
)
from orchestune.claim.issue_metadata import publish_claim_ownership_to_issue
from orchestune.claim.local_identity import validate_local_claim
from orchestune.claim.ownership import (
    ClaimConflict,
    ClaimConflictReason,
    OwnerToken,
    assess_claim_conflicts,
    build_reservation,
    held_claim_next_actions,
    new_owner_token,
    with_claim,
)
from orchestune.claim.preflight import (
    ClaimBaseResolutionView,
    PreflightDecision,
    evaluate_claim_preflight,
)
from orchestune.claim.reservation import (
    _build_dry_run_outcome,
    _build_success_outcome,
    _initialize_reservation,
    _make_active_saved_failure,
    _resume_preview,
    _validate_resume_active,
)
from orchestune.claim.resume_validation import (
    _fetch_and_validate_resume_issue,
    _preflight_labels,
    _validate_issue_for_resume,
)
from orchestune.claim.workspace import (
    ClaimWorkspace,
    check_repository_identity_match,
    resolve_claim_workspace,
)
from orchestune.claim.worktree_reclaim import (
    begin_worktree_reclaim,
    record_takeover_stage,
    takeover_rejection_message,
)
from orchestune.forge import Forge, ForgeGraphQLUnavailableError, GitHubForge
from orchestune.infra.git_cli import run_git
from orchestune.infra.process_utils import (
    FileLockContentionError,
    is_process_alive,
    run_state_lock,
)
from orchestune.issue_parsing import parse_task_from_issue
from orchestune.labels import STATUS_LABEL_PREFIX, StatusLabel
from orchestune.ledger.completion_reservations import completion_mutation_blocked
from orchestune.ledger.run_state import (
    ActiveWorktree,
    RunState,
    load_run_state,
    save_run_state,
)
from orchestune.ledger.status_labels import transition_status_label
from orchestune.models import IssueRecord, PrRecord
from orchestune.task_metadata import TaskMetadata
from orchestune.worktree_ops.preparation import (
    WorktreePreparation,
    prepare_task_worktree,
)


class _DefaultConflictView:
    """Fallback conflict view looking up tasks via forge when no view is provided."""

    def __init__(
        self, forge: Forge, cache: dict[int, TaskMetadata] | None = None
    ) -> None:
        self._forge = forge
        self._cache: dict[int, TaskMetadata] = dict(cache or {})

    def task(self, issue_number: int) -> TaskMetadata | None:
        if issue_number in self._cache:
            return self._cache[issue_number]
        issue = self._forge.get_issue(issue_number)
        if issue is not None:
            parsed = parse_task_from_issue(issue)
            self._cache[issue_number] = parsed
            return parsed
        return None


def _resolve_owner_token(request: ClaimRequest) -> tuple[ClaimRequest, str]:
    """Ensure the claim request carries a valid owner token, generating one if needed."""
    if request.owner_token and request.owner_token.strip():
        return request, request.owner_token
    token = new_owner_token().value
    return dataclasses.replace(request, owner_token=token), token


def _conflict_to_outcome(issue_number: int, conflict: ClaimConflict) -> ClaimOutcome:
    """Format a conflict decision into a structured rejection outcome."""
    reason = ClaimFailureReason.CLAIM_CONFLICT
    next_actions: tuple[str, ...] = ()
    if conflict.reason == ClaimConflictReason.SAME_ISSUE:
        reason = ClaimFailureReason.EXISTING_CLAIM_UNRECOVERED
        next_actions = held_claim_next_actions(conflict.active)
    msg = f"Claim conflict with issue #{conflict.active.core.issue_number}: {conflict.reason.value}"
    failure = ClaimFailure(
        reason=reason,
        message=msg,
        conflicting_issue_number=conflict.active.core.issue_number,
        conflicting_branch=conflict.active.core.branch or None,
        conflicting_path=Path(conflict.active.core.worktree_path)
        if conflict.active.core.worktree_path
        else None,
        next_actions=next_actions,
    )
    return ClaimOutcome(success=False, issue_number=issue_number, failure=failure)


def _evaluate_conflict_step(
    reservation: ActiveWorktree,
    run_state: RunState,
    conflict_view: Any,
    issue_number: int,
) -> tuple[ClaimOutcome | None, tuple[ClaimOverlapWarning, ...]]:
    """Evaluate conflict rules, returning a rejection outcome or non-blocking warnings."""
    try:
        assessment = assess_claim_conflicts(reservation, run_state, conflict_view)
    except ForgeGraphQLUnavailableError:
        # #1334: the CLI reports this as forge_unavailable, not as a conflict.
        raise
    except Exception as e:
        return _claim_failure(
            issue_number,
            ClaimFailure(
                reason=ClaimFailureReason.CLAIM_CONFLICT,
                message=f"Failed to evaluate claim conflicts due to metadata lookup failure: {e}",
            ),
        ), ()
    if assessment.conflict is not None:
        return _conflict_to_outcome(issue_number, assessment.conflict), ()
    return None, assessment.warnings


def _with_warnings(
    outcome: ClaimOutcome, warnings: tuple[ClaimOverlapWarning, ...]
) -> ClaimOutcome:
    return dataclasses.replace(outcome, warnings=warnings) if warnings else outcome


def _claim_failure(issue_number: int, failure: ClaimFailure | None) -> ClaimOutcome:
    return ClaimOutcome(success=False, issue_number=issue_number, failure=failure)


def _validate_preflight_and_conflict(
    request: ClaimRequest,
    run_state: RunState,
    forge: Forge,
    view: Any,
    default_base: str,
    *,
    adoption: bool = False,
) -> tuple[
    PreflightDecision | None,
    IssueRecord | None,
    ClaimOutcome | None,
    tuple[ClaimOverlapWarning, ...],
]:
    """Execute preflight and conflict checks in the current run_state snapshot."""
    issue = forge.get_issue(request.issue_number)
    if issue is None:
        failure = ClaimFailure(
            reason=ClaimFailureReason.ISSUE_NOT_FOUND,
            message=f"Issue #{request.issue_number} was not found.",
        )
        return None, None, _claim_failure(request.issue_number, failure), ()

    preflight_view = view if isinstance(view, ClaimBaseResolutionView) else None
    preflight = evaluate_claim_preflight(
        issue,
        labels=_preflight_labels(issue, adoption),
        view=preflight_view,
        default_base=default_base,
    )
    if not preflight.allowed:
        return (
            None,
            issue,
            _claim_failure(request.issue_number, preflight.failure),
            (),
        )

    task_meta = (
        preflight.task if preflight.task is not None else parse_task_from_issue(issue)
    )
    reservation = build_reservation(request, task_meta)
    conflict_view = (
        view
        if (view is not None and hasattr(view, "task"))
        else _DefaultConflictView(forge, cache={issue.number: task_meta})
    )
    conflict_outcome, warnings = _evaluate_conflict_step(
        reservation, run_state, conflict_view, request.issue_number
    )
    if conflict_outcome is not None:
        return None, issue, conflict_outcome, ()

    return preflight, issue, None, warnings


def _perform_git_fetch(repo_root: Path, issue_number: int) -> ClaimFailure | None:
    """Execute git fetch origin before making local modifications."""
    try:
        res = run_git(["fetch", "origin"], cwd=repo_root, check=False)
        if res.returncode != 0:
            err = res.stderr or res.stdout or "unknown fetch failure"
            return ClaimFailure(
                reason=ClaimFailureReason.GIT_FETCH_FAILED,
                message=f"git fetch origin failed: {err.strip()}",
            )
    except Exception as e:
        return ClaimFailure(
            reason=ClaimFailureReason.GIT_FETCH_FAILED,
            message=f"git fetch origin raised exception: {e}",
        )
    return None


def _worktree_reservation_failure(
    reason: ClaimFailureReason,
    message: str,
    *,
    issue_number: int,
    claim_id: str | None,
    canonical_branch: str,
    raw_token: str,
) -> ClaimOutcome:
    return ClaimOutcome(
        success=False,
        issue_number=issue_number,
        claim_id=claim_id,
        branch=canonical_branch,
        stage=ClaimStage.RESERVED,
        failure=ClaimFailure(reason=reason, message=message),
        owner_token=raw_token,
    )


def _prepare_worktree_for_reservation(
    workspace: ClaimWorkspace,
    canonical_branch: str,
    base_ref: str,
    raw_token: str,
    reservation: ActiveWorktree,
    run_state: RunState,
) -> tuple[WorktreePreparation | None, ClaimOutcome | None]:
    """Execute worktree preparation, returning structured failure on exception or rejection.

    #1381: a retained worktree of a GC-released generation is taken over only
    with verified evidence, persisted as prepared before its marker moves.
    """
    fail = partial(
        _worktree_reservation_failure,
        issue_number=reservation.core.issue_number,
        claim_id=reservation.claim.claim_id,
        canonical_branch=canonical_branch,
        raw_token=raw_token,
    )
    reclaim, save_error = begin_worktree_reclaim(
        run_state, workspace, reservation, save_run_state
    )
    if save_error is not None:
        return None, fail(ClaimFailureReason.STATE_SAVE_FAILED, save_error)
    try:
        prep = prepare_task_worktree(
            canonical_branch,
            workspace.worktree_root,
            base_ref,
            reservation.claim.claim_id or "",
            cwd=workspace.repository_root,
            # #943: この時点で`_validate_preflight_and_conflict`
            # (`evaluate_claim_conflicts`)が同じロック内で`run_state`全体を
            # 走査済みであり、`issue_number`を現在保持しているactiveが無いことを
            # 既に確認している。staleなマーカーより強い根拠が既にあるため、
            # 完了・巻き戻し後の正当な再claimが、ブランチだけが残っている
            # ことを理由に永久拒否されないようにする。
            trust_unclaimed_branch=True,
            takeover=reclaim.takeover,
        )
    except ValueError as e:
        # #943レビュー対応(Codex P2): `validate_ref_name`が送出する不正な
        # branch/subtask_id名は恒久的な入力不備（人手の修正が必要）であり、
        # OSError/git実行エラーのような一時的なインフラ障害とは扱いを分ける
        # 必要がある——launch.py側でこの理由だけを`status:blocked-human-review`
        # へ振り分け、それ以外の`WORKTREE_CREATION_FAILED`は再試行可能な
        # `status:blocked`のままにするため。
        return None, fail(
            ClaimFailureReason.INVALID_BRANCH_NAME, f"invalid branch name: {e}"
        )
    except Exception as e:
        return None, fail(
            ClaimFailureReason.WORKTREE_CREATION_FAILED,
            f"prepare_task_worktree raised exception: {e}",
        )

    if not prep.accepted:
        return None, fail(
            ClaimFailureReason.WORKTREE_CREATION_FAILED,
            takeover_rejection_message(prep.rejection_reason, reclaim),
        )
    return prep, None


def _replace_held_reservation(
    run_state: RunState, previous: ActiveWorktree, updated: ActiveWorktree
) -> None:
    """Exchange a held record without changing its existing ledger key."""
    for key, active in run_state.active_worktrees.items():
        if active is previous:
            run_state.active_worktrees[key] = updated


def _prepare_and_persist_worktree(
    workspace: ClaimWorkspace,
    reservation: ActiveWorktree,
    canonical_branch: str,
    base_ref: str,
    run_state: RunState,
    raw_token: str,
) -> tuple[ActiveWorktree, ClaimOutcome | None]:
    """Create (or take over) the worktree and update reservation to ACTIVE_SAVED."""
    prep, prep_failure = _prepare_worktree_for_reservation(
        workspace,
        canonical_branch,
        base_ref,
        raw_token,
        reservation,
        run_state,
    )
    if prep_failure is not None or prep is None:
        return reservation, prep_failure

    previous = reservation
    reservation = with_claim(
        reservation.with_core(
            dataclasses.replace(reservation.core, worktree_path=str(prep.worktree_path))
        ),
        dataclasses.replace(
            reservation.claim,
            base_sha=prep.base_sha,
            claim_stage=ClaimStage.ACTIVE_SAVED.value,
        ),
    )
    _replace_held_reservation(run_state, previous, reservation)
    record_takeover_stage(run_state, reservation)
    try:
        save_run_state(run_state, workspace.run_state_path)
    except Exception as e:
        return reservation, ClaimOutcome(
            success=False,
            issue_number=reservation.core.issue_number,
            claim_id=reservation.claim.claim_id,
            branch=canonical_branch,
            stage=ClaimStage.RESERVED,
            failure=ClaimFailure(
                reason=ClaimFailureReason.STATE_SAVE_FAILED,
                message=f"Failed to persist run_state after worktree creation: {e}",
            ),
            owner_token=raw_token,
        )
    return reservation, None


def _apply_status_label(
    forge: Forge, issue_number: int, labels: Sequence[str]
) -> ClaimFailure | None:
    """Transition status:in-progress label, returning a ClaimFailure on error."""
    old_labels = [
        lbl
        for lbl in labels
        if lbl.startswith(STATUS_LABEL_PREFIX) and lbl != StatusLabel.IN_PROGRESS
    ]
    try:
        transition_status_label(
            forge,
            issue_number,
            StatusLabel.IN_PROGRESS,
            old_labels,
        )
        return None
    except Exception as e:
        return ClaimFailure(
            reason=ClaimFailureReason.LABEL_UPDATE_FAILED,
            message=f"Failed to transition label: {e}",
        )


def _validate_and_publish_issue_finalization(
    forge: Forge,
    reservation: ActiveWorktree,
    owner_kind: OwnerKind,
    reservation_kind: ReservationKind,
    raw_token: str,
    view: Any = None,
    default_base: str = "origin/main",
) -> tuple[IssueRecord | None, ClaimOutcome | None]:
    """Fetch fresh issue, revalidate status, and publish recovery metadata."""
    fresh_issue = forge.get_issue(reservation.core.issue_number)
    if fresh_issue is None:
        failure = ClaimFailure(
            reason=ClaimFailureReason.ISSUE_NOT_FOUND,
            message=f"Issue #{reservation.core.issue_number} was not found during finalization.",
        )
        return None, _make_active_saved_failure(reservation, failure, raw_token)

    validation_error = _validate_issue_for_resume(
        fresh_issue,
        view=view,
        default_base=default_base,
        adopted=reservation.claim.adopted_pr_number is not None,
    )
    if validation_error is not None:
        return None, _make_active_saved_failure(
            reservation, validation_error, raw_token
        )

    if reservation.claim.adopted_pr_number is not None:
        try:
            verify_adopted_head(reservation, forge, run_git)
        except Exception as error:
            return None, _make_active_saved_failure(
                reservation,
                ClaimFailure(
                    reason=ClaimFailureReason.CLAIM_CONFLICT,
                    message=f"Adopted PR verification failed: {error}",
                ),
                raw_token,
            )

    publish_error = publish_claim_ownership_to_issue(
        forge,
        fresh_issue,
        owner_kind,
        reservation.claim.claim_id or "",
        reservation_kind,
    )
    if publish_error is not None:
        return None, _make_active_saved_failure(reservation, publish_error, raw_token)

    return fresh_issue, None


def _transition_labels_and_finalize(
    workspace: ClaimWorkspace,
    reservation: ActiveWorktree,
    run_state: RunState,
    forge: Forge,
    raw_token: str,
    owner_kind: OwnerKind,
    reservation_kind: ReservationKind,
    view: Any = None,
    default_base: str = "origin/main",
    observe_external: Callable[[ActiveWorktree, ClaimWorkspace], tuple[str, str]]
    | None = None,
) -> ClaimOutcome:
    """Transition status:in-progress label and mark claim stage as COMPLETED."""
    if reservation.claim.adopted_pr_number is not None:
        failure = revalidate_adoption(
            reservation,
            workspace,
            run_state,
            forge,
            observe_external,
            is_process_alive,
            run_git,
        )
        if failure is not None:
            return _make_active_saved_failure(reservation, failure, raw_token)
    fresh_issue, prep_outcome = _validate_and_publish_issue_finalization(
        forge,
        reservation,
        owner_kind,
        reservation_kind,
        raw_token,
        view=view,
        default_base=default_base,
    )
    if prep_outcome is not None or fresh_issue is None:
        assert prep_outcome is not None
        return prep_outcome

    label_failure = _apply_status_label(
        forge, reservation.core.issue_number, fresh_issue.labels
    )
    if label_failure is not None:
        return _make_active_saved_failure(reservation, label_failure, raw_token)

    return _persist_finalized_reservation(
        workspace, reservation, run_state, raw_token, owner_kind, reservation_kind
    )


def _persist_finalized_reservation(
    workspace: ClaimWorkspace,
    reservation: ActiveWorktree,
    run_state: RunState,
    raw_token: str,
    owner_kind: OwnerKind,
    reservation_kind: ReservationKind,
) -> ClaimOutcome:
    previous = reservation
    reservation = with_claim(
        reservation,
        dataclasses.replace(reservation.claim, claim_stage=ClaimStage.COMPLETED.value),
    )
    _replace_held_reservation(run_state, previous, reservation)
    record_takeover_stage(run_state, reservation)
    consumed = consume_adoption_receipt(run_state, reservation)
    try:
        save_run_state(run_state, workspace.run_state_path)
    except Exception as e:
        run_state.recovery_receipts.update(consumed)
        failure = ClaimFailure(
            reason=ClaimFailureReason.STATE_SAVE_FAILED,
            message=f"Failed to persist run_state on completion: {e}",
        )
        return _make_active_saved_failure(reservation, failure, raw_token)
    return _build_success_outcome(reservation, raw_token, owner_kind, reservation_kind)


def _handle_request_resume(
    request: ClaimRequest,
    raw_token: str,
    workspace: ClaimWorkspace,
    run_state: RunState,
    forge: Forge,
    apply: bool = True,
    view: Any = None,
    default_base: str = "origin/main",
    observe_external: Callable[[ActiveWorktree, ClaimWorkspace], tuple[str, str]]
    | None = None,
) -> ClaimOutcome | None:
    """Handle explicit resume via resume_claim_id in ClaimRequest if provided."""
    if not request.resume_claim_id:
        return None
    active, auth_error = _validate_resume_active(
        request.resume_claim_id,
        raw_token,
        run_state,
        fallback_issue_number=request.issue_number,
    )
    if auth_error is not None or active is None:
        assert auth_error is not None
        return auth_error
    identity_error = _check_resume_identity(workspace, active, raw_token)
    if identity_error is not None:
        return identity_error
    if not apply:
        failure = revalidate_if_adopted(
            active,
            workspace,
            run_state,
            forge,
            observe_external,
            is_process_alive,
            run_git,
        )
        if failure is not None:
            return _claim_failure(active.core.issue_number, failure)
        return _resume_preview(active, request, default_base, raw_token)
    return _resume_from_active(
        active,
        workspace,
        run_state,
        forge,
        raw_token,
        view=view,
        default_base=default_base,
        observe_external=observe_external,
    )


def _persist_initial_reservation(
    workspace: ClaimWorkspace,
    request: ClaimRequest,
    issue: IssueRecord,
    subtask_id: str | None,
    base_ref: str,
    run_state: RunState,
    raw_token: str,
    adopted_head: str | None = None,
) -> tuple[ActiveWorktree | None, ClaimOutcome | None]:
    """Initialize and persist reservation in run_state, returning error outcome on failure."""
    reservation = _initialize_reservation(
        request,
        issue,
        subtask_id,
        base_ref,
        workspace.repository_identity,
        adopted_head,
    )
    run_state.active_worktrees[str(issue.number)] = reservation
    try:
        save_run_state(run_state, workspace.run_state_path)
    except Exception as e:
        return None, ClaimOutcome(
            success=False,
            issue_number=issue.number,
            stage=ClaimStage.VALIDATING,
            failure=ClaimFailure(
                reason=ClaimFailureReason.STATE_SAVE_FAILED,
                message=f"Failed to persist initial reservation: {e}",
            ),
            owner_token=raw_token,
        )
    return reservation, None


def _apply_claim_side_effects(
    workspace: ClaimWorkspace,
    request: ClaimRequest,
    issue: IssueRecord,
    preflight: PreflightDecision,
    canonical_branch: str,
    base_ref: str,
    run_state: RunState,
    forge: Forge,
    raw_token: str,
    view: Any = None,
    default_base: str = "origin/main",
    adopted_head: str | None = None,
    observe_external: Callable[[ActiveWorktree, ClaimWorkspace], tuple[str, str]]
    | None = None,
) -> ClaimOutcome:
    """Apply Git fetch, reservation persistence, worktree creation, and labeling."""
    fetch_failure = _perform_git_fetch(workspace.repository_root, issue.number)
    if fetch_failure is not None:
        return _claim_failure(issue.number, fetch_failure)

    reservation, save_error = _persist_initial_reservation(
        workspace,
        request,
        issue,
        preflight.subtask_id,
        base_ref,
        run_state,
        raw_token,
        adopted_head,
    )
    if save_error is not None or reservation is None:
        assert save_error is not None
        return save_error

    reservation, prep_error = _prepare_and_persist_worktree(
        workspace, reservation, canonical_branch, base_ref, run_state, raw_token
    )
    if prep_error is not None:
        return prep_error

    return _transition_labels_and_finalize(
        workspace,
        reservation,
        run_state,
        forge,
        raw_token,
        request.owner_kind,
        preflight.reservation_kind,
        view=view,
        default_base=default_base,
        observe_external=observe_external,
    )


def _unfinished_completion_failure(issue_number: int) -> ClaimOutcome:
    return _claim_failure(
        issue_number,
        ClaimFailure(
            reason=ClaimFailureReason.EXISTING_CLAIM_UNRECOVERED,
            message="Issue has an unfinished or invalid completion reservation; resume complete first.",
        ),
    )


def _preview_adoption(
    request: ClaimRequest,
    token: str,
    workspace: ClaimWorkspace,
    state: RunState,
    forge: Forge,
    view: Any,
    default_base: str,
    apply: bool,
    observe_external: Callable[[ActiveWorktree, ClaimWorkspace], tuple[str, str]]
    | None,
) -> ClaimOutcome:
    """Read-only validation of identity, last escalation, and stopped execution."""
    try:
        plan, error = _prepare_adoption(
            request, workspace, state, forge, view, default_base, observe_external
        )
        if error is not None:
            return error
        assert plan is not None
        preflight, issue, warnings, branch, base, pr = plan
    except Exception as error:
        return _claim_failure(
            request.issue_number,
            ClaimFailure(
                reason=ClaimFailureReason.CLAIM_CONFLICT,
                message=f"Orphan PR adoption rejected: {error}",
            ),
        )
    if apply:
        result = _apply_claim_side_effects(
            workspace,
            request,
            issue,
            preflight,
            branch,
            base,
            state,
            forge,
            token,
            view=view,
            default_base=default_base,
            adopted_head=pr.head_sha,
            observe_external=observe_external,
        )
    else:
        result = _build_dry_run_outcome(issue, preflight, branch, base, request, token)
    return _with_warnings(result, warnings)


def _prepare_adoption(
    request: ClaimRequest,
    workspace: ClaimWorkspace,
    state: RunState,
    forge: Forge,
    view: Any,
    default_base: str,
    observe_external: Callable[[ActiveWorktree, ClaimWorkspace], tuple[str, str]]
    | None,
) -> tuple[
    tuple[
        PreflightDecision,
        IssueRecord,
        tuple[ClaimOverlapWarning, ...],
        str,
        str,
        PrRecord,
    ]
    | None,
    ClaimOutcome | None,
]:
    preflight, issue, error, warnings = _validate_preflight_and_conflict(
        request, state, forge, view, default_base, adoption=True
    )
    if error is not None or preflight is None or issue is None:
        assert error is not None
        return None, error
    branch, base, pr = inspect_adoption(
        issue,
        preflight,
        request.adopt_pr,
        default_base,
        workspace,
        state,
        forge,
        observe_external,
        is_process_alive,
        run_git,
    )
    return (preflight, issue, warnings, branch, base, pr), None


def _execute_claim_in_lock(
    request: ClaimRequest,
    raw_token: str,
    workspace: ClaimWorkspace,
    forge: Forge,
    apply: bool,
    view: Any,
    default_base: str,
    observe_external: Callable[[ActiveWorktree, ClaimWorkspace], tuple[str, str]]
    | None = None,
) -> ClaimOutcome:
    """State-locked claim execution sequencing §5 lifecycle steps."""
    run_state = load_run_state(workspace.run_state_path)
    if completion_mutation_blocked(run_state, request.issue_number):
        return _unfinished_completion_failure(request.issue_number)
    if request.adopt_pr is not None:
        return _preview_adoption(
            request,
            raw_token,
            workspace,
            run_state,
            forge,
            view,
            default_base,
            apply,
            observe_external,
        )
    resume_outcome = _handle_request_resume(
        request,
        raw_token,
        workspace,
        run_state,
        forge,
        apply=apply,
        view=view,
        default_base=default_base,
        observe_external=observe_external,
    )
    if resume_outcome is not None:
        return resume_outcome

    return _claim_new_reservation(
        request, raw_token, workspace, run_state, forge, apply, view, default_base
    )


def _claim_new_reservation(
    request: ClaimRequest,
    raw_token: str,
    workspace: ClaimWorkspace,
    run_state: RunState,
    forge: Forge,
    apply: bool,
    view: Any,
    default_base: str,
) -> ClaimOutcome:
    """Validate a new claim, then preview it or apply its side effects."""
    preflight, issue, error_outcome, warnings = _validate_preflight_and_conflict(
        request, run_state, forge, view, default_base
    )
    if error_outcome is not None or preflight is None or issue is None:
        assert error_outcome is not None
        return error_outcome

    canonical_branch = build_task_branch_name(issue.number, preflight.subtask_id)
    base_ref = preflight.base_ref or default_base
    if not apply:
        outcome = _build_dry_run_outcome(
            issue, preflight, canonical_branch, base_ref, request, raw_token
        )
    else:
        outcome = _apply_claim_side_effects(
            workspace,
            request,
            issue,
            preflight,
            canonical_branch,
            base_ref,
            run_state,
            forge,
            raw_token,
            view=view,
            default_base=default_base,
        )
    return _with_warnings(outcome, warnings)


def claim_task(
    request: ClaimRequest,
    *,
    apply: bool = True,
    forge: Forge | None = None,
    cwd: str | Path | None = None,
    view: Any = None,
    default_base: str = "origin/main",
    observe_external: Callable[[ActiveWorktree, ClaimWorkspace], tuple[str, str]]
    | None = None,
) -> ClaimOutcome:
    """Execute the sequential lifecycle to claim an issue."""
    effective_apply = apply and not request.dry_run
    effective_request, raw_token = _resolve_owner_token(request)
    workspace = resolve_claim_workspace(
        cwd,
        explicit_state_path=request.state_path,
        explicit_worktree_root=request.worktree_root,
    )
    active_forge = forge or GitHubForge()

    timeout = request.timeout_seconds if request.timeout_seconds is not None else 0.0
    try:
        cm = run_state_lock(workspace.lock_path, timeout=timeout)
        cm.__enter__()
    except (FileLockContentionError, RuntimeError) as e:
        return _claim_failure(
            request.issue_number,
            ClaimFailure(
                reason=ClaimFailureReason.STATE_LOCK_FAILED,
                message=f"Could not acquire run_state lock: {e}",
            ),
        )

    try:
        return _execute_claim_in_lock(
            effective_request,
            raw_token,
            workspace,
            active_forge,
            effective_apply,
            view,
            default_base,
            observe_external,
        )
    finally:
        cm.__exit__(None, None, None)


def _check_resume_identity(
    workspace: ClaimWorkspace, active: ActiveWorktree, owner_token: str
) -> ClaimOutcome | None:
    """Validate that the active reservation belongs to the current repository."""
    if not check_repository_identity_match(
        workspace.repository_identity, active.claim.repository_id
    ):
        return ClaimOutcome(
            success=False,
            issue_number=active.core.issue_number,
            failure=ClaimFailure(
                reason=ClaimFailureReason.INVALID_RESUME,
                message=(
                    f"Repository identity mismatch for claim {active.claim.claim_id}: "
                    f"expected '{workspace.repository_identity}', got '{active.claim.repository_id}'"
                ),
            ),
            owner_token=owner_token,
        )
    try:
        validate_local_claim(
            active,
            active.claim.claim_id,
            cwd=workspace.repository_root,
            state_path=workspace.run_state_path,
            allow_primary=True,
            allow_unprepared=True,
        )
    except ValueError as error:
        return _claim_failure(
            active.core.issue_number,
            ClaimFailure(
                reason=ClaimFailureReason.INVALID_RESUME,
                message=str(error),
            ),
        )
    return None


def _resume_from_active(
    active: ActiveWorktree,
    workspace: ClaimWorkspace,
    run_state: RunState,
    forge: Forge,
    owner_token: str,
    view: Any = None,
    default_base: str = "origin/main",
    observe_external: Callable[[ActiveWorktree, ClaimWorkspace], tuple[str, str]]
    | None = None,
) -> ClaimOutcome:
    """Resume an active claim from its current progression stage."""
    identity_error = _check_resume_identity(workspace, active, owner_token)
    if identity_error is not None:
        return identity_error

    failure = revalidate_if_adopted(
        active, workspace, run_state, forge, observe_external, is_process_alive, run_git
    )
    if failure is not None:
        return _claim_failure(active.core.issue_number, failure)

    owner_kind = OwnerKind(active.claim.owner_kind)
    reservation_kind = ReservationKind(active.claim.reservation_kind)

    if active.claim.claim_stage == ClaimStage.COMPLETED.value:
        return _build_success_outcome(active, owner_token, owner_kind, reservation_kind)

    active, prep_error = _prepare_resume(
        active, workspace, run_state, forge, owner_token, view, default_base
    )
    if prep_error is not None:
        return prep_error

    return _transition_labels_and_finalize(
        workspace,
        active,
        run_state,
        forge,
        owner_token,
        owner_kind,
        reservation_kind,
        view=view,
        default_base=default_base,
        observe_external=observe_external,
    )


def _prepare_resume(
    active: ActiveWorktree,
    workspace: ClaimWorkspace,
    state: RunState,
    forge: Forge,
    token: str,
    view: Any,
    default_base: str,
) -> tuple[ActiveWorktree, ClaimOutcome | None]:
    """Recheck the Issue and prepare only a reserved worktree."""
    issue, error = _fetch_and_validate_resume_issue(
        forge, active, token, view=view, default_base=default_base
    )
    if error is not None or issue is None:
        assert error is not None
        return active, error
    if active.claim.claim_stage == ClaimStage.RESERVED.value:
        return _prepare_and_persist_worktree(
            workspace,
            active,
            active.core.branch,
            active.claim.base_ref or default_base,
            state,
            token,
        )
    return active, None


def _normalize_owner_token(owner_token: str | OwnerToken) -> str | None:
    """Extract stripped string representation of owner token, or None if empty."""
    raw = owner_token.value if isinstance(owner_token, OwnerToken) else str(owner_token)
    return raw if raw.strip() else None


def _execute_resume_in_lock(
    claim_id: str,
    raw_token: str,
    workspace: ClaimWorkspace,
    active_forge: Forge,
    view: Any,
    default_base: str,
    observe_external: Callable[[ActiveWorktree, ClaimWorkspace], tuple[str, str]]
    | None = None,
) -> ClaimOutcome:
    """Load run state, authenticate, and resume within active file lock."""
    run_state = load_run_state(workspace.run_state_path)
    active, auth_error = _validate_resume_active(claim_id, raw_token, run_state)
    if auth_error is not None or active is None:
        assert auth_error is not None
        return auth_error
    if completion_mutation_blocked(run_state, active.core.issue_number):
        return _claim_failure(
            active.core.issue_number,
            ClaimFailure(
                reason=ClaimFailureReason.EXISTING_CLAIM_UNRECOVERED,
                message="Issue has an unfinished or invalid completion reservation; resume complete first.",
            ),
        )
    return _resume_from_active(
        active,
        workspace,
        run_state,
        active_forge,
        raw_token,
        view=view,
        default_base=default_base,
        observe_external=observe_external,
    )


def resume_claim(
    claim_id: str,
    owner_token: str | OwnerToken = "",
    *,
    forge: Forge | None = None,
    cwd: str | Path | None = None,
    state_path: str | Path | None = None,
    view: Any = None,
    default_base: str = "origin/main",
    timeout_seconds: float | None = None,
    observe_external: Callable[[ActiveWorktree, ClaimWorkspace], tuple[str, str]]
    | None = None,
) -> ClaimOutcome:
    """Resume the explicitly selected local claim generation."""
    raw_token = _normalize_owner_token(owner_token) or ""

    workspace = resolve_claim_workspace(cwd, explicit_state_path=state_path)
    active_forge = forge or GitHubForge()
    timeout = timeout_seconds if timeout_seconds is not None else 0.0

    try:
        cm = run_state_lock(workspace.lock_path, timeout=timeout)
        cm.__enter__()
    except (FileLockContentionError, RuntimeError) as e:
        return _claim_failure(
            0,
            ClaimFailure(
                reason=ClaimFailureReason.STATE_LOCK_FAILED,
                message=f"Could not acquire run_state lock: {e}",
            ),
        )

    try:
        return _execute_resume_in_lock(
            claim_id,
            raw_token,
            workspace,
            active_forge,
            view,
            default_base,
            observe_external,
        )
    finally:
        cm.__exit__(None, None, None)


__all__ = ["claim_task", "resume_claim"]
