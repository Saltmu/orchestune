"""Completion preflight service evaluating ownership, PR, and Git invariants (#999)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestune.claim.ownership import owner_token_digest
from orchestune.complete.contracts import (
    BlockedPayload,
    CompleteFailureReason,
    CompleteRequest,
    DonePayload,
)
from orchestune.infra.git_cli import WorktreeStatus, inspect_worktree_status
from orchestune.outcome_record import RESULT_BLOCKED, RESULT_DONE, RESULT_NOT_NEEDED


@dataclass(frozen=True)
class CompletePreflight:
    """Diagnostic result of completion preflight evaluation."""

    accepted: bool
    worktree_status: WorktreeStatus = WorktreeStatus.UNKNOWN
    reason: str | None = None
    failure_reason: CompleteFailureReason | None = None
    diagnostics: tuple[str, ...] = ()


def _resolve_worktree_path(
    request: CompleteRequest,
    worktree_path: Path | str | None,
    active: Any | None,
) -> Path | None:
    if worktree_path is not None:
        return Path(worktree_path)
    if active is not None and getattr(active, "worktree_path", None):
        return Path(active.worktree_path)
    if request.worktree_root is not None:
        return Path(request.worktree_root)
    return None


def _validate_ownership(
    request: CompleteRequest,
    run_state: Any | None,
) -> tuple[bool, str | None, CompleteFailureReason | None, Any | None]:
    active_worktrees = getattr(run_state, "active_worktrees", {}) if run_state else {}
    active = active_worktrees.get(str(request.issue_number))
    token = (
        request.owner_token.strip()
        if isinstance(request.owner_token, str) and request.owner_token.strip()
        else None
    )

    if request.result == RESULT_NOT_NEEDED:
        if active is None and token is None:
            return True, None, None, None
        if active is not None:
            if token is None or active.owner_token_digest != owner_token_digest(token):
                return (
                    False,
                    "Owner token mismatch",
                    CompleteFailureReason.OWNER_TOKEN_MISMATCH,
                    None,
                )
            return True, None, None, active
        return False, "Claim not found", CompleteFailureReason.CLAIM_NOT_FOUND, None

    if active is None:
        return (
            False,
            f"Task #{request.issue_number} is not claimed",
            CompleteFailureReason.CLAIM_NOT_FOUND,
            None,
        )

    if token is None:
        return (
            False,
            "Owner token is required",
            CompleteFailureReason.OWNER_TOKEN_MISMATCH,
            None,
        )

    if active.owner_token_digest != owner_token_digest(token):
        return (
            False,
            "Owner token mismatch",
            CompleteFailureReason.OWNER_TOKEN_MISMATCH,
            None,
        )

    return True, None, None, active


def _validate_worktree_status(
    result_type: str,
    status: WorktreeStatus,
) -> tuple[bool, str | None, CompleteFailureReason | None]:
    if result_type == RESULT_DONE:
        if status == WorktreeStatus.DIRTY:
            return (
                False,
                "Worktree has uncommitted changes",
                CompleteFailureReason.DIRTY_WORKTREE,
            )
        if status == WorktreeStatus.UNKNOWN:
            return (
                False,
                "Worktree status is unknown",
                CompleteFailureReason.DIRTY_WORKTREE,
            )
    return True, None, None


def _check_pr_identity_and_branches(
    pr: Any,
    payload_pr: int,
    active: Any | None,
    expected_base_ref: str | None,
) -> tuple[bool, str | None, CompleteFailureReason | None]:
    pr_state = getattr(pr, "state", "").upper()
    if pr_state == "MERGED":
        return (
            False,
            f"Pull request #{payload_pr} is already merged",
            CompleteFailureReason.INVALID_REQUEST,
        )
    if pr_state != "OPEN":
        return (
            False,
            f"Pull request #{payload_pr} is closed without being merged",
            CompleteFailureReason.INVALID_REQUEST,
        )

    active_branch = getattr(active, "branch", None) if active else None
    pr_head_ref = getattr(pr, "head_ref", None)
    if active_branch and pr_head_ref and pr_head_ref != active_branch:
        return (
            False,
            f"Pull request head branch mismatch: expected {active_branch}, got {pr_head_ref}",
            CompleteFailureReason.INVALID_REQUEST,
        )

    expected_base = expected_base_ref or (
        getattr(active, "base_ref", None) if active else None
    )
    pr_base = getattr(pr, "base_ref", None)
    if expected_base and pr_base and pr_base != expected_base:
        return (
            False,
            f"Pull request base branch mismatch: expected {expected_base}, got {pr_base}",
            CompleteFailureReason.INVALID_REQUEST,
        )
    return True, None, None


def _check_pr_head_and_diff(
    pr: Any,
    payload_pr: int,
    current_head_sha: str | None,
) -> tuple[bool, str | None, CompleteFailureReason | None]:
    pr_head_sha = (
        getattr(pr, "head_sha", None)
        or getattr(pr, "head_oid", None)
        or getattr(pr, "head_ref_oid", None)
    )
    if current_head_sha:
        if not pr_head_sha:
            return (
                False,
                f"Pull request #{payload_pr} head SHA cannot be verified against local HEAD",
                CompleteFailureReason.EVIDENCE_MISSING,
            )
        if pr_head_sha != current_head_sha:
            return (
                False,
                f"Local HEAD ({current_head_sha}) has not been pushed to PR ({pr_head_sha})",
                CompleteFailureReason.INVALID_REQUEST,
            )

    changed_files = getattr(pr, "changed_files", None)
    commits_count = getattr(pr, "commits_count", None)
    is_empty = False
    if changed_files is not None and len(changed_files) == 0:
        is_empty = True
    elif commits_count is not None and commits_count == 0:
        is_empty = True

    if is_empty:
        return (
            False,
            f"Pull request #{payload_pr} contains no changes (empty diff)",
            CompleteFailureReason.INVALID_REQUEST,
        )
    return True, None, None


def _fetch_pr(forge: Any, pr_number: int) -> tuple[Any | None, bool]:
    if hasattr(forge, "get_pull_request"):
        return forge.get_pull_request(pr_number), True
    if hasattr(forge, "list_prs"):
        prs = forge.list_prs(state="all")
        for p in prs:
            if getattr(p, "number", None) == pr_number:
                return p, True
        return None, True
    return None, False


def _validate_pull_request(
    payload: DonePayload,
    forge: Any | None,
    active: Any | None,
    expected_base_ref: str | None,
    current_head_sha: str | None,
) -> tuple[bool, str | None, CompleteFailureReason | None]:
    if forge is None:
        return (
            False,
            "Pull request validation requires a forge client",
            CompleteFailureReason.EVIDENCE_MISSING,
        )

    pr, supported = _fetch_pr(forge, payload.pr)
    if not supported:
        return (
            False,
            "Forge client does not support pull request lookup",
            CompleteFailureReason.EVIDENCE_MISSING,
        )
    if pr is None:
        return (
            False,
            f"Pull request #{payload.pr} not found",
            CompleteFailureReason.EVIDENCE_MISSING,
        )

    ok, reason, failure_reason = _check_pr_identity_and_branches(
        pr, payload.pr, active, expected_base_ref
    )
    if not ok:
        return ok, reason, failure_reason

    return _check_pr_head_and_diff(pr, payload.pr, current_head_sha)


def _validate_blocked_payload(
    payload: BlockedPayload,
) -> tuple[bool, str | None, CompleteFailureReason | None]:
    if payload.reason == "base-branch-red" and not payload.base_sha:
        return (
            False,
            "base-branch-red requires base_sha",
            CompleteFailureReason.INVALID_REQUEST,
        )
    if payload.reason == "review-timeout" and payload.attempt is None:
        return (
            False,
            "review-timeout requires attempt",
            CompleteFailureReason.INVALID_REQUEST,
        )
    return True, None, None


def _validate_result_payload(
    request: CompleteRequest,
    forge: Any | None,
    active: Any | None,
    expected_base_ref: str | None,
    current_head_sha: str | None,
) -> tuple[bool, str | None, CompleteFailureReason | None]:
    if request.result == RESULT_DONE:
        assert isinstance(request.payload, DonePayload)
        return _validate_pull_request(
            request.payload, forge, active, expected_base_ref, current_head_sha
        )
    if request.result == RESULT_BLOCKED:
        assert isinstance(request.payload, BlockedPayload)
        return _validate_blocked_payload(request.payload)
    return True, None, None


def evaluate_complete_preflight(
    request: CompleteRequest,
    *,
    worktree_path: Path | str | None = None,
    forge: Any | None = None,
    run_state: Any | None = None,
    expected_base_ref: str | None = None,
    current_head_sha: str | None = None,
) -> CompletePreflight:
    """Evaluate ownership, result, PR, and worktree preconditions."""
    try:
        request.validate()
    except ValueError as exc:
        return CompletePreflight(
            accepted=False,
            reason=str(exc),
            failure_reason=CompleteFailureReason.INVALID_REQUEST,
        )

    ok, reason, failure_reason, active = _validate_ownership(request, run_state)
    if not ok:
        return CompletePreflight(
            accepted=False, reason=reason, failure_reason=failure_reason
        )

    resolved_path = _resolve_worktree_path(request, worktree_path, active)
    status = inspect_worktree_status(resolved_path)

    ok, reason, failure_reason = _validate_worktree_status(request.result, status)
    if not ok:
        return CompletePreflight(
            accepted=False,
            worktree_status=status,
            reason=reason,
            failure_reason=failure_reason,
        )

    ok, reason, failure_reason = _validate_result_payload(
        request, forge, active, expected_base_ref, current_head_sha
    )
    if not ok:
        return CompletePreflight(
            accepted=False,
            worktree_status=status,
            reason=reason,
            failure_reason=failure_reason,
        )

    return CompletePreflight(accepted=True, worktree_status=status)


__all__ = [
    "CompletePreflight",
    "WorktreeStatus",
    "evaluate_complete_preflight",
    "inspect_worktree_status",
]
