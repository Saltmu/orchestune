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

ただし、比較対象は共有ファイルへ実際に「書き込む」サブタスク同士に限る。
契約タスクにdepends_onするだけで自身は共有ファイルに触れない消費者サブタスク
（読み取り・importのみ）は、たとえ`shared_contract`タグを共有していても、
互いに未接続なまま安全に並列実行できるため対象外とする。

`dispatch_locks.py` の `_HOTSPOT_PATTERNS` はディスパッチ実行時のチャーン抑制
（既知の頻出変更ファイルを無視する）が目的であり、意図が異なるためパターンは
共有しない。
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from typing import Protocol

from orchestune.dag.documents import declares_shared_document, is_shared_document_path
from orchestune.dag.models import (
    ConflictEdge,
    ConflictGraph,
    DagEdge,
    SubTask,
    is_ignored_footprint,
)

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
            r"(^|/)(pyproject\.toml|package\.json|poetry\.lock|uv\.lock|"
            r"package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.toml|go\.mod)$"
        ),
    ),
)


_CONTRACT_RESOURCE_PREFIX = "shared_contract:"


def _categorize(path: str) -> str | None:
    for category, pattern in _SHARED_CONTRACT_PATTERNS:
        if pattern.search(path):
            return category
    return None


class SharedContractTask(Protocol):
    """The metadata required to identify a shared-contract writer."""

    @property
    def footprint(self) -> tuple[str, ...]: ...

    @property
    def writes_shared_contract(self) -> bool: ...


def _touches_hotspot_category(subtask: SharedContractTask) -> bool:
    return any(_categorize(path) is not None for path in subtask.footprint)


def is_contract_writer(subtask: SharedContractTask) -> bool:
    """サブタスクが共有拡張ポイントの「書き込み者」かどうかを判定する。

    `shared_contract`タグは「同一の契約に関与している」ことしか意味しない —
    契約タスクにdepends_onするだけで自身のfootprintがその共有ファイルに
    触れない（依存・importするだけの）消費者サブタスクも同じタグを持ち得る。
    そうした消費者同士は互いに未接続でも安全に並列実行できるため、比較対象
    から除外する必要がある。書き込み者かどうかは、明示的な
    `writes_shared_contract`フラグ、footprintがいずれかの共有拡張ポイント
    カテゴリに一致するか、または共有文書（`docs/`配下のMarkdown、#724）を
    footprintへ宣言しているかで判定する（カテゴリ一致は一般的な命名のレジストリ
    ファイル等を自動検出するためのヒューリスティックであり、命名パターンに
    一致しない独自のファイル名を書き込む場合は明示的なフラグの指定が必要）。
    文書を読むだけでfootprintへ含めない消費者はwriterにならない。この判定は
    `dag_ignore_patterns`では解除されない（除外設定は自動検出だけに作用する）。
    """
    return (
        subtask.writes_shared_contract
        or _touches_hotspot_category(subtask)
        or declares_shared_document(subtask)
    )


def _representative_path(subtask: SubTask) -> str:
    for path in subtask.footprint:
        if _categorize(path) is not None:
            return path
    for path in subtask.footprint:
        if is_shared_document_path(path):
            return path
    return subtask.footprint[0] if subtask.footprint else "(footprint未指定)"


def _scope(path: str) -> str:
    """カテゴリだけでは無関係なパッケージ同士(例: packages/auth/__init__.py と
    packages/payments/__init__.py)まで同一ホットスポット扱いしてしまうため、
    親ディレクトリを追加のグルーピングキーとして用いる。"""
    return posixpath.dirname(path)


def _pairwise(ids: list[str]) -> list[tuple[str, str]]:
    ordered_ids = sorted(set(ids))
    return [
        (left, right)
        for index, left in enumerate(ordered_ids)
        for right in ordered_ids[index + 1 :]
    ]


