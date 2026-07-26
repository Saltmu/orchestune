"""外部ロック（Gitリモートブランチ・PRとの衝突）判定とfootprint逸脱検知。"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from orchestune.dispatch_scoring import Task
from orchestune.github import PrRecord, resolve_local_or_remote_branch

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
    remote_branches: Iterable[tuple[str, tuple[str, ...] | None]],
    prs: list[PrRecord],
    active_branches: Iterable[str],
) -> ExternalLockScanResult:
    """#239: ブランチ名がAIセッションの指示通りにならないケースに備え、
    タスクごとに「そのタスク自身のIssueをclosesするPR」を自己PRとして除外する
    （どのPRが自己PRかはタスクごとに異なるため、タスク単位で判定する）。

    #245: `remote_branches`のfootprintが`None`（差分取得不能）のブランチが
    1件でもある場合はfail closedとし、非hotspotなfootprintを持つ全タスクを
    「衝突あり」として扱う（既存lockは維持し、未lockタスクはlockする）。
    footprintが空またはhotspotのみのタスクは、どのブランチとも衝突し得ない
    ため従来通り対象外。"""
    active_set = set(active_branches)
    # #261レビュー対応: `remote_branches`はIterable契約のため、generator等の
    # 単回走査イテレータが渡されると2回目のループで要素が既に消費されており
    # `has_unknown_branch_footprint`が常にFalseになる（fail-closed判定の無効化）。
    # 先に具体化し、1回の走査で両方を構築する。
    remote_branch_list = list(remote_branches)
    branch_footprints: list[set[str]] = []
    has_unknown_branch_footprint = False
    for branch, changed_files in remote_branch_list:
        if branch in active_set:
            continue
        if changed_files is None:
            has_unknown_branch_footprint = True
        else:
            branch_footprints.append(set(changed_files))

    to_lock: list[Task] = []
    to_unlock: list[Task] = []
    for task in queued_tasks:
        currently_locked = "status:external-lock" in task.status_labels
        # #261 Codexレビュー指摘: status:doneと同様、status:not-neededも
        # 既に解決済み（対応不要と判定済み）で再ディスパッチされないため、
        # fail-closed判定を含む通常のlock対象から除外する。
        if (
            "status:done" in task.status_labels
            or "status:not-needed" in task.status_labels
        ):
            if currently_locked:
                to_unlock.append(task)
            continue

        pr_footprints = [
            set(pr.changed_files)
            for pr in prs
            if pr.head_ref not in active_set
            and task.issue_number not in pr.closes_issue_numbers
        ]
        # #250: changed filesが完全取得できずtruncated状態のPRが存在する場合、
        # 他タスクのfootprintとの重複可能性を排除できないためfail closedに判定する。
        has_truncated_pr = any(
            pr.is_files_truncated
            for pr in prs
            if pr.head_ref not in active_set
            and task.issue_number not in pr.closes_issue_numbers
        )

        # #209: poetry.lock等のホットスポットファイルだけの重複は、実質的な
        # 直列化(外部ロック)を引き起こさない(check_footprint_deviationと同じ
        # 除外パターンを適用する)。
        task_footprint = {path for path in task.footprint if not _is_hotspot(path)}
        overlaps = any(
            task_footprint & {path for path in footprint if not _is_hotspot(path)}
            for footprint in [*branch_footprints, *pr_footprints]
        ) or (
            (has_unknown_branch_footprint or has_truncated_pr) and bool(task_footprint)
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
) -> list[str] | None:
    """宣言footprint外のファイル変更を検知する。

    #200: ライブロック（チャーン）防止のため、`min_changed_lines`以下の
    変更行数（追加+削除）しかない微小な逸脱はバッファとして無視する。
    バイナリファイル（`git diff --numstat`が行数の代わりに`-`を返す）は
    行数で測れないため、バッファに関わらず常に逸脱として報告する。
    """
    # base がローカルに存在しないが、リモート追跡ブランチとして存在する場合はそちらを使用する
    resolved_base = resolve_local_or_remote_branch(
        worktree_path, base, prefer_remote=base.startswith("parent/")
    )

    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "diff",
                "--numstat",
                f"{resolved_base}...HEAD",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None

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
