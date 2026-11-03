"""Active-worktree garbage collection public interface."""

from __future__ import annotations

from orchestune.dispatch.gc.completion import (
    CompletedWorktreeDecision,
    is_completion_hold_event,
)
from orchestune.dispatch.gc.git import (
    VerifiedWorktreeRemovalRequest,
    WorktreeRemovalEvaluation,
    WorktreeRemovalResult,
    backup_wip_commit,
    evaluate_worktree_removal,
    remote_branch_commit_sha_if_ahead,
    remove_verified_worktree,
    remove_worktree,
    worktree_has_new_commits,
    worktree_has_uncommitted_changes,
)
from orchestune.dispatch.gc.zombies import (
    ZombieOrTimeoutReclaim,
)
from orchestune.infra.process_utils import is_process_alive

__all__ = [
    "CompletedWorktreeDecision",
    "VerifiedWorktreeRemovalRequest",
    "WorktreeRemovalEvaluation",
    "WorktreeRemovalResult",
    "ZombieOrTimeoutReclaim",
    "backup_wip_commit",
    "evaluate_worktree_removal",
    "is_completion_hold_event",
    "is_process_alive",
    "remote_branch_commit_sha_if_ahead",
    "remove_verified_worktree",
    "remove_worktree",
    "worktree_has_new_commits",
    "worktree_has_uncommitted_changes",
]