def build_shared_contract_conflicts(
    subtasks: list[SubTask],
    ignore_patterns: Iterable[re.Pattern[str]] = (),
) -> list[ConflictEdge]:
    """Return symmetric writer conflicts for declared and inferred contracts.

    Unlike warning generation below, these constraints are retained even when
    an explicit dependency also orders the pair. Precedence answers *when* a
    task becomes ready; this graph independently records whether the two tasks
    may run at the same time.
    """
    ignore_patterns = tuple(ignore_patterns)
    conflicts: dict[tuple[str, str], ConflictEdge] = {}
    explicit_groups: dict[str, list[str]] = {}
    for subtask in subtasks:
        if subtask.shared_contract and is_contract_writer(subtask):
            explicit_groups.setdefault(subtask.shared_contract, []).append(subtask.id)

    for contract_id, ids in sorted(explicit_groups.items()):
        for left, right in _pairwise(ids):
            conflicts[(left, right)] = ConflictEdge(
                left,
                right,
                reason="shared-contract",
                resources=(f"shared_contract:{contract_id}",),
            )

    heuristic_groups: dict[tuple[str, str], list[str]] = {}
    for subtask in subtasks:
        seen: set[tuple[str, str]] = set()
        for path in subtask.footprint:
            if is_ignored_footprint(path, ignore_patterns):
                continue
            category = _categorize(path)
            if category is None:
                continue
            key = (category, _scope(path))
            if key not in seen:
                heuristic_groups.setdefault(key, []).append(subtask.id)
                seen.add(key)

    for (category, scope), ids in sorted(heuristic_groups.items()):
        for left, right in _pairwise(ids):
            if (left, right) in conflicts:
                continue
            conflicts[(left, right)] = ConflictEdge(
                left,
                right,
                reason="shared-contract-hotspot",
                resources=(f"shared_contract_hotspot:{category}:{scope or '.'}",),
            )

    return list(conflicts.values())


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


def _unordered_pairs(
    ids: list[str], reachable: dict[str, set[str]]
) -> list[tuple[str, str]]:
    return [
        (a, b)
        for index, a in enumerate(ids)
        for b in ids[index + 1 :]
        if not _is_ordered(a, b, reachable)
    ]


def _format_warning(
    label: str,
    entries: list[tuple[str, str]],
    hint: str,
    pairs: list[tuple[str, str]],
) -> str:
    """排他と先行関係は別の事実なので、並列実行を断定せず未指定の先行関係を示す。"""
    detail = ", ".join(f"{subtask_id}:{path}" for subtask_id, path in entries)
    unresolved = ", ".join(f"{left}×{right}" for left, right in pairs)
    return (
        f"{label}に複数サブタスクが触れていますが、DAG上の先行関係（depends_on）が"
        "未指定で、共有契約の所有者・順序要否の確認が必要です。"
        f"{hint}: {detail}（未解決ペア: {unresolved}）"
    )


def _protected_contracts(
    conflict_graph: ConflictGraph | None,
) -> dict[frozenset[str], set[str]]:
    """明示契約`shared_contract:C`の排他辺を持つペアと契約IDの対応を返す。"""
    protected: dict[frozenset[str], set[str]] = {}
    if conflict_graph is None:
        return protected
    for edge in conflict_graph.edges:
        if edge.reason != "shared-contract":
            continue
        for resource in edge.resources:
            if resource.startswith(_CONTRACT_RESOURCE_PREFIX):
                protected.setdefault(edge.pair, set()).add(
                    resource[len(_CONTRACT_RESOURCE_PREFIX) :]
                )
    return protected


def _is_protected(
    pair: tuple[str, str],
    contract_id: str | None,
    protected: dict[frozenset[str], set[str]],
) -> bool:
    return contract_id is not None and contract_id in protected.get(
        frozenset(pair), set()
    )


