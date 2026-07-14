"""未確立の共有拡張ポイント（shared-contract）に対する所有権未確定の検出。

`dag_similarity.py` の重複検出は宣言済みfootprint/symbolsの文字列一致（または
その加重コサイン類似度）にのみ依存するため、複数のサブタスクがまだ存在しない
共有ファイル（フォーマットレジストリ、CLI配線モジュール、依存関係マニフェスト等）
を、それぞれ異なる想定パスで触れようとしている場合には重複を検出できない。
本モジュールは、そうした「典型的な共有拡張ポイントのカテゴリ」に複数のサブタスクが
明示的・暗黙的ないずれの依存関係も無しに触れている場合に警告を生成する（#175）。

`dispatch_locks.py` の `_HOTSPOT_PATTERNS` はディスパッチ実行時のチャーン抑制
（既知の頻出変更ファイルを無視する）が目的であり、意図が異なるためパターンは
共有しない。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from orchestune.dag_models import DagEdge, SubTask

_SHARED_CONTRACT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "registry",
        re.compile(r"(^|/)[\w.-]*regist(?:ry|ration)[\w.-]*\.\w+$", re.IGNORECASE),
    ),
    ("cli-wiring", re.compile(r"(^|/)(cli|__main__|main)\.\w+$")),
    ("public-api", re.compile(r"(^|/)(__init__\.py|index\.(ts|js|tsx|jsx))$")),
    (
        "dependency-manifest",
        re.compile(
            r"(^|/)(pyproject\.toml|package\.json|poetry\.lock|"
            r"package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.toml|go\.mod)$"
        ),
    ),
)


def _categorize(path: str) -> str | None:
    for category, pattern in _SHARED_CONTRACT_PATTERNS:
        if pattern.search(path):
            return category
    return None


class _UnionFind:
    def __init__(self, ids: Iterable[str]) -> None:
        self._parent = {id_: id_ for id_ in ids}

    def find(self, id_: str) -> str:
        while self._parent[id_] != id_:
            self._parent[id_] = self._parent[self._parent[id_]]
            id_ = self._parent[id_]
        return id_

    def union(self, a: str, b: str) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self._parent[root_a] = root_b


def find_unowned_shared_contract_hotspots(
    subtasks: list[SubTask],
    edges: list[DagEdge],
) -> list[str]:
    """所有者不明の共有拡張ポイントに対する警告メッセージ一覧を返す。

    同一カテゴリ（registry/cli-wiring/public-api/dependency-manifest）の
    footprintに複数のサブタスクが触れているにもかかわらず、それらが明示的
    （depends_on）・暗黙的（similarity）いずれのDAGエッジでも接続されていない
    場合に1件ずつ警告を生成する。ブロッキングエラーではなく、プラン修正を促す
    ための警告に留める。
    """
    union_find = _UnionFind(subtask.id for subtask in subtasks)
    for edge in edges:
        union_find.union(edge.source, edge.target)

    touches: dict[str, list[tuple[str, str]]] = {}
    for subtask in subtasks:
        seen_categories: set[str] = set()
        for path in subtask.footprint:
            category = _categorize(path)
            if category is None or category in seen_categories:
                continue
            seen_categories.add(category)
            touches.setdefault(category, []).append((subtask.id, path))

    warnings: list[str] = []
    for category, entries in sorted(touches.items()):
        subtask_ids = {subtask_id for subtask_id, _ in entries}
        if len(subtask_ids) < 2:
            continue
        roots = {union_find.find(subtask_id) for subtask_id in subtask_ids}
        if len(roots) <= 1:
            continue
        detail = ", ".join(f"{subtask_id}:{path}" for subtask_id, path in entries)
        warnings.append(
            f"共有拡張ポイント（カテゴリ: {category}）に複数サブタスクが依存関係なしで"
            f"触れています。shared-contract/integration-scaffoldタスクの導入を検討"
            f"してください: {detail}"
        )
    return warnings
