"""run_state.jsonの欠損・不整合をGitHub APIとローカルgit worktreeから自動復元する自己修復ロジック。"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from orchestune import github
from orchestune.dispatch_gc import is_process_alive
from orchestune.dispatch_scoring import _FOOTPRINT_BLOCK_PATTERN
from orchestune.dispatch_state import ActiveWorktree
from orchestune.dispatch_targets import DispatchHandle

if TYPE_CHECKING:
    from orchestune.dispatch_state import RunState
    from orchestune.dispatcher import DispatcherConfig


def _is_worktree_complete(active: ActiveWorktree, config: DispatcherConfig) -> bool:
    """#215: `external_id`が設定されている（ローカルpid以外でディスパッチされた）
    active worktreeは、設定されたdispatch_targetの`is_complete`に完了判定を委譲する。
    それ以外（従来通りのローカルsubprocess起動）は`is_process_alive`ベースのまま。"""
    if active.external_id is not None:
        handle = DispatchHandle(
            pid=active.pid,
            external_id=active.external_id,
            external_url=active.external_url,
            branch_name=active.branch,
            issue_number=active.issue_number,
        )
        assert config.dispatch_target is not None
        return config.dispatch_target.is_complete(handle)
    return not is_process_alive(active.pid)


def _parse_subtask_info_from_issue(
    issue: github.IssueRecord,
) -> tuple[str, tuple[str, ...]]:
    """Issueの本文から subtask_id と declared_footprint を抽出する。"""
    match = _FOOTPRINT_BLOCK_PATTERN.search(issue.body)
    subtask_id = None
    declared_footprint = ()
    if match:
        try:
            data = yaml.safe_load(match.group(1))
            if isinstance(data, dict):
                subtask_id = data.get("subtask_id")
                footprint = data.get("footprint", [])
                if isinstance(footprint, list):
                    declared_footprint = tuple(footprint)
        except Exception:
            pass

    if not subtask_id:
        subtask_id = f"issue-{issue.number}"

    return subtask_id, declared_footprint


def _restore_missing_active_worktrees(
    run_state: RunState,
    in_progress_issues: list[github.IssueRecord],
    config: DispatcherConfig,
) -> bool:
    """in-progressなIssueからActiveWorktreeを復元する。"""
    missing_issues = []
    for issue in in_progress_issues:
        subtask_id, declared_footprint = _parse_subtask_info_from_issue(issue)
        if str(issue.number) not in run_state.active_worktrees:
            missing_issues.append((issue, subtask_id, declared_footprint))

    if not missing_issues:
        return False

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

        run_state.active_worktrees[str(issue.number)] = ActiveWorktree(
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
        )
        print(
            f"Self-healing: Restored active worktree state for subtask '{subtask_id}' (Issue #{issue.number})",
            file=sys.stderr,
        )

    return True


def _warn_missing_physical_worktrees(run_state: RunState) -> None:
    """物理的な git worktree が存在しない場合に警告ログを出す。"""
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
