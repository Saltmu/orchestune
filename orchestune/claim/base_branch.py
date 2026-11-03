"""Base branch validation and automatic parent branch creation for claims."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from orchestune.claim.contracts import ClaimFailure, ClaimFailureReason
from orchestune.forge import Forge
from orchestune.infra.git_cli import GitResult, ensure_parent_branch
from orchestune.issue_parsing import is_epic_issue
from orchestune.worktree_ops.preparation import local_or_remote_branch_exists

_PARENT_BRANCH_PATTERN = re.compile(r"^parent/issue-(\d+)$")


def ensure_claim_base_ref(
    base_ref: str,
    *,
    forge: Forge,
    repository_root: str | Path | None = None,
    git_runner: Callable[..., GitResult] | None = None,
) -> ClaimFailure | None:
    """Ensure base branch exists prior to worktree creation if it refers to a parent issue.

    If base_ref is not of the form 'parent/issue-<N>', this function returns None.
    If the branch exists locally or remotely, returns None.
    If absent, inspects the parent issue via Forge:
    - If the parent issue is not found or not an EPIC, returns a ClaimFailure
      with BASE_RESOLUTION_FAILED without attempting branch creation.
    - If it is an EPIC, invokes ensure_parent_branch and re-verifies existence.
    """
    match = _PARENT_BRANCH_PATTERN.match(base_ref)
    if not match:
        return None

    if local_or_remote_branch_exists(
        base_ref, cwd=repository_root, git_runner=git_runner
    ):
        return None

    parent_issue_number = int(match.group(1))
    parent_issue = forge.get_issue(parent_issue_number)
    if parent_issue is None or not is_epic_issue(parent_issue):
        return ClaimFailure(
            reason=ClaimFailureReason.BASE_RESOLUTION_FAILED,
            message=(
                f"Parent issue #{parent_issue_number} does not exist or is not "
                f"an EPIC issue; cannot create base branch '{base_ref}'."
            ),
            next_actions=(
                f"Verify that parent issue #{parent_issue_number} is an EPIC issue.",
                (
                    f"Run `orchestune-dispatch -p {parent_issue_number} --apply` "
                    f"or manually create '{base_ref}' on origin and retry."
                ),
            ),
        )

    ensure_parent_branch(parent_issue_number, cwd=repository_root)

    if not local_or_remote_branch_exists(
        base_ref, cwd=repository_root, git_runner=git_runner
    ):
        return ClaimFailure(
            reason=ClaimFailureReason.BASE_RESOLUTION_FAILED,
            message=(
                f"Failed to create or verify base branch '{base_ref}' after "
                "ensuring parent branch."
            ),
            next_actions=(
                f"Verify that parent issue #{parent_issue_number} is an EPIC issue.",
                (
                    f"Run `orchestune-dispatch -p {parent_issue_number} --apply` "
                    f"or manually create '{base_ref}' on origin and retry."
                ),
            ),
        )

    return None
