"""外部ロック（Gitリモートブランチ・PRとの衝突）判定とfootprint逸脱検知。"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from orchestune.dispatch_scoring import Task
from orchestune.github import PrRecord

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


def scan_external_locks(
    queued_tasks: list[Task],
    remote_branches: Iterable[tuple[str, tuple[str, ...]]],
    prs: list[PrRecord],
    active_branches: Iterable[str],
) -> ExternalLockScanResult:
    """#239: ブランチ名がAIセッションの指示通りにならないケースに備え、
    タスクごとに「そのタスク自身のIssueをclosesするPR」を自己PRとして除外する
    （どのPRが自己PRかはタスクごとに異なるため、タスク単位で判定する）。"""
    active_set = set(active_branches)
    branch_footprints = [
        set(changed_files)
        for branch, changed_files in remote_branches
        if branch not in active_set
    ]

    to_lock: list[Task] = []
    to_unlock: list[Task] = []
    for task in queued_tasks:
        currently_locked = "status:external-lock" in task.status_labels
        if "status:done" in task.status_labels:
            if currently_locked:
                to_unlock.append(task)
            continue

        pr_footprints = [
            set(pr.changed_files)
            for pr in prs
            if pr.head_ref not in active_set
            and task.issue_number not in pr.closes_issue_numbers
        ]
        # #209: poetry.lock等のホットスポットファイルだけの重複は、実質的な
        # 直列化(外部ロック)を引き起こさない(check_footprint_deviationと同じ
        # 除外パターンを適用する)。
        task_footprint = {path for path in task.footprint if not _is_hotspot(path)}
        overlaps = any(
            task_footprint & {path for path in footprint if not _is_hotspot(path)}
            for footprint in [*branch_footprints, *pr_footprints]
        )
        if overlaps and not currently_locked:
            to_lock.append(task)
        elif not overlaps and currently_locked:
            to_unlock.append(task)

    return ExternalLockScanResult(to_lock=to_lock, to_unlock=to_unlock)


def check_footprint_deviation(
    worktree_path: str | Path,
    declared_footprint: tuple[str, ...],
    base: str = "origin/main",
    min_changed_lines: int = 0,
) -> list[str]:
    """宣言footprint外のファイル変更を検知する。

    #200: ライブロック（チャーン）防止のため、`min_changed_lines`以下の
    変更行数（追加+削除）しかない微小な逸脱はバッファとして無視する。
    バイナリファイル（`git diff --numstat`が行数の代わりに`-`を返す）は
    行数で測れないため、バッファに関わらず常に逸脱として報告する。
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "diff", "--numstat", f"{base}...HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return []

    declared = set(declared_footprint)
    deviated: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added_str, deleted_str, path = parts
        if path in declared:
            continue

        # ホットスポットファイルは逸脱チェックから除外する
        if _is_hotspot(path):
            print(
                f"Warning: Footprint deviation detected on hotspot file '{path}', skipping DAG recompute.",
                file=sys.stderr,
            )
            continue
        if added_str == "-" or deleted_str == "-":
            changed_lines = min_changed_lines + 1
        else:
            changed_lines = int(added_str) + int(deleted_str)
        if changed_lines > min_changed_lines:
            deviated.append(path)
    return deviated


def _strip_remote_prefix(branch: str, remote: str = "origin") -> str:
    """#194: `git branch -r`由来のリモート名プレフィックスを剥がし、
    PRのheadRefName・ディスパッチャ自身のブランチ名と同じ名前空間に正規化する。"""
    prefix = f"{remote}/"
    return branch[len(prefix) :] if branch.startswith(prefix) else branch
