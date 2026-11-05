"""Unified claim orchestration service integrating validation, reservation, worktree, and state."""

from __future__ import annotations

import dataclasses
import subprocess
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Any

from orchestune.branch_naming import build_task_branch_name
from orchestune.claim.admission import (
    _check_resume_identity,
    _claim_failure,
    _unfinished_completion_failure,
    _validate_preflight_and_conflict,
    _with_warnings,
    _worktree_reservation_failure,
)
from orchestune.claim.adopt_pr import (
    ClaimEffects,
    consume_adoption_receipt,
    inspect_adoption,
    revalidate_adoption,
    revalidate_if_adopted,
    verify_adopted_head,
)
from orchestune.claim.base_branch import ensure_claim_base_ref
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
from orchestune.claim.ownership import (
    OwnerToken,
    new_owner_token,
    with_claim,
)
from orchestune.claim.preflight import (
    PreflightDecision,
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
    _validate_issue_for_resume,
)
from orchestune.claim.workspace import (
    ClaimWorkspace,
    resolve_claim_workspace,
)
from orchestune.claim.worktree_reclaim import (
    begin_worktree_reclaim,
    record_takeover_stage,
    takeover_rejection_message,
)
from orchestune.forge import Forge, ForgeError, GitHubForge
from orchestune.infra.git_cli import run_git
from orchestune.infra.process_utils import (
    FileLockContentionError,
    is_process_alive,
    run_state_lock,
)
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
from orchestune.worktree_ops.preparation import (
    WorktreePreparation,
    prepare_task_worktree,
    resolve_worktree_path,
)

_ADOPTED_HEAD_ERRORS = (
    ForgeError,
    OSError,
    subprocess.CalledProcessError,
    ValueError,
)


def _resolve_owner_token(request: ClaimRequest) -> tuple[ClaimRequest, str]:
    """Ensure the claim request carries a valid owner token, generating one if needed."""
    if request.owner_token and request.owner_token.strip():
        return request, request.owner_token
    token = new_owner_token().value
    return dataclasses.replace(request, owner_token=token), token


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
    except OSError as e:
        return ClaimFailure(
            reason=ClaimFailureReason.GIT_FETCH_FAILED,
            message=f"git fetch origin raised exception: {e}",
        )
    return None


def _rollback_reservation_on_prep_failure(
    workspace: ClaimWorkspace,
    reservation: ActiveWorktree,
    canonical_branch: str,
    run_state: RunState,
    raw_token: str,
    reason: ClaimFailureReason,
    message: str,
) -> ClaimOutcome:
    run_state.active_worktrees.pop(str(reservation.core.issue_number), None)
    try:
        save_run_state(run_state, workspace.run_state_path)
    except (OSError, TypeError, ValueError):
        # test_claim_service_edge_cases.py::test_rollback_save_failure_preserves_primary_failure
        pass
    return ClaimOutcome(
        success=False,
        issue_number=reservation.core.issue_number,
        claim_id=None,
        branch=canonical_branch,
        stage=ClaimStage.VALIDATING,
        failure=ClaimFailure(reason=reason, message=message),
        owner_token=raw_token,
    )


def _handle_worktree_prep_exception(
    workspace: ClaimWorkspace,
    reservation: ActiveWorktree,
    canonical_branch: str,
    run_state: RunState,
    raw_token: str,
    reclaim_takeover: Any,
    is_resume: bool,
    reason: ClaimFailureReason,
    message: str,
) -> ClaimOutcome:
    try:
        worktree_path = resolve_worktree_path(workspace.worktree_root, canonical_branch)
        worktree_exists = worktree_path.exists()
    except ValueError:
        worktree_exists = False
    if not is_resume and reclaim_takeover is None and not worktree_exists:
        return _rollback_reservation_on_prep_failure(
            workspace,
            reservation,
            canonical_branch,
            run_state,
            raw_token,
            reason,
            message,
        )
    return _worktree_reservation_failure(
        reason,
        message,
        issue_number=reservation.core.issue_number,
        claim_id=reservation.claim.claim_id,
        canonical_branch=canonical_branch,
        raw_token=raw_token,
    )


