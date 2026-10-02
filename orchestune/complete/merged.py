"""Evidence checks for completion after a PR has already been merged."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestune.branch_naming import parse_task_branch_name
from orchestune.infra.git_cli import run_git
from orchestune.merge_evidence import (
    _identity_status,
    _is_after_reopen,
    merged_pr_problem,
)


def validate_merged_completion(pr: Any, active: Any, forge: Any) -> str | None:
    if active is None or not getattr(active.core, "worktree_path", None):
        return "Merged completion requires its registered claim worktree"
    try:
        path = Path(active.core.worktree_path)
        parsed = parse_task_branch_name(active.core.branch)
        subtask = parsed.subtask_id if parsed else ""
        if _identity_status(pr, active.core.issue_number, subtask) != "match":
            return "Merged PR closing identity does not match the claimed Issue"
        head = run_git(["rev-parse", "HEAD"], cwd=path).stdout.strip()
        if not getattr(pr, "head_sha", None) or pr.head_sha != head:
            return "Merged PR head does not match the claimed worktree HEAD"
        claimed_at = active.claim.claimed_at
        if isinstance(claimed_at, str):
            claimed = datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))
        elif isinstance(claimed_at, int | float) and not isinstance(claimed_at, bool):
            claimed = datetime.fromtimestamp(claimed_at, UTC)
        else:
            return "Claim creation time is missing"
        merged = datetime.fromisoformat(pr.merged_at.replace("Z", "+00:00"))
        if merged < claimed:
            return "Merge predates the current claim generation"
        reopened = forge.get_issue_last_reopened_at(active.core.issue_number)
        if _is_after_reopen(pr.merged_at, reopened) is not True:
            return "Merge evidence predates reopening or is uncertain"
        remotes = set(run_git(["remote"], cwd=path).stdout.splitlines())
        expected_base = active.claim.base_ref or active.core.base_branch
        if expected_base.startswith("refs/heads/"):
            expected_base = expected_base.removeprefix("refs/heads/")
        else:
            ref = expected_base.removeprefix("refs/remotes/")
            remote, separator, branch = ref.partition("/")
            if separator and remote in remotes:
                expected_base = branch
        problem = merged_pr_problem(
            pr,
            pr_number=pr.number,
            branch=active.core.branch,
            base=expected_base,
            reachable=forge.is_merge_commit_reachable_from,
        )
        return f"Merged PR evidence rejected: {problem}" if problem else None
    except (AttributeError, OSError, ValueError, TypeError, RuntimeError) as error:
        return f"Merged PR evidence unavailable: {error}"
