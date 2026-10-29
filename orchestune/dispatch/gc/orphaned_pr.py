"""Generation-bound evidence for orphaned pull request recovery."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from orchestune.claim.adopt_pr import orphan_marker
from orchestune.claim.adopt_pr import orphan_receipt as orphan_receipt
from orchestune.dispatch.gc.completion_contracts import CompletedWorktreeDecision
from orchestune.ledger.run_state import ActiveWorktree, RunState
from orchestune.models import PrRecord


def find_orphaned_pr(
    active: ActiveWorktree,
    prs: Sequence[PrRecord],
    root: str | Path | None,
    git: Callable[..., Any],
) -> PrRecord | None:
    candidates = [
        pr
        for pr in prs
        if pr.state.upper() == "OPEN"
        and pr.head_ref == active.core.branch
        and pr.is_cross_repository is False
        and pr.head_sha
    ]
    if len(candidates) != 1:
        return None
    ref = (
        f"refs/remotes/origin/{active.core.branch}"
        if active.launch.external_id
        else f"refs/heads/{active.core.branch}"
    )
    try:
        result = git(
            ["rev-parse", "--verify", ref],
            cwd=root or Path(active.core.worktree_path).parent,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    return candidates[0] if result.stdout.strip() == candidates[0].head_sha else None


def orphan_decision(
    active: ActiveWorktree,
    subtask: str,
    prs: Sequence[PrRecord],
    root: str | Path | None,
    git: Callable[..., Any],
) -> CompletedWorktreeDecision | None:
    pr = find_orphaned_pr(active, prs, root, git)
    return (
        CompletedWorktreeDecision(
            action="completed_orphaned_pr",
            subtask_id=subtask,
            commit_sha=pr.head_sha,
            orphaned_pr=pr,
        )
        if pr
        else None
    )


def saved_orphan_decision(
    active: ActiveWorktree,
    subtask: str,
    prs: Sequence[PrRecord] | None,
    state: RunState | None,
) -> CompletedWorktreeDecision | None:
    if state is None or not active.claim.claim_id or not active.claim.repository_id:
        return None
    key = f"orphaned-pr::{active.claim.repository_id}::{active.core.issue_number}::{active.claim.claim_id}"
    receipt = state.recovery_receipts.get(key)
    if receipt is None:
        return None
    candidates = [
        pr
        for pr in (prs or ())
        if pr.number == receipt.get("pr_number")
        and pr.state.upper() == "OPEN"
        and pr.is_cross_repository is False
    ]
    if len(candidates) == 1:
        pr = candidates[0]
        try:
            if orphan_receipt(active, pr)[1] == receipt:
                return CompletedWorktreeDecision(
                    action="completed_orphaned_pr",
                    subtask_id=subtask,
                    commit_sha=pr.head_sha,
                    orphaned_pr=pr,
                )
        except ValueError:
            pass
    return CompletedWorktreeDecision(
        action="completion_skipped_forge_error",
        subtask_id=subtask,
        operation="orphaned_pr_replay",
        error="Saved orphan evidence or current PR cannot be verified",
    )


def orphan_message(active: ActiveWorktree, pr: PrRecord) -> str:
    return (
        f"{orphan_marker(active.claim.claim_id or '', pr)}\n"
        f"completed_orphaned_pr: PR #{pr.number}, head `{pr.head_sha}`. "
        "ローカル CI と現在の HEAD に対するレビュー証跡は再確認が必要です。\n\n"
        f"`orchestune claim {active.core.issue_number} --adopt-pr {pr.number}` → "
        "`./scripts/local-ci.sh` → "
        f"`orchestune complete --issue {active.core.issue_number} --pr {pr.number} "
        "--result done --reviewer <bot> --review-reply <file>`。\n"
        "手動マージは Layer-1 ゲートを通らない代替手段です。"
    )


def save_orphan_receipt(
    active: ActiveWorktree,
    pr: PrRecord,
    state: RunState,
    path: Path,
    save: Callable[..., Any],
) -> None:
    key, receipt = orphan_receipt(active, pr)
    previous = state.recovery_receipts.get(key)
    if previous is not None and previous != receipt:
        raise RuntimeError("orphaned PR evidence changed during escalation")
    state.recovery_receipts[key] = receipt
    try:
        save(state, path)
    except Exception:
        if previous is None:
            del state.recovery_receipts[key]
        raise
