"""解決済み依存を「観測された実効状態」だけで4状態へ分類する純粋な
Lifecycle Assessment（#867）。

`dependency_resolution`がIdentity（どのIssue番号が依存か）を担うのに対し、
本モジュールはLifecycle（その依存が今どの状態か）だけを担う。分類は
COMPLETED > CHANGES_REQUESTED > CI_PASSED_UNMERGED > WAITINGの優先順位で
一度だけ行い、`CI_PASSED_UNMERGED`は「実効完了していない依存のCIが通って
いる」という観測事実のみを表す。stack可否・単一依存条件・孫依存・ブランチ・
quota・promotion・rebaseといった用途固有の判断は後続policyの責務であり、
ここには混入させない。

実効完了の意味（DONE、NOT_NEEDED、同一サイクル完了、検証済み先行マージ等）は
`DependencyStateView`の実装側が所有する。本モジュールはラベルやRunStateから
状態を再構成せず、I/O・ログ・キャッシュも持たない。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from orchestune.dispatch.dependency_resolution import (
    TaskDependencies,
    UnresolvedDependency,
)


class DependencyState(Enum):
    """1件の解決済み依存が取りうるライフサイクル状態。"""

    COMPLETED = "completed"
    CHANGES_REQUESTED = "changes-requested"
    CI_PASSED_UNMERGED = "ci-passed-unmerged"
    WAITING = "waiting"


class DependencyStateView(Protocol):
    """分類に必要な最小限のread-onlyな実効状態view。

    構造的部分型として使うため、実装側に継承も`@runtime_checkable`も要求しない。
    1回の`assess_dependencies`呼び出しの間は一貫した値を返すことが契約。
    """

    def is_effectively_done(self, issue_number: int) -> bool: ...

    def has_changes_requested(self, issue_number: int) -> bool: ...

    def is_ci_passed(self, issue_number: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class AssessedDependency:
    """1件の解決済み依存と、その分類結果の組。"""

    issue_number: int
    state: DependencyState


@dataclass(frozen=True, slots=True)
class DependencyAssessment:
    """1タスク分のライフサイクル分類結果。

    未解決依存は`WAITING`へ丸めず`unresolved`として保持する。呼び出し側は
    未解決を1件でも持つタスクを、依存が満たされたタスクと同じに扱ってはならない
    （この不変条件は`dependency_resolution`から引き継ぐ）。
    """

    resolved: tuple[AssessedDependency, ...] = ()
    unresolved: tuple[UnresolvedDependency, ...] = ()


def _classify(issue_number: int, state_view: DependencyStateView) -> DependencyState:
    """優先順位に従って短絡評価し、分類が決まった後の下位照会は行わない。"""
    if state_view.is_effectively_done(issue_number):
        return DependencyState.COMPLETED
    if state_view.has_changes_requested(issue_number):
        return DependencyState.CHANGES_REQUESTED
    if state_view.is_ci_passed(issue_number):
        return DependencyState.CI_PASSED_UNMERGED
    return DependencyState.WAITING


def _normalize_unresolved(
    unresolved: tuple[UnresolvedDependency, ...],
) -> tuple[UnresolvedDependency, ...]:
    """未解決診断を無損失のまま決定論的に並べ直す。

    候補番号はIssue番号昇順、診断自体は`(reason, raw, candidates)`の辞書式昇順。
    `raw`は文字列として比較し、数値変換・strip・大小文字変換を行わない。
    診断の件数も候補番号の重複数も落とさない。
    """
    normalized = [
        UnresolvedDependency(
            raw=diagnostic.raw,
            reason=diagnostic.reason,
            candidates=tuple(sorted(diagnostic.candidates)),
        )
        for diagnostic in unresolved
    ]
    normalized.sort(key=lambda d: (d.reason, d.raw, d.candidates))
    return tuple(normalized)


def assess_dependencies(
    dependencies: TaskDependencies,
    state_view: DependencyStateView,
) -> DependencyAssessment:
    """依存解決結果と実効状態viewから、依存ごとの状態を一度だけ分類する。

    解決済みIssue番号は重複排除してIssue番号昇順に1件ずつ分類する。未解決診断は
    viewへ問い合わせない（候補番号は「解決済み」ではないため）。同じ番号が
    resolvedにもある場合、その番号はresolved由来としてだけ分類し、未解決診断は
    そのまま残す。viewが送出した例外は握りつぶさずそのまま伝播させる。
    """
    return DependencyAssessment(
        resolved=tuple(
            AssessedDependency(
                issue_number=issue_number,
                state=_classify(issue_number, state_view),
            )
            for issue_number in sorted(set(dependencies.resolved))
        ),
        unresolved=_normalize_unresolved(dependencies.unresolved),
    )


__all__ = [
    "AssessedDependency",
    "DependencyAssessment",
    "DependencyState",
    "DependencyStateView",
    "assess_dependencies",
]
