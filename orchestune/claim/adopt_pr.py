"""Fail-closed identity checks for explicitly adopted orphaned pull requests."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from orchestune.branch_naming import build_task_branch_name
from orchestune.claim.contracts import ClaimFailure, ClaimFailureReason
from orchestune.claim.workspace import ClaimWorkspace
from orchestune.forge import Forge
from orchestune.labels import StatusLabel
from orchestune.ledger.active_codec import (
    decode_active_worktree,
    encode_active_worktree,
)
from orchestune.ledger.active_records import ActiveWorktree
from orchestune.ledger.external_stop_receipts import matching_confirmation
from orchestune.ledger.run_state import RunState
from orchestune.models import PrRecord
from orchestune.pr_link_notice import pr_matches_issue
from orchestune.pr_link_notice_trust import NoticePresence, TrustContext


def orphan_marker(claim_id: str, pr: PrRecord) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", claim_id):
        raise ValueError("Invalid claim identity")
    return f"<!-- orchestune:orphaned-pr:{claim_id}:{pr.number}:{pr.head_sha} -->"


def duplicate_marker(pr: PrRecord) -> str:
    return f"<!-- orchestune:duplicate-pr:{pr.number}:{pr.head_sha} -->"


def validate_escalation_reason(
    comments: Sequence[dict[str, Any]],
    marker: str,
    trusted: Callable[[dict[str, Any]], bool],
    *,
    duplicate: str | None = None,
) -> None:
    relevant = [
        comment
        for comment in comments
        if StatusLabel.BLOCKED_HUMAN_REVIEW.value in str(comment.get("body", ""))
        or "<!-- orchestune:orphaned-pr:" in str(comment.get("body", ""))
        or "<!-- orchestune:duplicate-pr:" in str(comment.get("body", ""))
    ]
    if (
        not relevant
        or not any(
            value and value in str(relevant[-1].get("body", ""))
            for value in (marker, duplicate)
        )
        or not trusted(relevant[-1])
    ):
        raise ValueError("Latest escalation is not a trusted matching orphan PR notice")


def validate_adoption_pr(
    prs: Sequence[PrRecord],
    issue_number: int,
    pr_number: int,
    canonical_branch: str,
    base_branch: str,
) -> PrRecord:
    """Return the unique same-repository open PR with the expected identity."""
    candidates = [
        pr
        for pr in prs
        if pr.state.upper() == "OPEN"
        and (pr.head_ref == canonical_branch or pr_matches_issue(pr, issue_number))
    ]
    if len(candidates) != 1:
        raise ValueError("Expected exactly one open PR associated with the issue")
    pr = candidates[0]
    if (
        pr.number != pr_number
        or pr.is_cross_repository is not False
        or pr.head_ref != canonical_branch
        or pr.base_ref != base_branch
        or not re.fullmatch(r"[0-9a-f]{40}", pr.head_sha)
    ):
        raise ValueError("PR identity, repository, base, or head does not match")
    return pr


def inspect_adoption(
    issue: Any,
    preflight: Any,
    number: int | None,
    default_base: str,
    workspace: ClaimWorkspace,
    state: RunState,
    forge: Forge,
    observe_external: Callable[[ActiveWorktree, ClaimWorkspace], tuple[str, str]]
    | None,
    alive: Callable[[int | None], bool],
    git: Callable[..., Any],
) -> tuple[str, str, PrRecord]:
    if {label for label in issue.labels if label.startswith("status:")} != {
        StatusLabel.BLOCKED_HUMAN_REVIEW
    }:
        raise ValueError("Adoption requires only status:blocked-human-review")
    if number is None:
        raise ValueError("PR number is required")
    branch = build_task_branch_name(issue.number, preflight.subtask_id)
    base = preflight.base_ref or default_base
    base_name = base.removeprefix("refs/remotes/origin/").removeprefix("origin/")
    pr = validate_adoption_pr(
        forge.list_prs(state="open"), issue.number, number, branch, base_name
    )
    validate_adoption_evidence(
        pr, issue.number, workspace, state, forge, observe_external, alive, git
    )
    return branch, base, pr


def validate_orphan_evidence(
    state: RunState,
    repository: str,
    issue_number: int,
    pr: PrRecord,
    is_stopped: Callable[[ActiveWorktree], bool],
    *,
    allowed_claim_id: str | None = None,
) -> ActiveWorktree:
    """Validate a unique retained execution identity and freshly prove it stopped."""
    _check_active_claims(state, issue_number, allowed_claim_id)
    records = [
        (key, record)
        for key, record in state.recovery_receipts.items()
        if record.get("operation") == "orphaned-pr"
        and record.get("repository_id") == repository
        and record.get("issue_number") == issue_number
    ]
    if len(records) != 1:
        raise ValueError("Orphan execution evidence is missing or ambiguous")
    key, record = records[0]
    try:
        old = decode_active_worktree(record["active"])
        old.claim.validate()
        old.launch.validate()
        claim_id = old.claim.claim_id
        expected_key = f"orphaned-pr::{repository}::{issue_number}::{claim_id}"
        if (
            record.get("schema_version") != 1
            or not claim_id
            or key != expected_key
            or old.claim.repository_id != repository
            or old.core.issue_number != issue_number
            or old.core.branch != pr.head_ref
            or record.get("claim_id") != claim_id
            or record.get("pr_number") != pr.number
            or record.get("head_sha") != pr.head_sha
        ):
            raise ValueError("Orphan execution evidence does not match the PR")
        if not is_stopped(old):
            raise ValueError("Previous execution is not confirmed stopped")
    except (KeyError, TypeError) as error:
        raise ValueError("Orphan execution evidence is invalid") from error
    return old


def _check_active_claims(
    state: RunState, issue_number: int, allowed_claim_id: str | None
) -> None:
    active = [
        a
        for a in state.active_worktrees.values()
        if a.core.issue_number == issue_number
    ]
    if active and (
        len(active) != 1
        or allowed_claim_id is None
        or active[0].claim.claim_id != allowed_claim_id
    ):
        raise ValueError("An active claim still exists for the issue")


def validate_adoption_evidence(
    pr: PrRecord,
    issue_number: int,
    workspace: ClaimWorkspace,
    state: RunState,
    forge: Forge,
    observe_external: Callable[[ActiveWorktree, ClaimWorkspace], tuple[str, str]]
    | None,
    alive: Callable[[int | None], bool],
    git: Callable[..., Any],
    allowed_claim_id: str | None = None,
) -> None:
    def stopped(old: ActiveWorktree) -> bool:
        if old.launch.pid is not None and alive(old.launch.pid):
            return False
        if old.launch.external_id is not None:
            status, _ = (
                observe_external(old, workspace)
                if observe_external
                else ("unknown", "unavailable")
            )
            return status == "stopped" or (
                status == "unknown"
                and matching_confirmation(state, old, workspace.repository_identity)
            )
        return old.launch.pid is not None and old.launch.started_at is not None

    old = validate_orphan_evidence(
        state,
        workspace.repository_identity,
        issue_number,
        pr,
        stopped,
        allowed_claim_id=allowed_claim_id,
    )
    trust = TrustContext(forge)
    validate_escalation_reason(
        forge.list_comments(issue_number),
        orphan_marker(old.claim.claim_id or "", pr),
        lambda comment: trust.evaluate_candidate_markers([comment])[0]
        is NoticePresence.PRESENT,
        duplicate=duplicate_marker(pr),
    )
    tip = git(
        ["ls-remote", "--heads", "origin", f"refs/heads/{pr.head_ref}"],
        cwd=workspace.repository_root,
        check=True,
    )
    if tip.stdout.strip().split() != [pr.head_sha, f"refs/heads/{pr.head_ref}"]:
        raise ValueError("Remote branch tip does not match PR head")


def revalidate_adoption(
    active: ActiveWorktree,
    workspace: ClaimWorkspace,
    state: RunState,
    forge: Forge,
    observe_external: Callable[[ActiveWorktree, ClaimWorkspace], tuple[str, str]]
    | None,
    alive: Callable[[int | None], bool],
    git: Callable[..., Any],
) -> ClaimFailure | None:
    try:
        number, sha = active.claim.adopted_pr_number, active.claim.adopted_pr_head_sha
        if number is None or sha is None:
            raise ValueError("Adopted PR identity is missing")
        base = (
            (active.claim.base_ref or active.core.base_branch)
            .removeprefix("refs/remotes/origin/")
            .removeprefix("origin/")
        )
        pr = validate_adoption_pr(
            forge.list_prs(state="open"),
            active.core.issue_number,
            number,
            active.core.branch,
            base,
        )
        if pr.head_sha != sha:
            raise ValueError("PR head changed after adoption")
        validate_adoption_evidence(
            pr,
            active.core.issue_number,
            workspace,
            state,
            forge,
            observe_external,
            alive,
            git,
            active.claim.claim_id,
        )
    except Exception as error:
        return ClaimFailure(
            reason=ClaimFailureReason.INVALID_RESUME,
            message=f"Adopted PR revalidation failed: {error}",
        )
    return None


def revalidate_if_adopted(
    active: ActiveWorktree,
    workspace: ClaimWorkspace,
    state: RunState,
    forge: Forge,
    observe_external: Callable[[ActiveWorktree, ClaimWorkspace], tuple[str, str]]
    | None,
    alive: Callable[[int | None], bool],
    git: Callable[..., Any],
) -> ClaimFailure | None:
    if (
        active.claim.adopted_pr_number is None
        or active.claim.claim_stage == "completed"
    ):
        return None
    return revalidate_adoption(
        active, workspace, state, forge, observe_external, alive, git
    )


def verify_adopted_head(
    active: ActiveWorktree, forge: Forge, git: Callable[..., Any]
) -> None:
    number, sha = active.claim.adopted_pr_number, active.claim.adopted_pr_head_sha
    if number is None or sha is None or not active.core.worktree_path:
        raise ValueError("Adopted PR identity or worktree is missing")
    base = (
        (active.claim.base_ref or active.core.base_branch)
        .removeprefix("refs/remotes/origin/")
        .removeprefix("origin/")
    )
    pr = validate_adoption_pr(
        forge.list_prs(state="open"),
        active.core.issue_number,
        number,
        active.core.branch,
        base,
    )
    if pr.head_sha != sha:
        raise ValueError("PR head changed after adoption")
    worktree = Path(active.core.worktree_path)
    remote = git(
        ["ls-remote", "--heads", "origin", f"refs/heads/{active.core.branch}"],
        cwd=worktree,
        check=True,
    )
    if remote.stdout.strip().split() != [sha, f"refs/heads/{active.core.branch}"]:
        raise ValueError("Remote branch head changed after adoption")
    local = git(["rev-parse", "--verify", "HEAD"], cwd=worktree, check=True)
    if local.stdout.strip() != sha:
        raise ValueError("Worktree head does not match the adopted PR")


def orphan_receipt(active: ActiveWorktree, pr: PrRecord) -> tuple[str, dict[str, Any]]:
    """Capture the retired execution; consumers must independently check it stopped."""
    repository = active.claim.repository_id
    claim = active.claim.claim_id
    if not repository or not claim:
        raise ValueError("orphaned execution has no repository or claim identity")
    if pr.head_ref != active.core.branch or not pr.head_sha or pr.number <= 0:
        raise ValueError("orphaned PR does not match the execution")
    key = f"orphaned-pr::{repository}::{active.core.issue_number}::{claim}"
    return key, {
        "schema_version": 1,
        "operation": "orphaned-pr",
        "repository_id": repository,
        "issue_number": active.core.issue_number,
        "claim_id": claim,
        "pr_number": pr.number,
        "head_sha": pr.head_sha,
        "active": encode_active_worktree(active),
    }