def _execute_prepare_worktree(
    workspace: ClaimWorkspace,
    reservation: ActiveWorktree,
    canonical_branch: str,
    base_ref: str,
    run_state: RunState,
    raw_token: str,
    reclaim_takeover: Any,
    is_resume: bool,
) -> tuple[WorktreePreparation | None, ClaimOutcome | None]:
    try:
        prep = prepare_task_worktree(
            canonical_branch,
            workspace.worktree_root,
            base_ref,
            reservation.claim.claim_id or "",
            cwd=workspace.repository_root,
            trust_unclaimed_branch=True,
            takeover=reclaim_takeover,
        )
        return prep, None
    except ValueError as e:
        return None, _handle_worktree_prep_exception(
            workspace,
            reservation,
            canonical_branch,
            run_state,
            raw_token,
            reclaim_takeover,
            is_resume,
            ClaimFailureReason.INVALID_BRANCH_NAME,
            f"invalid branch name: {e}",
        )
    except Exception as e:  # noqa: BLE001 boundary: convert worktree preparation failures to claim outcomes
        return None, _handle_worktree_prep_exception(
            workspace,
            reservation,
            canonical_branch,
            run_state,
            raw_token,
            reclaim_takeover,
            is_resume,
            ClaimFailureReason.WORKTREE_CREATION_FAILED,
            f"prepare_task_worktree raised exception: {e}",
        )


