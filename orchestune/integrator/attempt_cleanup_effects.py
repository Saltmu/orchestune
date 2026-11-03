"""Execute side effects for Layer-2 integration attempt cleanups (#1440, #1597)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from orchestune.forge import Forge, ForgeAuthError
from orchestune.infra.git_cli import (
    ConditionalBranchDeletionResult as Deletion,
)
from orchestune.integrator.attempt import OpenAttempt
from orchestune.integrator.attempt_cleanup_plan import (
    AttemptCleanupPlan,
    _Hold,
    _Target,
)
from orchestune.issue_notice import render_notice
from orchestune.review.semantic_gate import SemanticReviewStateV1


class AttemptCleanupEffects:
    """Side effects for attempt cleanups: notices, PR closure, branch deletion."""

    def __init__(
        self,
        forge: Forge,
        repository_root: Path,
        parent_issue_number: int,
        plan: AttemptCleanupPlan,
        parent_tip: Callable[[Path, int], str | None],
        delete_ref: Callable[[Path, str, str], Deletion],
    ) -> None:
        self._forge = forge
        self._root = repository_root
        self._parent = parent_issue_number
        self._plan = plan
        self._parent_tip = parent_tip
        self._delete_ref = delete_ref

    def post(self, target: _Target, kind: str, body: str) -> None:
        number = target.pr.number
        try:
            self._forge.add_comment(number, render_notice(kind, body))
        except ForgeAuthError:
            raise
        except Exception:
            # The answer may have been lost after the comment landed: look again
            # before deciding, and never send it twice.
            target.comments = self._forge.list_all_issue_comments(number)
            if not self._plan.noticed(target.comments, kind):
                raise

    def close(self, target: _Target) -> None:
        number = target.pr.number
        self._forge.close_pull_request(number)
        after = self._forge.get_pull_request(number)
        if after.state.upper() == "MERGED" or after.merged_at:
            raise _Hold("blocked", "the PR was merged; no destructive cleanup")
        if after.state.upper() != "CLOSED":
            raise _Hold("pending", "close not confirmed by the read-back")
        target.pr = after if after.head_ref else target.pr

    def delete(self, target: _Target) -> None:
        pr, state = target.pr, target.state
        current = self._forge.get_pull_request(pr.number)
        if current.state.upper() == "MERGED" or current.merged_at:
            raise _Hold("blocked", "the PR was merged; no destructive cleanup")
        if current.state.upper() != "CLOSED":
            raise _Hold("pending", "the PR is not closed yet")
        others = [
            p
            for p in self._forge.list_prs(state="open", head=pr.head_ref)
            if p.number != pr.number
        ]
        if others:
            raise _Hold("blocked", "the branch is used by another open PR; kept")
        try:
            result = self._delete_ref(self._root, pr.head_ref, state.head_sha)
        except ValueError as error:
            raise _Hold(
                "blocked", f"the recorded head is not a commit: {error}"
            ) from error
        if result in {Deletion.DELETED, Deletion.ALREADY_ABSENT}:
            return
        if result is Deletion.TIP_MISMATCH:
            raise _Hold("blocked", "lease rejected: the branch tip moved; kept")
        if result is Deletion.DENIED:
            raise _Hold("failed", "deletion denied by the remote")
        raise _Hold("failed", "deletion failed")

    def notice(
        self, state: SemanticReviewStateV1, successor: OpenAttempt | None
    ) -> str:
        current = self._parent_tip(self._root, self._parent)
        moved = current is not None and current != state.base_sha
        reason = (
            "the parent branch moved after this attempt was built"
            if moved
            else "the child set or the head of the attempt changed"
        )
        follow = (
            "announced in the next comment"
            if successor is not None
            else "not created yet; a separate comment names it once it exists"
        )
        return (
            "## Layer-2 semantic review attempt invalidated\n\n"
            "This integration attempt no longer matches its parent branch, so it is "
            "out of scope for review and merge. The PR is closed and the attempt "
            "branch is removed; the work continues in a newer attempt.\n\n"
            f"- Attempt: `{state.attempt_id}`\n"
            f"- Reason: {reason}\n"
            f"- Base when the attempt was built: `{state.base_sha}`\n"
            f"- Parent branch `{self._plan.parent_branch}` tip now: `{current or 'unavailable'}`\n"
            f"- Head of this attempt: `{state.head_sha}`\n"
            f"- Successor: {follow}"
        )

    def announcement(self, state: SemanticReviewStateV1, successor: OpenAttempt) -> str:
        replacement = successor.state
        assert replacement is not None
        return (
            "## Layer-2 semantic review attempt superseded\n\n"
            f"Review and merge continue in PR #{successor.pr_number} "
            f"(attempt `{replacement.attempt_id}`, base `{replacement.base_sha}`). "
            f"This PR (attempt `{state.attempt_id}`) is closed and must not be merged."
        )


__all__ = ["AttemptCleanupEffects"]
