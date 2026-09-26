"""Read-only evidence and worktree decisions for handoff-ready task GC."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from orchestune.claim.workspace import ClaimWorkspace
from orchestune.dispatch.claim_marker import claim_marker_path, read_claim_marker
from orchestune.dispatch.gc.git import (
    VerifiedWorktreeRemovalRequest,
    evaluate_worktree_removal,
)
from orchestune.dispatch.gc.outcome_decision import (
    _is_handoff_ready,
    _is_handoff_retained_dirty,
)
from orchestune.dispatch.state import ActiveWorktree
from orchestune.infra.git_cli import run_git
from orchestune.models import PrRecord
from orchestune.outcome_record import OutcomeRecord, parse_from_comments


class HandoffForge(Protocol):
    """Read-only GitHub operations needed to verify a completion handoff."""

    def list_all_issue_comments(
        self, issue_number: int | str
    ) -> list[dict[str, Any]]: ...

    def get_pull_request(self, pr_number: int | str) -> PrRecord: ...

    def is_merge_commit_reachable_from(self, commit_oid: str, base: str) -> bool: ...


@dataclass(frozen=True)
class GcRequest:
    state_path: Path | None = None
    apply: bool = True
    timeout_seconds: float = 0.0


@dataclass(frozen=True)
class HandoffPlan:
    key: str
    issue_number: int
    result: str | None
    action: str
    reason: str
    worktree_action: str
    outcome: OutcomeRecord | None = None
    removal_request: VerifiedWorktreeRemovalRequest | None = None


def _held_plan(
    active: ActiveWorktree, reason: str, outcome: OutcomeRecord | None = None
) -> HandoffPlan:
    return HandoffPlan(
        key=str(active.issue_number),
        issue_number=active.issue_number,
        result=active.completion_result,
        action="hold",
        reason=reason,
        worktree_action="retain",
        outcome=outcome,
    )


def _verify_outcome(
    active: ActiveWorktree, forge: HandoffForge
) -> tuple[OutcomeRecord | None, str | None]:
    required = (
        active.claim_id,
        active.completion_id,
        active.completion_result,
        active.completion_comment_id,
        active.completion_comment_url,
    )
    if not all(isinstance(value, str) and value for value in required):
        return None, "handoff_evidence_missing"
    payload = active.completion_payload
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("outcome"), str)
        or not payload["outcome"]
    ):
        return None, "handoff_evidence_missing"
    try:
        comments = forge.list_all_issue_comments(active.issue_number)
    except Exception:
        return None, "outcome_unknown"

    comment = next(
        (
            item
            for item in comments
            if str(item.get("id")) == active.completion_comment_id
        ),
        None,
    )
    if comment is None:
        return None, "outcome_absent"
    if comment.get("html_url") != active.completion_comment_url:
        return None, "outcome_mismatch"
    if comment.get("body") != payload["outcome"]:
        return None, "outcome_mismatch"
    outcome = parse_from_comments([comment])
    if outcome is None:
        return None, "outcome_absent"
    if not _outcome_matches_active(outcome, active):
        return None, "outcome_mismatch"
    return outcome, None


def _outcome_matches_active(outcome: OutcomeRecord, active: ActiveWorktree) -> bool:
    if (
        outcome.issue != active.issue_number
        or outcome.claim_id != active.claim_id
        or outcome.completion_id != active.completion_id
        or outcome.result != active.completion_result
    ):
        return False
    if outcome.result == "done":
        return (
            isinstance(outcome.pr, int)
            and not isinstance(outcome.pr, bool)
            and outcome.pr > 0
        )
    return outcome.result in {"blocked", "not-needed"}


def _normalise_base_ref(ref: str, *, remote_names: frozenset[str]) -> str:
    if ref.startswith("refs/heads/"):
        return ref.removeprefix("refs/heads/")
    remote_ref = ref.removeprefix("refs/remotes/")
    remote, separator, branch = remote_ref.partition("/")
    if separator and remote in remote_names:
        return branch
    return ref


def _verify_merged_pr(
    active: ActiveWorktree,
    outcome: OutcomeRecord,
    forge: HandoffForge,
    workspace: ClaimWorkspace,
) -> str | None:
    pr_number = outcome.pr
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
        return "done_pr_missing"
    try:
        pr = forge.get_pull_request(pr_number)
    except Exception:
        return "pr_unknown"
    try:
        remote_names = frozenset(
            run_git(
                ["remote"], cwd=workspace.repository_root, check=False
            ).stdout.splitlines()
        )
    except OSError:
        remote_names = frozenset()
    expected_base = _normalise_base_ref(
        active.base_ref or active.base_branch, remote_names=remote_names
    )
    actual_base = pr.base_ref
    if (
        pr.number != outcome.pr
        or pr.head_ref != active.branch
        or actual_base != expected_base
        or pr.is_cross_repository is not False
    ):
        return "pr_mismatch"
    if pr.state.upper() != "MERGED":
        return "pr_not_merged"
    if not pr.merged_at or not pr.merge_commit_oid:
        return "merge_unverified"
    try:
        reachable = forge.is_merge_commit_reachable_from(
            pr.merge_commit_oid, expected_base
        )
    except Exception:
        return "merge_unverified"
    return None if reachable else "merge_unverified"


def _marker_problem(
    active: ActiveWorktree, path: Path, *, required: bool
) -> str | None:
    marker_path = claim_marker_path(path)
    marker_present = marker_path.exists() or marker_path.is_symlink()
    if not marker_present:
        return "owner_unknown" if required else None
    if marker_path.is_symlink():
        return "owner_unknown"
    marker = read_claim_marker(path)
    if marker is None:
        return "owner_unknown"
    if (
        marker.get("claim_id") != active.claim_id
        or marker.get("branch") != active.branch
    ):
        return "owner_mismatch"
    return None


def _is_current_worktree(path: Path, cwd: Path) -> bool:
    target = path.resolve(strict=False)
    current = cwd.resolve(strict=False)
    return target == current or target in current.parents


def _verify_done_head(outcome: OutcomeRecord, worktree: Path) -> str | None:
    if outcome.result != "done" or not outcome.head_sha:
        return None
    try:
        head = run_git(["rev-parse", "HEAD"], cwd=worktree, check=True).stdout.strip()
    except Exception:
        return "head_unknown"
    return None if head == outcome.head_sha else "head_changed"


def _inspect_worktree(
    active: ActiveWorktree,
    outcome: OutcomeRecord,
    workspace: ClaimWorkspace,
    cwd: str | Path | None,
) -> tuple[str, str, str, VerifiedWorktreeRemovalRequest | None]:
    raw_path = active.worktree_path.strip()
    if not raw_path:
        return "hold", "worktree_path_missing", "retain", None
    primary_root = workspace.worktree_root.parent
    target = Path(raw_path)
    if not target.is_absolute():
        target = primary_root / target
    target = Path(target.absolute())
    current = Path(cwd) if cwd is not None else Path.cwd()
    if _is_current_worktree(target, current):
        return "hold", "current_worktree", "retain", None
    if target.is_symlink():
        return "hold", "symlink_mismatch", "retain", None

    inspected = replace(active, worktree_path=str(target))
    if not target.exists():
        return _inspect_absent_worktree(inspected, target, outcome, primary_root)
    if problem := _marker_problem(inspected, target, required=True):
        return "hold", problem, "retain", None
    evaluation = evaluate_worktree_removal(inspected, repo_root=primary_root)
    if evaluation.rejection_reason == "dirty_worktree":
        if _is_handoff_retained_dirty(inspected, outcome):
            return "release", "outcome_verified", "retain", None
        return "hold", "dirty_worktree", "retain", None
    if not evaluation.can_remove or evaluation.request is None:
        return (
            "hold",
            evaluation.rejection_reason or "worktree_unverified",
            "retain",
            None,
        )
    if problem := _verify_done_head(outcome, target):
        return "hold", problem, "retain", None
    reason = "already_merged" if outcome.result == "done" else "outcome_verified"
    return "release", reason, "remove", evaluation.request


def _inspect_absent_worktree(
    active: ActiveWorktree,
    target: Path,
    outcome: OutcomeRecord,
    primary_root: Path,
) -> tuple[str, str, str, VerifiedWorktreeRemovalRequest | None]:
    evaluation = evaluate_worktree_removal(active, repo_root=primary_root)
    if evaluation.rejection_reason != "unregistered_worktree":
        problem = _marker_problem(active, target, required=False)
        if problem:
            return "hold", problem, "absent", None
        reason = (
            "registered_missing_worktree"
            if evaluation.can_remove
            else evaluation.rejection_reason or "worktree_unverified"
        )
        return "hold", reason, "absent", None
    if problem := _marker_problem(active, target, required=False):
        return "hold", problem, "absent", None
    reason = "already_merged" if outcome.result == "done" else "outcome_verified"
    return "release", reason, "absent", None


def inspect_handoff(
    active: ActiveWorktree,
    workspace: ClaimWorkspace,
    forge: HandoffForge,
    cwd: str | Path | None = None,
) -> HandoffPlan:
    """Verify durable completion evidence and evaluate a single handoff entry."""
    if not _is_handoff_ready(active):
        return _held_plan(active, "not_handoff_ready")
    if active.repository_id != workspace.repository_identity:
        return _held_plan(active, "repository_mismatch")
    outcome, error = _verify_outcome(active, forge)
    if outcome is None:
        return _held_plan(active, error or "outcome_absent")
    if outcome.result == "done":
        if error := _verify_merged_pr(active, outcome, forge, workspace):
            return _held_plan(active, error, outcome)

    action, reason, worktree_action, request = _inspect_worktree(
        active, outcome, workspace, cwd
    )
    return HandoffPlan(
        key=str(active.issue_number),
        issue_number=active.issue_number,
        result=outcome.result,
        action=action,
        reason=reason,
        worktree_action=worktree_action,
        outcome=outcome,
        removal_request=request,
    )