def _prepare_worktree_for_reservation(
    workspace: ClaimWorkspace,
    canonical_branch: str,
    base_ref: str,
    raw_token: str,
    reservation: ActiveWorktree,
    run_state: RunState,
    is_resume: bool = False,
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

    prep, exc_failure = _execute_prepare_worktree(
        workspace,
        reservation,
        canonical_branch,
        base_ref,
        run_state,
        raw_token,
        reclaim.takeover,
        is_resume,
    )
    if exc_failure is not None:
        return None, exc_failure
    assert prep is not None

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
    is_resume: bool = False,
) -> tuple[ActiveWorktree, ClaimOutcome | None]:
    """Create (or take over) the worktree and update reservation to ACTIVE_SAVED."""
    prep, prep_failure = _prepare_worktree_for_reservation(
        workspace,
        canonical_branch,
        base_ref,
        raw_token,
        reservation,
        run_state,
        is_resume=is_resume,
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
    except (OSError, TypeError, ValueError) as e:
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
    except ForgeError as e:
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
        except _ADOPTED_HEAD_ERRORS as error:
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
    effects: ClaimEffects,
    reservation: ActiveWorktree,
    owner_kind: OwnerKind,
    reservation_kind: ReservationKind,
) -> ClaimOutcome:
    """Transition status:in-progress label and mark claim stage as COMPLETED."""
    if reservation.claim.adopted_pr_number is not None:
        alive = effects.alive or is_process_alive
        git = effects.git or run_git
        failure = revalidate_adoption(
            reservation,
            effects.workspace,
            effects.run_state,
            effects.forge,
            effects.observe_external,
            alive,
            git,
        )
        if failure is not None:
            return _make_active_saved_failure(reservation, failure, effects.raw_token)
    fresh_issue, prep_outcome = _validate_and_publish_issue_finalization(
        effects.forge,
        reservation,
        owner_kind,
        reservation_kind,
        effects.raw_token,
        view=effects.view,
        default_base=effects.default_base,
    )
    if prep_outcome is not None or fresh_issue is None:
        assert prep_outcome is not None
        return prep_outcome

    label_failure = _apply_status_label(
        effects.forge, reservation.core.issue_number, fresh_issue.labels
    )
    if label_failure is not None:
        return _make_active_saved_failure(reservation, label_failure, effects.raw_token)

    return _persist_finalized_reservation(
        effects.workspace,
        reservation,
        effects.run_state,
        effects.raw_token,
        owner_kind,
        reservation_kind,
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
    except (OSError, TypeError, ValueError) as e:
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
    except (OSError, TypeError, ValueError) as e:
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


def _preclaim_fetch_and_base_check(
    workspace: ClaimWorkspace,
    issue_number: int,
    base_ref: str,
    forge: Forge,
) -> ClaimFailure | None:
    fetch_failure = _perform_git_fetch(workspace.repository_root, issue_number)
    if fetch_failure is not None:
        return fetch_failure
    return ensure_claim_base_ref(
        base_ref,
        forge=forge,
        repository_root=workspace.repository_root,
        git_runner=run_git,
    )


def _persist_and_prepare_initial(
    workspace: ClaimWorkspace,
    request: ClaimRequest,
    issue: IssueRecord,
    preflight: PreflightDecision,
    canonical_branch: str,
    base_ref: str,
    run_state: RunState,
    raw_token: str,
    adopted_head: str | None,
) -> tuple[ActiveWorktree | None, ClaimOutcome | None]:
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
        return None, save_error
    return _prepare_and_persist_worktree(
        workspace, reservation, canonical_branch, base_ref, run_state, raw_token
    )


def _apply_claim_side_effects(
    effects: ClaimEffects,
    request: ClaimRequest,
    issue: IssueRecord,
    preflight: PreflightDecision,
    canonical_branch: str,
    base_ref: str,
    adopted_head: str | None = None,
) -> ClaimOutcome:
    """Apply Git fetch, reservation persistence, worktree creation, and labeling."""
    preclaim_err = _preclaim_fetch_and_base_check(
        effects.workspace, issue.number, base_ref, effects.forge
    )
    if preclaim_err is not None:
        return _claim_failure(issue.number, preclaim_err)

    reservation, prep_error = _persist_and_prepare_initial(
        effects.workspace,
        request,
        issue,
        preflight,
        canonical_branch,
        base_ref,
        effects.run_state,
        effects.raw_token,
        adopted_head,
    )
    if prep_error is not None or reservation is None:
        assert prep_error is not None
        return prep_error

    return _transition_labels_and_finalize(
        effects,
        reservation,
        request.owner_kind,
        preflight.reservation_kind,
    )


def _finalize_adoption_outcome(
    plan: tuple[
        PreflightDecision,
        IssueRecord,
        tuple[ClaimOverlapWarning, ...],
        str,
        str,
        PrRecord,
    ],
    request: ClaimRequest,
    effects: ClaimEffects,
    apply: bool,
) -> ClaimOutcome:
    preflight, issue, warnings, branch, base, pr = plan
    if apply:
        result = _apply_claim_side_effects(
            effects,
            request,
            issue,
            preflight,
            branch,
            base,
            adopted_head=pr.head_sha,
        )
    else:
        result = _build_dry_run_outcome(
            issue, preflight, branch, base, request, effects.raw_token
        )
    return _with_warnings(result, warnings)


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
    except Exception as error:  # noqa: BLE001 boundary: keep orphan adoption fail-closed
        return _claim_failure(
            request.issue_number,
            ClaimFailure(
                reason=ClaimFailureReason.CLAIM_CONFLICT,
                message=f"Orphan PR adoption rejected: {error}",
            ),
        )
    effects = ClaimEffects(
        workspace=workspace,
        run_state=state,
        forge=forge,
        raw_token=token,
        view=view,
        default_base=default_base,
        observe_external=observe_external,
        alive=is_process_alive,
        git=run_git,
    )
    return _finalize_adoption_outcome(plan, request, effects, apply)


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
    effects = ClaimEffects(
        workspace=workspace,
        run_state=state,
        forge=forge,
        view=view,
        default_base=default_base,
        observe_external=observe_external,
        alive=is_process_alive,
        git=run_git,
    )
    branch, base, pr = inspect_adoption(
        issue,
        preflight,
        request.adopt_pr,
        effects,
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
        effects = ClaimEffects(
            workspace=workspace,
            run_state=run_state,
            forge=forge,
            raw_token=raw_token,
            view=view,
            default_base=default_base,
            alive=is_process_alive,
            git=run_git,
        )
        outcome = _apply_claim_side_effects(
            effects,
            request,
            issue,
            preflight,
            canonical_branch,
            base_ref,
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

    effects = ClaimEffects(
        workspace=workspace,
        run_state=run_state,
        forge=forge,
        raw_token=owner_token,
        view=view,
        default_base=default_base,
        observe_external=observe_external,
        alive=is_process_alive,
        git=run_git,
    )
    return _transition_labels_and_finalize(
        effects,
        active,
        owner_kind,
        reservation_kind,
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
            is_resume=True,
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


__all__ = ["ClaimEffects", "claim_task", "resume_claim"]
