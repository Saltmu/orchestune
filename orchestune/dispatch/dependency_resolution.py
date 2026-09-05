"""`depends_on`（本文のsubtask_id文字列）とネイティブ`blocked_by`
（Issue番号）を、Issue番号ベースで解決する共通resolver（#799）。

`subtask_id`はサブタスクIDの一意性が保証されるのが1つの分解計画（EPIC）内に
限られるため、`--parent-issue`を指定しないディスパッチサイクルのように複数
EPICのIssueが混在する母集団では、`{subtask_id: ...}`という単純な辞書は
別EPICの同名subtask_idを取り違える。本モジュールは、本文の`depends_on`文字列を
必ず「自タスクの親Issue」でスコープした上で解決し、ネイティブ依存は
Issue番号のまま（常に一意なので）扱うことで、この取り違いを構造的に防ぐ。

未解決（`unresolved`）は「依存なし」と同義ではない。呼び出し側は、
未解決の依存を1件でも持つタスクを、依存が満たされたタスクと同じに
扱ってはならない（起動・スタック・自動リベース・依存元ロック除外の
いずれからも除外する）。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from orchestune.models import Task

REASON_UNKNOWN_PARENT = "unknown-parent"
REASON_AMBIGUOUS = "ambiguous"
REASON_MISSING = "missing"


@dataclass(frozen=True)
class UnresolvedDependency:
    """1件の未解決依存の診断情報。"""

    raw: str
    reason: str
    candidates: tuple[int, ...] = ()


@dataclass(frozen=True)
class TaskDependencies:
    """1タスク分の依存解決結果。"""

    resolved: tuple[int, ...] = ()
    unresolved: tuple[UnresolvedDependency, ...] = ()

    @property
    def is_fully_resolved(self) -> bool:
        return not self.unresolved

    @property
    def is_empty(self) -> bool:
        """本文・ネイティブいずれの依存も宣言されていない。"""
        return not self.resolved and not self.unresolved


EMPTY_DEPENDENCIES = TaskDependencies()

# (親Issue番号, subtask_id) -> その名前を持つタスクのIssue番号集合。
_CandidateIndex = Mapping[tuple[int, str], set[int]]


def _candidate_index(tasks_by_issue: Mapping[int, Task]) -> _CandidateIndex:
    index: dict[tuple[int, str], set[int]] = defaultdict(set)
    for issue_number, task in tasks_by_issue.items():
        if task.subtask_id and task.parent_number is not None:
            index[(task.parent_number, task.subtask_id)].add(issue_number)
    return index


def _resolve_native(
    task: Task,
    tasks_by_issue: Mapping[int, Task],
    resolved: list[int],
    unresolved: list[UnresolvedDependency],
    seen: set[int],
) -> None:
    """ネイティブ`blocked_by`はIssue番号のまま扱うため、別EPICへの明示的な
    依存でも(候補集合内に存在する限り)常に一意に解決できる。候補集合
    （今サイクルで取得したIssue）に無い場合は、状態を確認できないので
    「依存なし」に倒さず未解決として保持する。
    """
    for dep_number in task.native_depends_on:
        if dep_number not in tasks_by_issue:
            unresolved.append(
                UnresolvedDependency(
                    raw=str(dep_number),
                    reason=REASON_MISSING,
                    candidates=(dep_number,),
                )
            )
            continue
        if dep_number not in seen:
            resolved.append(dep_number)
            seen.add(dep_number)


def _resolve_body(
    task: Task,
    index: _CandidateIndex,
    resolved: list[int],
    unresolved: list[UnresolvedDependency],
    seen: set[int],
) -> None:
    """本文の`depends_on`文字列は、必ず自タスクの親Issueでスコープした
    (親, subtask_id)の組で解決する。親が不明な場合は、共通スコープへ
    格上げせず全て未解決にする。
    """
    if task.parent_number is None:
        unresolved.extend(
            UnresolvedDependency(raw=raw, reason=REASON_UNKNOWN_PARENT)
            for raw in task.depends_on
        )
        return

    for raw in task.depends_on:
        candidates = index.get((task.parent_number, raw), set())
        if len(candidates) == 1:
            (dep_number,) = tuple(candidates)
            if dep_number not in seen:
                resolved.append(dep_number)
                seen.add(dep_number)
        elif not candidates:
            unresolved.append(UnresolvedDependency(raw=raw, reason=REASON_MISSING))
        else:
            unresolved.append(
                UnresolvedDependency(
                    raw=raw,
                    reason=REASON_AMBIGUOUS,
                    candidates=tuple(sorted(candidates)),
                )
            )


def resolve_task_dependencies(
    task: Task,
    tasks_by_issue: Mapping[int, Task],
    candidate_index: _CandidateIndex | None = None,
) -> TaskDependencies:
    """1タスク分の依存を解決する。

    `candidate_index`は`resolve_all_dependencies`が複数タスク分をまとめて
    解決する際、母集団全体で1回だけ構築したインデックスを使い回すための
    ものであり、単体で呼ぶ場合は省略してよい（`tasks_by_issue`から
    その場で構築する）。
    """
    index = (
        candidate_index
        if candidate_index is not None
        else _candidate_index(tasks_by_issue)
    )
    resolved: list[int] = []
    unresolved: list[UnresolvedDependency] = []
    seen: set[int] = set()

    # ネイティブと本文は独立に解決する: 曖昧な本文依存は、たまたま同名の
    # ネイティブ依存が別途存在しても解消されない（両者は別個の依存表明である）。
    _resolve_native(task, tasks_by_issue, resolved, unresolved, seen)
    _resolve_body(task, index, resolved, unresolved, seen)

    return TaskDependencies(resolved=tuple(resolved), unresolved=tuple(unresolved))


def describe_unresolved_dependency(dependency: UnresolvedDependency) -> str:
    """1件の未解決依存を、運用者向けの1行診断テキストへ整形する。

    「理由・参照元Issue（呼び出し側が付与）・依存文字列または番号・候補Issue
    番号（存在する場合）」のうち、依存文字列/番号と理由・候補番号を担う
    （#799受け入れ基準）。
    """
    if dependency.reason == REASON_AMBIGUOUS:
        candidates = ", ".join(f"#{number}" for number in dependency.candidates)
        return f"{dependency.raw} (ambiguous: {candidates})"
    if dependency.reason == REASON_MISSING and dependency.candidates:
        return f"{dependency.raw} (unresolved: #{dependency.candidates[0]})"
    if dependency.reason == REASON_MISSING:
        return f"{dependency.raw} (missing)"
    if dependency.reason == REASON_UNKNOWN_PARENT:
        return f"{dependency.raw} (unknown-parent)"
    return dependency.raw


def resolve_all_dependencies(
    tasks_by_issue: Mapping[int, Task],
) -> dict[int, TaskDependencies]:
    """`tasks_by_issue`が保持する全タスク分の依存解決を1回のインデックス構築で行う。"""
    index = _candidate_index(tasks_by_issue)
    return {
        issue_number: resolve_task_dependencies(task, tasks_by_issue, index)
        for issue_number, task in tasks_by_issue.items()
    }


__all__ = [
    "REASON_AMBIGUOUS",
    "REASON_MISSING",
    "REASON_UNKNOWN_PARENT",
    "EMPTY_DEPENDENCIES",
    "TaskDependencies",
    "UnresolvedDependency",
    "describe_unresolved_dependency",
    "resolve_all_dependencies",
    "resolve_task_dependencies",
]