def _entries_for_pairs(
    entries: list[tuple[str, str]], pairs: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    involved = {subtask_id for pair in pairs for subtask_id in pair}
    return [entry for entry in entries if entry[0] in involved]


def _check_explicit_contract_warnings(
    subtasks: list[SubTask],
    reachable: dict[str, set[str]],
    protected: dict[frozenset[str], set[str]],
    warned_pairs: set[frozenset[str]],
    warnings: list[str],
) -> None:
    explicit_groups: dict[str, list[tuple[str, str]]] = {}
    for subtask in subtasks:
        if not subtask.shared_contract or not is_contract_writer(subtask):
            continue
        explicit_groups.setdefault(subtask.shared_contract, []).append(
            (subtask.id, _representative_path(subtask))
        )

    for contract_id, entries in sorted(explicit_groups.items()):
        ids = sorted({subtask_id for subtask_id, _ in entries})
        pairs = [
            pair
            for pair in _unordered_pairs(ids, reachable)
            if not _is_protected(pair, contract_id, protected)
        ]
        if len(ids) < 2 or not pairs:
            continue
        warned_pairs.update(frozenset(pair) for pair in pairs)
        warnings.append(
            _format_warning(
                f"共有コントラクト（shared_contract: {contract_id}）",
                _entries_for_pairs(entries, pairs),
                "依存順序（depends_on）の追加を検討してください",
                pairs,
            )
        )


def _check_heuristic_contract_warnings(
    subtasks: list[SubTask],
    reachable: dict[str, set[str]],
    protected: dict[frozenset[str], set[str]],
    warned_pairs: set[frozenset[str]],
    warnings: list[str],
) -> None:
    contracts = {
        subtask.id: subtask.shared_contract
        for subtask in subtasks
        if is_contract_writer(subtask)
    }
    heuristic_touches: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for subtask in subtasks:
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
        pairs = [
            pair
            for pair in _unordered_pairs(ids, reachable)
            if frozenset(pair) not in warned_pairs
            and not _is_same_protected_contract(pair, contracts, protected)
        ]
        if len(ids) < 2 or not pairs:
            continue
        warnings.append(
            _format_warning(
                f"共有拡張ポイント（カテゴリ: {category}, scope: {scope or '.'}）",
                _entries_for_pairs(entries, pairs),
                "shared_contract識別子の付与、またはshared-contract/"
                "integration-scaffoldタスクの導入を検討してください",
                pairs,
            )
        )


def _is_same_protected_contract(
    pair: tuple[str, str],
    contracts: dict[str, str | None],
    protected: dict[frozenset[str], set[str]],
) -> bool:
    """両者が同じ明示契約のwriterで、その契約の排他辺を確認できるか。"""
    left, right = pair
    contract_id = contracts.get(left)
    return (
        left in contracts
        and right in contracts
        and contract_id == contracts[right]
        and _is_protected(pair, contract_id, protected)
    )


def find_unowned_shared_contract_hotspots(
    subtasks: list[SubTask],
    edges: list[DagEdge],
    *,
    conflict_graph: ConflictGraph | None = None,
) -> list[str]:
    """所有者不明の共有拡張ポイントに対する警告メッセージ一覧を返す。

    2段階で検出する:
    1. 明示的な`shared_contract`タグ（プラン作成者が同一の未確立コントラクトだと
       明示したサブタスク群）のうち、実際に共有ファイルへ「書き込む」サブタスク
       同士（`is_contract_writer`参照）。
    2. カテゴリとディレクトリスコープに基づくヒューリスティックなフォールバック。

    `conflict_graph`を渡すと、同じ明示契約Cのwriterペアのうち`shared-contract`
    かつ`shared_contract:C`の排他辺を確認できるものを警告から除外する（#724）。
    similarity・shared-document・hotspot辺や別契約の辺では抑制しない。
    省略時は排他確認をせず従来の保守的な判定を維持する。
    """
    reachable = _forward_reachable((subtask.id for subtask in subtasks), edges)
    protected = _protected_contracts(conflict_graph)
    warnings: list[str] = []
    warned_pairs: set[frozenset[str]] = set()

    _check_explicit_contract_warnings(
        subtasks, reachable, protected, warned_pairs, warnings
    )
    _check_heuristic_contract_warnings(
        subtasks, reachable, protected, warned_pairs, warnings
    )

    return warnings
