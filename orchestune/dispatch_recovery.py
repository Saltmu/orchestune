"""run_state.json消失時・不整合時の自己修復（self-healing）処理。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from orchestune.dispatch_state import ActiveWorktree, RunState
from orchestune.git_cli import run_git
from orchestune.issue_parsing import FOOTPRINT_BLOCK_PATTERN, parse_task_from_issue
from orchestune.models import IssueRecord, PrRecord

if TYPE_CHECKING:
    from orchestune.dispatch_config import DispatcherConfig


def _extract_raw_subtask_id(issue: IssueRecord) -> str | None:
    """Issue本文のFootprint YAMLブロックから、素のsubtask_id（未検出ならNone）を取り出す。

    呼び出し側ごとにNone時のフォールバック方針が異なる（自己修復ブランチ名生成では
    合成IDへフォールバックするが、依存解決用マップでは未検出issueを含めない）ため、
    フォールバックを持たない共通の抽出処理として切り出している。
    """
    match = FOOTPRINT_BLOCK_PATTERN.search(issue.body)
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
    issue: IssueRecord,
) -> tuple[str, tuple[str, ...]]:
    """Issueの本文から subtask_id と declared_footprint を抽出する。"""
    match = FOOTPRINT_BLOCK_PATTERN.search(issue.body)
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


def _dependency_issue_numbers(
    issue: IssueRecord,
    issue_to_subtask_id: dict[int, str],
    subtask_id_to_issue_number: dict[str, int],
) -> tuple[int, ...]:
    """自己修復に使う依存Issue番号をnative関係またはYAMLから解決する。"""
    if issue.blocked_by:
        return issue.blocked_by

    task = parse_task_from_issue(issue, issue_to_subtask_id)
    return tuple(
        subtask_id_to_issue_number[subtask_id]
        for subtask_id in task.depends_on
        if subtask_id in subtask_id_to_issue_number
    )


def _restored_base_branch(
    issue: IssueRecord,
    open_prs: list[PrRecord],
    issue_to_subtask_id: dict[int, str],
    subtask_id_to_issue_number: dict[str, int],
) -> str:
    """Issueの親・依存関係から自己修復時のbase branchを決定する。"""
    base_branch = "origin/main"
    if issue.parent and issue.parent.get("number") is not None:
        base_branch = f"parent/issue-{issue.parent['number']}"

    dependency_issue_numbers = _dependency_issue_numbers(
        issue,
        issue_to_subtask_id,
        subtask_id_to_issue_number,
    )
    for pr in open_prs:
        if any(
            dep_num in pr.closes_issue_numbers for dep_num in dependency_issue_numbers
        ):
            return pr.head_ref

    return base_branch


def _decide_missing_active_worktrees(
    run_state: RunState,
    in_progress_issues: list[IssueRecord],
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

    issue_to_subtask_id: dict[int, str] = {}
    for issue in in_progress_issues:
        raw_subtask_id = _extract_raw_subtask_id(issue)
        if raw_subtask_id is not None:
            issue_to_subtask_id[issue.number] = raw_subtask_id
    subtask_id_to_issue_number = {
        subtask_id: issue_number
        for issue_number, subtask_id in issue_to_subtask_id.items()
    }

    print(
        f"Self-healing: Found {len(missing_issues)} active issues missing from run_state.",
        file=sys.stderr,
    )

    try:
        open_prs = config.resolved_forge.list_open_prs()
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

        restored_base_branch = _restored_base_branch(
            issue,
            open_prs,
            issue_to_subtask_id,
            subtask_id_to_issue_number,
        )

        active_worktree = ActiveWorktree(
            issue_number=issue.number,
            branch=branch_name,
            worktree_path=str(worktree_path),
            pid=None,
            # Issue作成日時やPRメタデータはdispatch開始日時を表さない。信頼
            # できる開始時刻を復元できないTaskはtimeout判定を保留する。
            started_at=None,
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
    """decide層が算出した復元内容のみをrun_state.active_worktreesへ書き込む。"""
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
    in_progress_issues: list[IssueRecord],
    config: DispatcherConfig,
) -> bool:
    """in-progressなIssueからActiveWorktreeを復元する（decide+applyの薄いラッパー）。"""
    restorations = _decide_missing_active_worktrees(
        run_state, in_progress_issues, config
    )
    return _apply_restore_missing_active_worktrees(run_state, restorations)


def _warn_missing_physical_worktrees(run_state: RunState) -> None:
    """物理的な git worktree が存在しない場合に警告ログを出す（読み取り専用）。"""
    try:
        res = run_git(["worktree", "list", "--porcelain"], cwd=None, check=True)
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
    in_progress_issues: list[IssueRecord],
    config: DispatcherConfig,
) -> bool:
    """run_state.jsonが失われたり不整合が起きている場合に、GitHub API (in_progress_issues / open_prs)
    およびローカルの物理的な git worktree から RunState を自動復元する。
    """
    modified = _restore_missing_active_worktrees(run_state, in_progress_issues, config)
    _warn_missing_physical_worktrees(run_state)
    return modified
