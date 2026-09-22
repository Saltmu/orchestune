"""Shared exit-code contract for claim and complete operations."""

from __future__ import annotations

from enum import IntEnum


class TaskExitCode(IntEnum):
    """Stable, non-overlapping exit codes for task lifecycle commands."""

    SUCCESS = 0
    GENERIC_ERROR = 1

    ISSUE_NOT_FOUND = 10
    ISSUE_CLOSED = 11
    ALREADY_IN_PROGRESS = 12
    TERMINAL_ESCALATION = 13
    UNRESOLVED_DEPENDENCIES = 14
    EXTERNAL_LOCK_CONFLICT = 15
    INVALID_RESUME = 16
    INVALID_BRANCH_NAME = 17

    CLAIM_CONFLICT = 20
    EXISTING_CLAIM_UNRECOVERED = 21
    STATE_LOCK_FAILED = 22

    GIT_FETCH_FAILED = 30
    BASE_RESOLUTION_FAILED = 31
    WORKTREE_CREATION_FAILED = 32
    STATE_SAVE_FAILED = 33
    LABEL_UPDATE_FAILED = 34

    INVALID_REQUEST = 40
    CLAIM_NOT_FOUND = 41
    OWNER_TOKEN_MISMATCH = 42
    INVALID_RESULT_PAYLOAD = 43
    PR_REQUIRED = 44
    PR_PROHIBITED = 45
    REASON_REQUIRED = 46
    DIRTY_WORKTREE = 47
    EVIDENCE_MISSING = 48
    CONCURRENT_COMPLETION = 49
    INVALID_STAGE_TRANSITION = 50
    FORGE_POST_FAILED = 51


_CLAIM_FAILURE_EXIT_CODES: dict[str, TaskExitCode] = {
    "issue_not_found": TaskExitCode.ISSUE_NOT_FOUND,
    "issue_closed": TaskExitCode.ISSUE_CLOSED,
    "already_in_progress": TaskExitCode.ALREADY_IN_PROGRESS,
    "terminal_escalation": TaskExitCode.TERMINAL_ESCALATION,
    "unresolved_dependencies": TaskExitCode.UNRESOLVED_DEPENDENCIES,
    "external_lock_conflict": TaskExitCode.EXTERNAL_LOCK_CONFLICT,
    "invalid_resume": TaskExitCode.INVALID_RESUME,
    "invalid_branch_name": TaskExitCode.INVALID_BRANCH_NAME,
    "claim_conflict": TaskExitCode.CLAIM_CONFLICT,
    "existing_claim_unrecovered": TaskExitCode.EXISTING_CLAIM_UNRECOVERED,
    "state_lock_failed": TaskExitCode.STATE_LOCK_FAILED,
    "git_fetch_failed": TaskExitCode.GIT_FETCH_FAILED,
    "base_resolution_failed": TaskExitCode.BASE_RESOLUTION_FAILED,
    "worktree_creation_failed": TaskExitCode.WORKTREE_CREATION_FAILED,
    "state_save_failed": TaskExitCode.STATE_SAVE_FAILED,
    "label_update_failed": TaskExitCode.LABEL_UPDATE_FAILED,
}

_COMPLETE_FAILURE_EXIT_CODES: dict[str, TaskExitCode] = {
    "invalid_request": TaskExitCode.INVALID_REQUEST,
    "claim_not_found": TaskExitCode.CLAIM_NOT_FOUND,
    "owner_token_mismatch": TaskExitCode.OWNER_TOKEN_MISMATCH,
    "invalid_result_payload": TaskExitCode.INVALID_RESULT_PAYLOAD,
    "pr_required": TaskExitCode.PR_REQUIRED,
    "pr_prohibited": TaskExitCode.PR_PROHIBITED,
    "reason_required": TaskExitCode.REASON_REQUIRED,
    "dirty_worktree": TaskExitCode.DIRTY_WORKTREE,
    "evidence_missing": TaskExitCode.EVIDENCE_MISSING,
    "state_lock_failed": TaskExitCode.STATE_LOCK_FAILED,
    "concurrent_completion": TaskExitCode.CONCURRENT_COMPLETION,
    "invalid_stage_transition": TaskExitCode.INVALID_STAGE_TRANSITION,
    "forge_post_failed": TaskExitCode.FORGE_POST_FAILED,
    "state_save_failed": TaskExitCode.STATE_SAVE_FAILED,
}


def claim_failure_exit_code(reason: str) -> TaskExitCode:
    """Return the shared exit code for a claim failure reason."""
    try:
        return _CLAIM_FAILURE_EXIT_CODES[reason]
    except (KeyError, TypeError) as error:
        raise KeyError(f"Unmapped claim failure reason: {reason!r}") from error


def complete_failure_exit_code(reason: str) -> TaskExitCode:
    """Return the shared exit code for a complete failure reason."""
    try:
        return _COMPLETE_FAILURE_EXIT_CODES[reason]
    except (KeyError, TypeError) as error:
        raise KeyError(f"Unmapped complete failure reason: {reason!r}") from error
