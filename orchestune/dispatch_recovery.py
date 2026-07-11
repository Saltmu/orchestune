"""run_state.json消失時・不整合時の自己修復（self-healing）処理。"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from orchestune import github
from orchestune.dispatch_scoring import _FOOTPRINT_BLOCK_PATTERN
from orchestune.dispatch_state import ActiveWorktree, RunState

if TYPE_CHECKING:
    from orchestune.dispatcher import DispatcherConfig


def _extract_raw_subtask_id(issue: github.IssueRecord) -> str | None:
    """Issue本文のFootprint YAMLブロックから、素のsubtask_id（未検出ならNone）を取り出す。

    呼び出し側ごとにNone時のフォールバック方針が異なる（自己修復ブランチ名生成では
    合成IDへフォールバックするが、依存解決用マップでは未検出issueを含めない）ため、
    フォールバックを持たない共通の抽出処理として切り出している。
    """
    match = _FOOTPRINT_BLOCK_PATTERN.search(issue.body)
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    subtask_id = data.get("subtask_id")
    return str(subtask_id) if subtask_id else None


def _parse_subtask_info_from_issue(
    issue: github.IssueRecord,
) -> tuple[str, tuple[str, ...]]:
    """Issueの本文から subtask_id と declared_footprint を抽出する。"""
    match = _FOOTPRINT_BLOCK_PATTERN.search(issue.body)
    subtask_id = _extract_raw_subtask_id(issue)
    declared_footprint: tuple[str, ...] = ()
    if match:
        try:
            data = yaml.safe_load(match.group(1))
            if isinstance(data, dict):
                footprint = data.get("footprint", [])
                if isinstance(footprint, list):
                    declared_footprint = tuple(footprint)
        except Exception:
            pass

    if not subtask_id:
        subtask_id = f"issue-{issue.number}"

    return subtask_id, declared_footprint


def _decide_missing_active_worktrees(
    run_state: RunState,
    in_progress_issues: list[github.IssueRecord],
    config: DispatcherConfig,
) -> list[tuple[str, str, ActiveWorktree]]:
    """in-progressなIssueのうちrun_stateに欠けているものについて、復元すべき
    ActiveWorktreeを算出する（run_stateへの書き込みは行わない）。

    戻り値は (run_state辞書キー, subtask_id, 復元するActiveWorktree) のリスト。
    """
    missing_issues = []
    for issue in in_progress_issues:
        subtask_id, declared_footprint = _parse_subtask_info_from_issue(issue)
        if str(issue.number) not in run_state.active_worktrees:
            missing_issues.append((issue, subtask_id, declared_footprint))

    if not missing_issues:
        return []

    print(
        f"Self-healing: Found {len(missing_issues)} active issues missing from run_state.",
        file=sys.stderr,
    )

    try:
        open_prs = github.list_open_prs()
    except Exception as e:
        print(
            f"Self-healing warning: Failed to list open PRs: {e}",
            file=sys.stderr,
        )
        open_prs = []

    restorations: list[tuple[str, str, ActiveWorktree]] = []
    for issue, subtask_id, declared_footprint in missing_issues:
        associated_pr = None
        for pr in open_prs:
            if issue.number in pr.closes_issue_numbers:
                associated_pr = pr
                break

        if associated_pr:
            branch_name = associated_pr.head_ref
            external_id = str(associated_pr.number)
            external_url = f"PR#{associated_pr.number}"
        else:
            branch_name = f"claude/issue-{issue.number}-{subtask_id}"
            external_id = None
            external_url = None

        slug = branch_name.replace("/", "-")
        worktree_path = Path(config.worktree_root) / slug

        started_at = time.time()
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(issue.created_at.replace("Z", "+00:00"))
            started_at = dt.timestamp()
        except Exception:
            pass

        restored_base_branch = "origin/main"
        if config.parent_issue_number is not None:
            restored_base_branch = f"parent/issue-{config.parent_issue_number}"

        if issue.blocked_by:
            dep_pr = None
            for pr in open_prs:
                if any(
                    dep_num in pr.closes_issue_numbers for dep_num in issue.blocked_by
                ):
                    dep_pr = pr
                    break
            if dep_pr:
                restored_base_branch = dep_pr.head_ref

        active_worktree = ActiveWorktree(
            issue_number=issue.number,
            branch=branch_name,
            worktree_path=str(worktree_path),
            pid=None,
            started_at=started_at,
            declared_footprint=declared_footprint,
            recompute_count=0,
            forced_serial=False,
            external_id=external_id,
            external_url=external_url,
            base_branch=restored_base_branch,
        )
        restorations.append((str(issue.number), subtask_id, active_worktree))

    return restorations


def _apply_restore_missing_active_worktrees(
    run_state: RunState,
    restorations: list[tuple[str, str, ActiveWorktree]],
) -> bool:
    """decide層が算出した復元内容をrun_state.active_worktreesへ書き込む。"""
    if not restorations:
        return False

    for key, subtask_id, active_worktree in restorations:
        run_state.active_worktrees[key] = active_worktree
        print(
            f"Self-healing: Restored active worktree state for subtask '{subtask_id}' "
            f"(Issue #{active_worktree.issue_number})",
            file=sys.stderr,
        )

    return True


def _restore_missing_active_worktrees(
    run_state: RunState,
    in_progress_issues: list[github.IssueRecord],
    config: DispatcherConfig,
) -> bool:
    """in-progressなIssueからActiveWorktreeを復元する（decide+applyの薄いラッパー）。"""
    restorations = _decide_missing_active_worktrees(run_state, in_progress_issues, config)
    return _apply_restore_missing_active_worktrees(run_state, restorations)


def _warn_missing_physical_worktrees(run_state: RunState) -> None:
    """物理的な git worktree が存在しない場合に警告ログを出す（読み取り専用）。"""
    try:
        res = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        existing_worktree_paths = set()
        for line in res.stdout.splitlines():
            if line.startswith("worktree "):
                existing_worktree_paths.add(Path(line.split(" ", 1)[1]).resolve())
    except Exception as e:
        print(
            f"Self-healing warning: Failed to list git worktrees: {e}",
            file=sys.stderr,
        )
        existing_worktree_paths = None

    if existing_worktree_paths is not None:
        for subtask_id, active in run_state.active_worktrees.items():
            active_path = Path(active.worktree_path).resolve()
            if active_path not in existing_worktree_paths:
                print(
                    f"Self-healing warning: Physical worktree for subtask '{subtask_id}' not found at '{active.worktree_path}'.",
                    file=sys.stderr,
                )


def recover_run_state(
    run_state: RunState,
    in_progress_issues: list[github.IssueRecord],
    config: DispatcherConfig,
) -> bool:
    """run_state.jsonが失われたり不整合が起きている場合に、GitHub API (in_progress_issues / open_prs)
    およびローカルの物理的な git worktree から RunState を自動復元する。
    """
    modified = _restore_missing_active_worktrees(run_state, in_progress_issues, config)
    _warn_missing_physical_worktrees(run_state)
    return modified
