"""外部ロック（Gitリモートブランチ・PRとの衝突）判定とfootprint逸脱検知。"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from orchestune.branch_naming import build_task_branch_name
from orchestune.dispatch.dependency_resolution import (
    EMPTY_DEPENDENCIES,
    TaskDependencies,
    resolve_all_dependencies,
)
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


def _direct_dependency_canonical_branches(
    task: Task,
    tasks_by_issue: dict[int, Task],
    dependency_resolution: dict[int, TaskDependencies],
) -> frozenset[str]:
    """taskの直接の`depends_on`が指す依存元タスクの正規ブランチ名の集合。

    #799: 依存元の同定は`dependency_resolution`が親Issueでスコープして
    解決したIssue番号で行う。未解決の依存（親不明・曖昧・依存先欠落）は
    どの依存元も指すか分からないため、ここでは含めない
    （＝そのぶん通常の衝突判定からは除外されず、fail closedのまま残る）。

    #796: スタッキング起動(`orchestune.dispatch.launch._get_stack_eligible_tasks`)
    は依存元ブランチをbaseに積むため、依存元のPR・ブランチとの重複は
    「Orchestune管理外の衝突」ではない。`_is_task_stack_eligible`はスタック時に
    baseへ入るのが直接依存1本だけであることを前提にしているため、除外もそれに
    揃えて直接依存に限る（祖先依存はここでは解決しない）。

    Codexレビュー対応(PR#797 P2): `orchestune.branch_naming.branch_matches_task`
    は任意のprefix（`fix/issue-N-x`等）を受理する設計だが、スタッキング
    起動や`_build_pr_mappings`の`branch_by_issue_number`が実際に使うのは
    `build_task_branch_name`が生成する既定prefixのブランチそのもの。
    見た目が同じ形状でも別prefixのブランチ・PRはスタッキングの取り込み対象
    ではないため、比較は既定prefixの完全一致に限定する。

    `depends_on`が指すsubtask_idが候補集合に見つからない（解決不能な依存）場合は
    無視する。fail closedのまま、従来通り衝突判定に残る。

    Codexレビュー対応(PR#797 P2, Finding 4): 依存元が既に`status:done`/
    `status:not-needed`に到達している場合は除外しない。`_is_task_stack_eligible`
    はdoneな依存を`stackable_deps`に加えない（＝スタックのbaseとして選ばれない）
    ため、依存元がdoneになった時点でこのタスクは（`status:queued`へ昇格した
    上で）通常のparent/mainからそのまま起動され得る。このときの依存元PR・
    ブランチとの重複は、Integratorのマージが未完了で実際にはbaseへ取り込まれて
    いない変更を意味しうる、正真正銘の外部衝突であり除外してはならない。

    Codexレビュー対応(PR#797 P2, Finding 5): タスク自身が`status:blocked`で
    ない場合は除外しない。`_get_stack_eligible_tasks`がbaseを割り当てるのは
    `issues.blocked`のみで、スタッキングはタスクが`status:blocked`の間しか
    起こらない。`status:queued`のタスクは通常、依存が解決済みだからqueuedに
    昇格しているはずだが、`orchestune.consistency.invariants.status`の
    `QUEUED_WITH_UNRESOLVED_DEPENDENCIES`が検知・修復する異常系として、
    依存未解決のまま`status:queued`になり得る（repair実行前の一時的な状態）。
    このとき`_filter_queued_candidates`はfootprintや`depends_on`を見ずに
    候補として扱うため、除外を適用すると依存元の変更が実際には入っていない
    baseから起動されてしまう。
    """
    if StatusLabel.BLOCKED not in task.status_labels:
        return frozenset()
    deps = dependency_resolution.get(task.issue_number, EMPTY_DEPENDENCIES)
    return frozenset(
        build_task_branch_name(dep_task.issue_number, dep_task.subtask_id)
        for dep_issue in deps.resolved
        if (dep_task := tasks_by_issue.get(dep_issue)) is not None
        and StatusLabel.DONE not in dep_task.status_labels
        and StatusLabel.NOT_NEEDED not in dep_task.status_labels
    )


def _is_dependency_pr(pr: PrRecord, dependency_branches: frozenset[str]) -> bool:
    """PRが依存元タスクの正規ブランチそのものだと確実に同定できる場合のみ`True`。

    Codexレビュー対応(PR#797): `pr_matches_issue`はPRのタイトル・本文中の
    `#N`言及だけでも一致するため、依存元のIssue番号を(意図的か偶然かを問わず)
    言及しているだけの無関係なPR――フォーク由来を含む――まで、その言及だけで
    ロック判定から除外できてしまう。除外は依存元の正規ブランチ名との完全一致
    (`_direct_dependency_canonical_branches`、`build_task_branch_name`)と、
    `is_cross_repository is False`（同一リポジトリ由来だと確認できる場合のみ。
    `None`＝不明もfail closedで除外しない）に限定する。
    """
    return pr.is_cross_repository is False and pr.head_ref in dependency_branches


def _is_dependency_branch(branch: str, dependency_branches: frozenset[str]) -> bool:
    return branch in dependency_branches


def _external_prs(
    task: Task,
    prs: list[PrRecord],
    active_set: set[str],
    dependency_branches: frozenset[str],
) -> list[PrRecord]:
    return sorted(
        (
            pr
            for pr in prs
            if pr.head_ref not in active_set
            and not pr_matches_issue(pr, task.issue_number, task.subtask_id)
            and not _is_dependency_pr(pr, dependency_branches)
        ),
        key=lambda pr: pr.number,
    )


def _overlapping_files(
    task_footprint: set[str], other_files: Iterable[str]
) -> tuple[str, ...]:
    overlap = task_footprint & {path for path in other_files if not _is_hotspot(path)}
    return tuple(sorted(overlap))


def _collect_task_conflicts(
    task: Task,
    active_set: set[str],
    prs: list[PrRecord],
    branch_footprints: list[tuple[str, set[str]]],
    unknown_branches: list[str],
    dependency_branches: frozenset[str],
) -> tuple[ExternalLockConflict, ...]:
    """タスクが外部ロックされる理由をすべて集める。空タプルなら衝突なし。

    並びは決定的（ブランチ名昇順→PR番号昇順→fail closed種別）。Issueコメントの
    dedupは本文の完全一致で行うため、同じ衝突集合からは毎回同じ本文が要る。

    `dependency_branches`（#796）に一致するPR・ブランチは、`branch-diff-unknown`
    /`pr-files-truncated`によるfail closedも含めて衝突の母集団から除外する。
    """
    task_footprint = {path for path in task.footprint if not _is_hotspot(path)}
    if not task_footprint:
        return ()
    external_prs = _external_prs(task, prs, active_set, dependency_branches)
    conflicts = [
        ExternalLockConflict(KIND_BRANCH, branch, files)
        for branch, footprint in branch_footprints
        if not _is_dependency_branch(branch, dependency_branches)
        and (files := _overlapping_files(task_footprint, footprint))
    ]
    conflicts.extend(
        ExternalLockConflict(KIND_PR, f"#{pr.number}", files)
        for pr in external_prs
        if (files := _overlapping_files(task_footprint, pr.changed_files))
    )
    conflicts.extend(
        ExternalLockConflict(KIND_BRANCH_DIFF_UNKNOWN, branch)
        for branch in unknown_branches
        if not _is_dependency_branch(branch, dependency_branches)
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
    # #796/#799: `depends_on`の解決に使う。呼び出し元(`_decide_external_lock_sync`)は
    # 実際には全タスク（queued/blocked/in-progress/done/not-needed）を渡すため、
    # 依存元タスクがまだ完了していなくてもここで引ける。依存元の同定は
    # subtask_idの文字列一致ではなく、親Issueでスコープした
    # `dependency_resolution`（Issue番号）で行う（#799: 別EPICの同名
    # subtask_idを取り違えないため）。
    tasks_by_issue = {task.issue_number: task for task in queued_tasks}
    dependency_resolution = resolve_all_dependencies(tasks_by_issue)
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

        dependency_branches = _direct_dependency_canonical_branches(
            task, tasks_by_issue, dependency_resolution
        )
        conflicts = _collect_task_conflicts(
            task,
            active_set,
            prs,
            branch_footprints,
            unknown_branches,
            dependency_branches,
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


_CONFLICT_KIND_LABELS = {
    KIND_BRANCH: "リモートブランチ",
    KIND_PR: "PR",
}

NOTICE_KIND_EXTERNAL_LOCK = "external-lock"

_RELEASE_NOTICE = (
    "🔓 外部ロックを解除しました（`status:external-lock` を外しました）。\n\n"
    "衝突していた変更が解消されたため、次のディスパッチサイクルから"
    "起動候補に戻ります。"
)


def describe_conflict(conflict: ExternalLockConflict) -> str:
    """1行に収まる衝突の要約。

    サイクルサマリーのテキスト経路（stderr）にそのまま載るため、ASCIIだけで
    組む。`local-ci.ps1`を使うWindowsのコンソール（cp932）で壊れないため。
    """
    if conflict.kind == KIND_BRANCH_DIFF_UNKNOWN:
        return f"{conflict.source} (diff unavailable)"
    if conflict.kind == KIND_PR_FILES_TRUNCATED:
        return f"{conflict.source} (file list truncated)"
    return f"{conflict.source} [{', '.join(conflict.files)}]"


def _fail_closed_lines(conflicts: tuple[ExternalLockConflict, ...]) -> list[str]:
    """fail closed由来のロックは件数へ丸める。

    衝突ファイルを特定できない以上ブランチ名を並べても手掛かりにならず、
    一時的な取得失敗のたびに本文が変わるとIssueコメントが連投になる
    （通知の重複判定は本文の完全一致で行う）。"""
    lines = []
    unknown = sum(1 for c in conflicts if c.kind == KIND_BRANCH_DIFF_UNKNOWN)
    truncated = sum(1 for c in conflicts if c.kind == KIND_PR_FILES_TRUNCATED)
    if unknown:
        lines.append(
            f"- 差分を取得できないリモートブランチが {unknown} 件あるため、"
            "保守的にロックしています。"
        )
    if truncated:
        lines.append(
            f"- 変更ファイル一覧を完全に取得できないPRが {truncated} 件あるため、"
            "保守的にロックしています。"
        )
    return lines


def render_external_lock_notice(conflicts: tuple[ExternalLockConflict, ...]) -> str:
    """ロック理由をIssueコメント本文（Markdown）へ整形する。"""
    lines = [
        "🔒 このタスクは外部ロック中です（`status:external-lock`）。",
        "",
        "他の変更とfootprintが重なっているため、衝突が解消されるまで自動起動しません。",
        "",
    ]
    identified = [c for c in conflicts if c.kind in _CONFLICT_KIND_LABELS]
    if identified:
        lines.extend(
            [
                "| 衝突相手 | 種別 | 衝突ファイル |",
                "| --- | --- | --- |",
            ]
        )
        lines.extend(
            f"| `{conflict.source}` | {_CONFLICT_KIND_LABELS[conflict.kind]} | "
            f"{', '.join(f'`{path}`' for path in conflict.files)} |"
            for conflict in identified
        )
        lines.append("")
    lines.extend(_fail_closed_lines(conflicts))
    if not identified:
        lines.append("")
    lines.append(
        "衝突相手がマージ・削除されると、次のディスパッチサイクルで自動的に解除されます。"
    )
    return "\n".join(lines)


def render_external_lock_release_notice() -> str:
    return _RELEASE_NOTICE


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
