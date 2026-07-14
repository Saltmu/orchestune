"""未確立の共有拡張ポイント（shared-contract）に対する所有権未確定の検出。

`dag_similarity.py` の重複検出は宣言済みfootprint/symbolsの文字列一致（または
その加重コサイン類似度）にのみ依存するため、複数のサブタスクがまだ存在しない
共有ファイル（フォーマットレジストリ、CLI配線モジュール、依存関係マニフェスト等）
を、それぞれ異なる想定パスで触れようとしている場合には重複を検出できない。
本モジュールは、そうした「典型的な共有拡張ポイントのカテゴリ」に複数のサブタスクが
**並列実行され得る**（DAG上でどちらもどちらへも到達不能な）順序関係なしに触れて
いる場合に警告を生成する（#175）。

判定は「同じ連結成分に属するか」ではなく「両者の間に有向の到達可能性（一方が
他方の祖先であるか）があるか」で行う。共通の祖先タスクを持つだけの2タスクは
（例: `shared -> csv`, `shared -> yaml`）互いには到達不能であり、実際には並列に
実行され得るため、これは警告対象である。

`dispatch_locks.py` の `_HOTSPOT_PATTERNS` はディスパッチ実行時のチャーン抑制
（既知の頻出変更ファイルを無視する）が目的であり、意図が異なるためパターンは
共有しない。
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable

from orchestune.dag_models import DagEdge, SubTask

_SHARED_CONTRACT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "registry",
        re.compile(r"(^|/)[\w.-]*regist(?:ry|ration|rar)[\w.-]*\.\w+$", re.IGNORECASE),
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


def _scope(path: str) -> str:
    """カテゴリだけでは無関係なパッケージ同士(例: packages/auth/__init__.py と
    packages/payments/__init__.py)まで同一ホットスポット扱いしてしまうため、
    親ディレクトリを追加のグルーピングキーとして用いる。"""
    return posixpath.dirname(path)


def _forward_reachable(
    node_ids: Iterable[str], edges: list[DagEdge]
) -> dict[str, set[str]]:
    """各ノードから有向エッジ(source -> target、sourceが先行)を辿って到達できる
    ノード集合(=そのノードより後に実行されるノード)を返す。"""
    node_ids = list(node_ids)
    graph: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for edge in edges:
        graph[edge.source].append(edge.target)

    reachable: dict[str, set[str]] = {}
    for node_id in node_ids:
        seen: set[str] = set()
        stack = list(graph[node_id])
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(graph.get(current, []))
        reachable[node_id] = seen
    return reachable


def _is_ordered(a: str, b: str, reachable: dict[str, set[str]]) -> bool:
    """aとbのどちらかが他方の祖先であれば(=DAG上で順序付けられ、並列実行され
    得なければ)Trueを返す。"""
    return b in reachable[a] or a in reachable[b]


def _has_unordered_pair(ids: list[str], reachable: dict[str, set[str]]) -> bool:
    return any(
        not _is_ordered(a, b, reachable)
        for index, a in enumerate(ids)
        for b in ids[index + 1 :]
    )


def _format_warning(label: str, entries: list[tuple[str, str]], hint: str) -> str:
    detail = ", ".join(f"{subtask_id}:{path}" for subtask_id, path in entries)
    return (
        f"{label}に複数サブタスクが並列実行され得る順序関係のまま触れています。"
        f"{hint}: {detail}"
    )


def find_unowned_shared_contract_hotspots(
    subtasks: list[SubTask],
    edges: list[DagEdge],
) -> list[str]:
    """所有者不明の共有拡張ポイントに対する警告メッセージ一覧を返す。

    2段階で検出する:
    1. 明示的な`shared_contract`タグ（プラン作成者が同一の未確立コントラクトだと
       明示したサブタスク群）。ヒューリスティックに頼らない、最も信頼できる
       シグナル。
    2. カテゴリ(registry/cli-wiring/public-api/dependency-manifest)とディレクトリ
       スコープに基づくヒューリスティックなフォールバック（`shared_contract`が
       付与されていないサブタスクのみが対象）。ディレクトリが異なる場合は
       同一ホットスポットとみなさないため、レジストリのように想定パスの
       ディレクトリごと異なるケースまでは捕捉できない — そうしたケースは
       明示的な`shared_contract`タグの付与が推奨される。

    いずれの段階でも、判定は連結性ではなく有向の到達可能性（一方が他方の祖先か）
    で行う。ブロッキングエラーにはしない。
    """
    reachable = _forward_reachable((subtask.id for subtask in subtasks), edges)

    warnings: list[str] = []

    explicit_groups: dict[str, list[tuple[str, str]]] = {}
    tagged_ids: set[str] = set()
    for subtask in subtasks:
        if not subtask.shared_contract:
            continue
        tagged_ids.add(subtask.id)
        path_hint = subtask.footprint[0] if subtask.footprint else "(footprint未指定)"
        explicit_groups.setdefault(subtask.shared_contract, []).append(
            (subtask.id, path_hint)
        )

    for contract_id, entries in sorted(explicit_groups.items()):
        ids = sorted({subtask_id for subtask_id, _ in entries})
        if len(ids) < 2 or not _has_unordered_pair(ids, reachable):
            continue
        warnings.append(
            _format_warning(
                f"共有コントラクト（shared_contract: {contract_id}）",
                entries,
                "依存順序（depends_on）の追加を検討してください",
            )
        )

    heuristic_touches: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for subtask in subtasks:
        if subtask.id in tagged_ids:
            continue
        seen_keys: set[tuple[str, str]] = set()
        for path in subtask.footprint:
            category = _categorize(path)
            if category is None:
                continue
            key = (category, _scope(path))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            heuristic_touches.setdefault(key, []).append((subtask.id, path))

    for (category, scope), entries in sorted(heuristic_touches.items()):
        ids = sorted({subtask_id for subtask_id, _ in entries})
        if len(ids) < 2 or not _has_unordered_pair(ids, reachable):
            continue
        warnings.append(
            _format_warning(
                f"共有拡張ポイント（カテゴリ: {category}, scope: {scope or '.'}）",
                entries,
                "shared_contract識別子の付与、またはshared-contract/"
                "integration-scaffoldタスクの導入を検討してください",
            )
        )

    return warnings
