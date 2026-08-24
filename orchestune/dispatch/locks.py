"""外部ロック（Gitリモートブランチ・PRとの衝突）判定とfootprint逸脱検知。"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from orchestune.dispatch.scoring import Task
from orchestune.infra.git_cli import resolve_local_or_remote_branch, run_git
from orchestune.models import PrRecord

_HOTSPOT_PATTERNS = (
    re.compile(
        r"(^|/)(package\.json|poetry\.lock|package-lock\.json|yarn\.lock|pnpm-lock\.yaml)$"
    ),
    re.compile(r"(^|/)src/routes\.py$"),
    re.compile(r"(^|/)src/routes/.*"),
)


def _is_hotspot(path: str) -> bool:
    """ほぼ全タスクが触れうる「ホットスポットファイル」かどうかを判定する。

    footprint逸脱検知(check_footprint_deviation)・外部ロック判定
    (scan_external_locks)の双方で、これらのファイルだけの重複・変更は
    無視する(#200, #209)。"""
    return any(pattern.search(path) for pattern in _HOTSPOT_PATTERNS)


@dataclass
class ExternalLockScanResult:
    to_lock: list[Task]
    to_unlock: list[Task]


def _collect_branch_footprints(
    remote_branches: Iterable[tuple[str, tuple[str, ...] | None]],
    active_set: set[str],
) -> tuple[list[set[str]], bool]:
    branch_footprints: list[set[str]] = []
    has_unknown = False
    for branch, changed_files in list(remote_branches):
        if branch in active_set:
            continue
        if changed_files is None:
            has_unknown = True
        else:
            branch_footprints.append(set(changed_files))
    return branch_footprints, has_unknown


def _check_task_overlap(
    task: Task,
    active_set: set[str],
    prs: list[PrRecord],
    branch_footprints: list[set[str]],
    has_unknown_branch_footprint: bool,
) -> bool:
    pr_footprints = [
        set(pr.changed_files)
        for pr in prs
        if pr.head_ref not in active_set
        and task.issue_number not in pr.closes_issue_numbers
    ]
    has_truncated_pr = any(
        pr.is_files_truncated
        for pr in prs
        if pr.head_ref not in active_set
        and task.issue_number not in pr.closes_issue_numbers
    )
    task_footprint = {path for path in task.footprint if not _is_hotspot(path)}
    return any(
        task_footprint & {path for path in footprint if not _is_hotspot(path)}
        for footprint in [*branch_footprints, *pr_footprints]
    ) or ((has_unknown_branch_footprint or has_truncated_pr) and bool(task_footprint))


def scan_external_locks(
    queued_tasks: list[Task],
    remote_branches: Iterable[tuple[str, tuple[str, ...] | None]],
    prs: list[PrRecord],
    active_branches: Iterable[str],
) -> ExternalLockScanResult:
    active_set = set(active_branches)
    branch_footprints, has_unknown = _collect_branch_footprints(
        remote_branches, active_set
    )
    to_lock: list[Task] = []
    to_unlock: list[Task] = []
    for task in queued_tasks:
        currently_locked = "status:external-lock" in task.status_labels
        if (
            "status:done" in task.status_labels
            or "status:not-needed" in task.status_labels
        ):
            if currently_locked:
                to_unlock.append(task)
            continue

        overlaps = _check_task_overlap(
            task, active_set, prs, branch_footprints, has_unknown
        )
        if overlaps and not currently_locked:
            to_lock.append(task)
        elif not overlaps and currently_locked:
            to_unlock.append(task)

    return ExternalLockScanResult(to_lock=to_lock, to_unlock=to_unlock)


def _parse_numstat_line(
    line: str, declared: set[str], min_changed_lines: int
) -> str | None:
    line = line.strip()
    if not line:
        return None
    parts = line.split("\t")
    if len(parts) != 3:
        return None
    added_str, deleted_str, path = parts
    if path in declared:
        return None
    if _is_hotspot(path):
        print(
            f"Warning: Footprint deviation detected on hotspot file '{path}', "
            "skipping Conflict Graph recompute.",
            file=sys.stderr,
        )
        return None
    if added_str == "-" or deleted_str == "-":
        changed_lines = min_changed_lines + 1
    else:
        changed_lines = int(added_str) + int(deleted_str)
    return path if changed_lines > min_changed_lines else None


def check_footprint_deviation(
    worktree_path: str | Path,
    declared_footprint: tuple[str, ...],
    base: str = "origin/main",
    min_changed_lines: int = 0,
) -> list[str] | None:
    resolved_base = resolve_local_or_remote_branch(
        worktree_path, base, prefer_remote=base.startswith("parent/")
    )
    try:
        result = run_git(
            ["diff", "--numstat", f"{resolved_base}...HEAD"],
            cwd=worktree_path,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None

    declared = set(declared_footprint)
    deviated: list[str] = []
    for line in result.stdout.splitlines():
        path = _parse_numstat_line(line, declared, min_changed_lines)
        if path is not None:
            deviated.append(path)
    return deviated


def _strip_remote_prefix(branch: str, remote: str = "origin") -> str:
    """#194: `git branch -r`由来のリモート名プレフィックスを剥がし、
    PRのheadRefName・ディスパッチャ自身のブランチ名と同じ名前空間に正規化する。"""
    prefix = f"{remote}/"
    return branch[len(prefix) :] if branch.startswith(prefix) else branch
