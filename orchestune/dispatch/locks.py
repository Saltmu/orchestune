"""外部ロック（Gitリモートブランチ・PRとの衝突）判定とfootprint逸脱検知。"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from orchestune.dispatch.scoring import Task
from orchestune.infra.git_cli import resolve_local_or_remote_branch, run_git
from orchestune.labels import StatusLabel
from orchestune.models import PrRecord
from orchestune.pr_link_notice import pr_matches_issue

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


KIND_BRANCH = "branch"
KIND_PR = "pr"
KIND_BRANCH_DIFF_UNKNOWN = "branch-diff-unknown"
KIND_PR_FILES_TRUNCATED = "pr-files-truncated"


@dataclass(frozen=True)
class ExternalLockConflict:
    """#787: 外部ロック1件分の理由。運用者が「なぜ起動しないのか」を追える最小単位。

    `files`は`branch`/`pr`種別でのみ埋まる。差分を取得できなかったブランチや
    changed filesが打ち切られたPRはfail closedでロックするため衝突ファイルを
    特定できず、種別だけで理由を表す。"""

    kind: str
    source: str
    files: tuple[str, ...] = ()


@dataclass
class ExternalLockScanResult:
    to_lock: list[Task]
    to_unlock: list[Task]
    # #787: 新規ロック(to_lock)だけでなく「前サイクルから継続してロック中の
    # タスク」も収録する。継続ロックはto_lock/to_unlockのどちらにも現れず、
    # 理由を引ける場所が他に無いため（#695の実例）。
    conflicts: dict[int, tuple[ExternalLockConflict, ...]] = field(default_factory=dict)


def _collect_branch_footprints(
    remote_branches: Iterable[tuple[str, tuple[str, ...] | None]],
    active_set: set[str],
) -> tuple[list[tuple[str, set[str]]], list[str]]:
    branch_footprints: list[tuple[str, set[str]]] = []
    unknown_branches: list[str] = []
    for branch, changed_files in list(remote_branches):
        if branch in active_set:
            continue
        if changed_files is None:
            unknown_branches.append(branch)
        else:
            branch_footprints.append((branch, set(changed_files)))
    return sorted(branch_footprints), sorted(unknown_branches)


def _external_prs(
    task: Task, prs: list[PrRecord], active_set: set[str]
) -> list[PrRecord]:
    return sorted(
        (
            pr
            for pr in prs
            if pr.head_ref not in active_set
            and not pr_matches_issue(pr, task.issue_number, task.subtask_id)
        ),
        key=lambda pr: pr.number,
    )


def _overlapping_files(
    task_footprint: set[str], other_files: Iterable[str]
) -> tuple[str, ...]:
    overlap = task_footprint & {
        path for path in other_files if not _is_hotspot(path)
    }
    return tuple(sorted(overlap))


def _collect_task_conflicts(
    task: Task,
    active_set: set[str],
    prs: list[PrRecord],
    branch_footprints: list[tuple[str, set[str]]],
    unknown_branches: list[str],
) -> tuple[ExternalLockConflict, ...]:
    """タスクが外部ロックされる理由をすべて集める。空タプルなら衝突なし。

    並びは決定的（ブランチ名昇順→PR番号昇順→fail closed種別）。Issueコメントの
    dedupは本文の完全一致で行うため、同じ衝突集合からは毎回同じ本文が要る。"""
    task_footprint = {path for path in task.footprint if not _is_hotspot(path)}
    if not task_footprint:
        return ()
    external_prs = _external_prs(task, prs, active_set)
    conflicts = [
        ExternalLockConflict(KIND_BRANCH, branch, files)
        for branch, footprint in branch_footprints
        if (files := _overlapping_files(task_footprint, footprint))
    ]
    conflicts.extend(
        ExternalLockConflict(KIND_PR, f"#{pr.number}", files)
        for pr in external_prs
        if (files := _overlapping_files(task_footprint, pr.changed_files))
    )
    conflicts.extend(
        ExternalLockConflict(KIND_BRANCH_DIFF_UNKNOWN, branch)
        for branch in unknown_branches
    )
    conflicts.extend(
        ExternalLockConflict(KIND_PR_FILES_TRUNCATED, f"#{pr.number}")
        for pr in external_prs
        if pr.is_files_truncated
    )
    return tuple(conflicts)


def scan_external_locks(
    queued_tasks: list[Task],
    remote_branches: Iterable[tuple[str, tuple[str, ...] | None]],
    prs: list[PrRecord],
    active_branches: Iterable[str],
) -> ExternalLockScanResult:
    active_set = set(active_branches)
    branch_footprints, unknown_branches = _collect_branch_footprints(
        remote_branches, active_set
    )
    to_lock: list[Task] = []
    to_unlock: list[Task] = []
    conflicts_by_issue: dict[int, tuple[ExternalLockConflict, ...]] = {}
    for task in queued_tasks:
        currently_locked = StatusLabel.EXTERNAL_LOCK in task.status_labels
        if (
            StatusLabel.DONE in task.status_labels
            or StatusLabel.NOT_NEEDED in task.status_labels
        ):
            if currently_locked:
                to_unlock.append(task)
            continue

        conflicts = _collect_task_conflicts(
            task, active_set, prs, branch_footprints, unknown_branches
        )
        if conflicts:
            conflicts_by_issue[task.issue_number] = conflicts
            if not currently_locked:
                to_lock.append(task)
        elif currently_locked:
            to_unlock.append(task)

    return ExternalLockScanResult(
        to_lock=to_lock, to_unlock=to_unlock, conflicts=conflicts_by_issue
    )


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
