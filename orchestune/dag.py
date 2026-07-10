from __future__ import annotations

import logging
import math
import re
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_SIMILARITY_THRESHOLD = 0.2

_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

_IGNORED_FOOTPRINT_PATTERNS = (
    re.compile(r"(^|/)pyproject\.toml$"),
    re.compile(r"(^|/)poetry\.lock$"),
    re.compile(r"(^|/)logging\.py$"),
    re.compile(r"(^|/)logger\.py$"),
    re.compile(r"(^|/)config\.py$"),
    re.compile(r"(^|/)settings\.py$"),
)


def _is_ignored_footprint(path_str: str) -> bool:
    return any(p.search(path_str) for p in _IGNORED_FOOTPRINT_PATTERNS)


_RISK_PATH_PATTERNS = (
    re.compile(r"(^|/)data/sources/"),
    re.compile(r"(^|/)credentials/"),
    re.compile(r"(^|/)auth", re.IGNORECASE),
)
_RISK_KEYWORDS = ("subprocess", "auth", "credential")

_FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


class DagCycleError(ValueError):
    """依存関係グラフに循環参照が検出された場合に送出される。"""


@dataclass(frozen=True)
class SubTask:
    id: str
    description: str
    footprint: tuple[str, ...]
    symbols: tuple[str, ...]
    depends_on: tuple[str, ...]
    risk: bool
    risk_reasons: tuple[str, ...]
    priority: str = "medium"

    def touch_set(self) -> frozenset[str]:
        filtered_footprint = frozenset(
            p for p in self.footprint if not _is_ignored_footprint(p)
        )
        return filtered_footprint | frozenset(self.symbols)


@dataclass(frozen=True)
class DagEdge:
    source: str
    target: str
    reason: str
    score: float | None = None


@dataclass(frozen=True)
class DagResult:
    subtasks: dict[str, SubTask]
    edges: list[DagEdge]
    topological_order: list[str]
    parallel_leaves: list[str]
    risky_subtask_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "subtasks": {
                subtask_id: {
                    "description": s.description,
                    "footprint": list(s.footprint),
                    "symbols": list(s.symbols),
                    "depends_on": list(s.depends_on),
                    "risk": s.risk,
                    "risk_reasons": list(s.risk_reasons),
                }
                for subtask_id, s in self.subtasks.items()
            },
            "edges": [
                {
                    "source": e.source,
                    "target": e.target,
                    "reason": e.reason,
                    "score": e.score,
                }
                for e in self.edges
            ],
            "topological_order": list(self.topological_order),
            "parallel_leaves": list(self.parallel_leaves),
            "risky_subtask_ids": list(self.risky_subtask_ids),
        }


@dataclass(frozen=True)
class FootprintConflict:
    subtask_id: str
    other_subtask_id: str
    similarity: float
    blocked_subtask_id: str


def _detect_risk_from_values(
    footprint: Iterable[str],
    symbols: Iterable[str],
    description: str,
    explicit: bool = False,
) -> tuple[bool, tuple[str, ...]]:
    footprint = tuple(footprint)
    symbols = tuple(symbols)
    reasons: list[str] = []

    for path_str in footprint:
        for pattern in _RISK_PATH_PATTERNS:
            if pattern.search(path_str):
                reasons.append(f"footprint:{path_str}")
                break

    haystack = " ".join([*footprint, *symbols, description]).lower()
    for keyword in _RISK_KEYWORDS:
        if keyword in haystack:
            reasons.append(f"keyword:{keyword}")

    if explicit:
        reasons.append("explicit")

    unique_reasons = tuple(dict.fromkeys(reasons))
    return bool(unique_reasons), unique_reasons


def _extract_frontmatter(text: str) -> dict[str, Any]:
    match = _FRONTMATTER_PATTERN.match(text)
    if not match:
        raise ValueError(
            "decomposition_plan.md にYAMLフロントマター（--- ... ---）が見つかりません"
        )
    data = yaml.safe_load(match.group(1))
    if not isinstance(data, dict):
        raise ValueError("フロントマターの内容がマッピング形式ではありません")
    return data


def parse_decomposition_plan(path: str | Path) -> list[SubTask]:
    """decomposition_plan.md のYAMLフロントマターをパースし、SubTaskの一覧を返す。"""
    text = Path(path).read_text(encoding="utf-8")
    data = _extract_frontmatter(text)

    raw_subtasks = data.get("subtasks")
    if not isinstance(raw_subtasks, list) or not raw_subtasks:
        raise ValueError("subtasks が定義されていないか、空です")

    subtasks: list[SubTask] = []
    seen_ids: set[str] = set()
    for raw in raw_subtasks:
        subtask_id = str(raw["id"])
        if subtask_id in seen_ids:
            raise ValueError(f"サブタスクIDが重複しています: {subtask_id}")
        seen_ids.add(subtask_id)

        footprint = tuple(str(f) for f in raw.get("footprint", []) or [])
        symbols = tuple(str(s) for s in raw.get("symbols", []) or [])
        depends_on = tuple(str(d) for d in raw.get("depends_on", []) or [])
        description = str(raw.get("description", ""))
        priority = str(raw.get("priority", "medium")).lower()
        if priority not in ("high", "medium", "low"):
            priority = "medium"

        risk, risk_reasons = _detect_risk_from_values(
            footprint, symbols, description, explicit=bool(raw.get("risk", False))
        )

        subtasks.append(
            SubTask(
                id=subtask_id,
                description=description,
                footprint=footprint,
                symbols=symbols,
                depends_on=depends_on,
                risk=risk,
                risk_reasons=risk_reasons,
                priority=priority,
            )
        )

    known_ids = {s.id for s in subtasks}
    for s in subtasks:
        unknown = [d for d in s.depends_on if d not in known_ids]
        if unknown:
            raise ValueError(f"未知のdepends_onが指定されています: {s.id} -> {unknown}")

    return subtasks


def _otsuka_ochiai(set_a: frozenset[str], set_b: frozenset[str]) -> float:
    """Otsuka-Ochiai係数によるコサイン類似度: |Si∩Sj| / sqrt(|Si|・|Sj|)。"""
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    if intersection == 0:
        return 0.0
    return intersection / math.sqrt(len(set_a) * len(set_b))


def _document_frequencies(subtasks: list[SubTask]) -> dict[str, int]:
    """footprint/symbolsの各アイテムが、いくつのサブタスクのtouch集合に
    出現するか（文書頻度）を数える。"""
    freq: dict[str, int] = {}
    for subtask in subtasks:
        for item in subtask.touch_set():
            freq[item] = freq.get(item, 0) + 1
    return freq


def _idf_weights(subtasks: list[SubTask]) -> dict[str, float]:
    """#191: IDF（逆文書頻度）風の重み付け。

    Logger・Configのような、多くのサブタスクが触れる共通ユーティリティは
    文書頻度(df)が高くなるため、重みが1.0に近づき結合度スコアへの寄与が
    小さくなる。固有のシンボル/ファイル（dfが低い）ほど重みが大きくなり、
    実質的な結合は引き続き強く検出される。

    smooth-idf: ln((n+1) / (df+1)) + 1.0 （nはサブタスク総数）。
    """
    n = len(subtasks)
    freq = _document_frequencies(subtasks)
    return {item: math.log((n + 1) / (df + 1)) + 1.0 for item, df in freq.items()}


def _weighted_otsuka_ochiai(
    subtask_a: SubTask, subtask_b: SubTask, weights: dict[str, float]
) -> float:
    """アイテムごとのIDF重みを考慮した重み付きOtsuka-Ochiai係数（重み付きコサイン類似度）。

    マージ衝突の直接的原因となる `footprint` (ファイル) の重みを `symbols` (シンボル) より
    高く評価するために、ファイルには2.0の倍率を適用します。
    """
    set_a = subtask_a.touch_set()
    set_b = subtask_b.touch_set()
    if not set_a or not set_b:
        return 0.0
    shared = set_a & set_b
    if not shared:
        return 0.0

    def get_weighted_value(item: str, subtask: SubTask) -> float:
        base_w = weights.get(item, 1.0)
        multiplier = 1.5 if item in subtask.footprint else 1.0
        return base_w * multiplier

    weighted_intersection = sum(
        get_weighted_value(item, subtask_a) * get_weighted_value(item, subtask_b)
        for item in shared
    )
    norm_a = math.sqrt(sum(get_weighted_value(item, subtask_a) ** 2 for item in set_a))
    norm_b = math.sqrt(sum(get_weighted_value(item, subtask_b) ** 2 for item in set_b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return weighted_intersection / (norm_a * norm_b)


def _find_candidate_pairs(subtasks: list[SubTask]) -> set[tuple[str, str]]:
    """一次探索: 共有ファイル/シンボル単位の軽量な逆引きインデックスから、
    交差が非ゼロになり得るペアだけを安価に絞り込む（総当りO(n^2)を回避）。"""
    inverted_index: dict[str, list[str]] = {}
    for subtask in subtasks:
        for item in subtask.touch_set():
            inverted_index.setdefault(item, []).append(subtask.id)

    candidates: set[tuple[str, str]] = set()
    for ids_sharing_item in inverted_index.values():
        if len(ids_sharing_item) < 2:
            continue
        for i, a in enumerate(ids_sharing_item):
            for b in ids_sharing_item[i + 1 :]:
                if a != b:
                    candidates.add(tuple(sorted((a, b))))  # type: ignore[arg-type]
    return candidates


def _determine_edge_direction(
    subtask_a: SubTask, subtask_b: SubTask, score: float
) -> DagEdge:
    a_pri = _PRIORITY_ORDER.get(subtask_a.priority.lower(), 1)
    b_pri = _PRIORITY_ORDER.get(subtask_b.priority.lower(), 1)

    # 1. 優先度の比較 (小さい方が優先度が高い)
    if a_pri != b_pri:
        if a_pri < b_pri:
            return DagEdge(
                source=subtask_a.id,
                target=subtask_b.id,
                reason="similarity",
                score=score,
            )
        else:
            return DagEdge(
                source=subtask_b.id,
                target=subtask_a.id,
                reason="similarity",
                score=score,
            )

    # 2. footprintの数の比較 (大きい方が先)
    a_fp_len = len(subtask_a.footprint)
    b_fp_len = len(subtask_b.footprint)
    if a_fp_len != b_fp_len:
        if a_fp_len > b_fp_len:
            return DagEdge(
                source=subtask_a.id,
                target=subtask_b.id,
                reason="similarity",
                score=score,
            )
        else:
            return DagEdge(
                source=subtask_b.id,
                target=subtask_a.id,
                reason="similarity",
                score=score,
            )

    # 3. 最終手段としてIDの辞書順
    if subtask_a.id < subtask_b.id:
        return DagEdge(
            source=subtask_a.id, target=subtask_b.id, reason="similarity", score=score
        )
    else:
        return DagEdge(
            source=subtask_b.id, target=subtask_a.id, reason="similarity", score=score
        )


def build_similarity_edges(
    subtasks: list[SubTask], threshold: float = DEFAULT_SIMILARITY_THRESHOLD
) -> list[DagEdge]:
    """二次探索: 一次探索の候補ペアに対してのみ、精緻な結合度スコアを算出する。

    #191: 出現頻度に応じたIDF風の重み付け（`_idf_weights`）を適用し、Logger・
    Configのような多くのサブタスクが共有する高頻度アイテムのみに基づく疑似結合を
    抑制する。固有シンボル/ファイルの共有による実質的な結合は、重みが1.0に
    近づかないため引き続き検出される。
    """
    by_id = {s.id: s for s in subtasks}
    weights = _idf_weights(subtasks)
    edges: list[DagEdge] = []
    for a_id, b_id in sorted(_find_candidate_pairs(subtasks)):
        score = _weighted_otsuka_ochiai(by_id[a_id], by_id[b_id], weights)
        if score > threshold:
            edges.append(_determine_edge_direction(by_id[a_id], by_id[b_id], score))
    return edges


def _collect_explicit_edges(subtasks: list[SubTask]) -> list[DagEdge]:
    edges: list[DagEdge] = []
    for subtask in subtasks:
        for dep_id in subtask.depends_on:
            edges.append(DagEdge(source=dep_id, target=subtask.id, reason="explicit"))
    return edges


def _merge_explicit_and_similarity(
    explicit_edges: list[DagEdge], similarity_edges: list[DagEdge]
) -> list[DagEdge]:
    explicit_pairs = {(e.source, e.target) for e in explicit_edges}
    merged = list(explicit_edges)
    for edge in similarity_edges:
        if (edge.source, edge.target) in explicit_pairs:
            continue
        if (edge.target, edge.source) in explicit_pairs:
            continue
        merged.append(edge)
    return merged


def _detect_cycle(node_ids: list[str], edges: list[DagEdge]) -> list[str] | None:
    """DFSによる循環参照検出。循環があればその経路を、なければNoneを返す。"""
    graph: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for edge in edges:
        graph[edge.source].append(edge.target)

    white, gray, black = 0, 1, 2
    color = dict.fromkeys(node_ids, white)
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = gray
        path.append(node)
        for neighbor in graph[node]:
            if color[neighbor] == gray:
                cycle_start = path.index(neighbor)
                return [*path[cycle_start:], neighbor]
            if color[neighbor] == white:
                found = visit(neighbor)
                if found:
                    return found
        path.pop()
        color[node] = black
        return None

    for node_id in node_ids:
        if color[node_id] == white:
            cycle = visit(node_id)
            if cycle:
                return cycle
    return None


def _topological_sort(node_ids: list[str], edges: list[DagEdge]) -> list[str]:
    in_degree = dict.fromkeys(node_ids, 0)
    graph: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for edge in edges:
        graph[edge.source].append(edge.target)
        in_degree[edge.target] += 1

    queue: deque[str] = deque(sorted(n for n in node_ids if in_degree[n] == 0))
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for neighbor in sorted(graph[node]):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if len(order) != len(node_ids):
        raise DagCycleError("トポロジカルソートに失敗しました（循環参照が疑われます）")
    return order


def _resolve_cycles_if_possible(
    node_ids: list[str], edges: list[DagEdge]
) -> list[DagEdge]:
    edges = list(edges)
    while True:
        cycle = _detect_cycle(node_ids, edges)
        if not cycle:
            break

        cycle_edges: list[DagEdge] = []
        for i in range(len(cycle) - 1):
            src, tgt = cycle[i], cycle[i + 1]
            for edge in edges:
                if edge.source == src and edge.target == tgt:
                    cycle_edges.append(edge)
                    break

        similarity_cycle_edges = [e for e in cycle_edges if e.reason == "similarity"]

        if not similarity_cycle_edges:
            raise DagCycleError(f"循環参照を検出しました: {' -> '.join(cycle)}")

        weakest_edge = min(
            similarity_cycle_edges,
            key=lambda e: e.score if e.score is not None else 0.0,
        )

        logger.warning(
            f"循環参照を検出したため、類似度エッジを自動解消しました: {weakest_edge.source} -> {weakest_edge.target} "
            f"(reason: {weakest_edge.reason}, score: {weakest_edge.score})"
        )
        edges.remove(weakest_edge)

    return edges


def _assemble_dag(
    subtask_list: list[SubTask], merged_edges: list[DagEdge]
) -> DagResult:
    node_ids = [s.id for s in subtask_list]

    resolved_edges = _resolve_cycles_if_possible(node_ids, merged_edges)

    topological_order = _topological_sort(node_ids, resolved_edges)

    in_degree = dict.fromkeys(node_ids, 0)
    for edge in resolved_edges:
        in_degree[edge.target] += 1
    parallel_leaves = sorted(n for n in node_ids if in_degree[n] == 0)

    risky_subtask_ids = sorted(s.id for s in subtask_list if s.risk)

    return DagResult(
        subtasks={s.id: s for s in subtask_list},
        edges=resolved_edges,
        topological_order=topological_order,
        parallel_leaves=parallel_leaves,
        risky_subtask_ids=risky_subtask_ids,
    )


def build_dag(
    subtasks: list[SubTask], threshold: float = DEFAULT_SIMILARITY_THRESHOLD
) -> DagResult:
    """明示的依存 + 結合度スコアによる依存を統合し、DAGを構築する。

    循環参照が見つかった場合は DagCycleError を送出する。
    """
    explicit_edges = _collect_explicit_edges(subtasks)
    similarity_edges = build_similarity_edges(subtasks, threshold=threshold)
    merged_edges = _merge_explicit_and_similarity(explicit_edges, similarity_edges)
    return _assemble_dag(subtasks, merged_edges)


def build_dag_from_plan(
    path: str | Path, threshold: float = DEFAULT_SIMILARITY_THRESHOLD
) -> dict[str, Any]:
    """decomposition_plan.md のパスを受け取り、DAG構造(JSON互換dict)を返す単純な入出力。

    #187のドラフト呼び出し導線から薄いラッパーとして呼び出される想定のため、
    入出力はこの1関数（パス文字列 -> dict）のみで完結する。
    """
    subtasks = parse_decomposition_plan(path)
    dag = build_dag(subtasks, threshold=threshold)
    return dag.to_dict()


def recompute_dag_for_footprint_change(
    subtasks: dict[str, SubTask],
    subtask_id: str,
    updated_footprint: Iterable[str] | None = None,
    updated_symbols: Iterable[str] | None = None,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> tuple[DagResult, list[FootprintConflict]]:
    """実行中サブタスクのfootprint逸脱を反映してDAGを再計算する。

    設計判断: 新たに検出された結合度エッジ（衝突ペア）は、既に実行が
    始まっている `subtask_id` 側を優先し、相手側のサブタスクを直列化
    （待機）させる方向で辺を張る。どちらも未実行の状態から新規に競合が
    発覚するケースでは「先に走り出した側が優先される」という単純な
    ルールの方が、通知・記録（#184）側で扱いやすいと判断した。
    また、一度リスクフラグが立ったサブタスクは、再計算後も安全側に倒し
    リスクフラグを保持し続ける（モノトニックに扱う）。
    """
    if subtask_id not in subtasks:
        raise KeyError(f"未知のサブタスクIDです: {subtask_id}")

    before_similarity_pairs = {
        frozenset((e.source, e.target))
        for e in build_similarity_edges(list(subtasks.values()), threshold=threshold)
    }

    old_subtask = subtasks[subtask_id]
    new_footprint = (
        tuple(updated_footprint)
        if updated_footprint is not None
        else old_subtask.footprint
    )
    new_symbols = (
        tuple(updated_symbols) if updated_symbols is not None else old_subtask.symbols
    )

    heuristic_risk, heuristic_reasons = _detect_risk_from_values(
        new_footprint, new_symbols, old_subtask.description
    )
    combined_risk = old_subtask.risk or heuristic_risk
    combined_reasons = tuple(
        dict.fromkeys([*old_subtask.risk_reasons, *heuristic_reasons])
    )

    new_subtask = SubTask(
        id=old_subtask.id,
        description=old_subtask.description,
        footprint=new_footprint,
        symbols=new_symbols,
        depends_on=old_subtask.depends_on,
        risk=combined_risk,
        risk_reasons=combined_reasons,
        priority=old_subtask.priority,
    )

    updated_subtasks = dict(subtasks)
    updated_subtasks[subtask_id] = new_subtask
    subtask_list = list(updated_subtasks.values())

    explicit_edges = _collect_explicit_edges(subtask_list)
    explicit_pairs = {(e.source, e.target) for e in explicit_edges}
    similarity_edges = build_similarity_edges(subtask_list, threshold=threshold)

    conflicts: list[FootprintConflict] = []
    final_similarity_edges: list[DagEdge] = []
    for edge in similarity_edges:
        pair_key = frozenset((edge.source, edge.target))
        if (edge.source, edge.target) in explicit_pairs or (
            edge.target,
            edge.source,
        ) in explicit_pairs:
            continue  # 明示的な依存が既にあるため、新規の衝突として扱わない

        is_new = pair_key not in before_similarity_pairs
        if is_new and subtask_id in pair_key:
            other_id = edge.target if edge.source == subtask_id else edge.source
            final_similarity_edges.append(
                DagEdge(
                    source=subtask_id,
                    target=other_id,
                    reason="similarity",
                    score=edge.score,
                )
            )
            conflicts.append(
                FootprintConflict(
                    subtask_id=subtask_id,
                    other_subtask_id=other_id,
                    similarity=edge.score or 0.0,
                    blocked_subtask_id=other_id,
                )
            )
        else:
            final_similarity_edges.append(edge)

    merged_edges = explicit_edges + final_similarity_edges
    result = _assemble_dag(subtask_list, merged_edges)
    return result, conflicts


def main() -> None:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="orchestune DAG validation tool")
    parser.add_argument(
        "--plan",
        default="decomposition_plan.md",
        help="Path to the decomposition plan markdown file (default: decomposition_plan.md)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    args = parser.parse_args()

    try:
        dag_dict = build_dag_from_plan(args.plan)
        if args.json:
            print(json.dumps(dag_dict, indent=2))
        else:
            print("DAG validation succeeded.")
            print(f"Topological order: {' -> '.join(dag_dict['topological_order'])}")
            print(f"Parallel leaves: {', '.join(dag_dict['parallel_leaves'])}")
            if dag_dict["risky_subtask_ids"]:
                print(f"Risky subtasks: {', '.join(dag_dict['risky_subtask_ids'])}")
            if dag_dict["edges"]:
                print("Edges:")
                for edge in dag_dict["edges"]:
                    score_str = (
                        f" (score: {edge['score']:.2f})"
                        if edge.get("score") is not None
                        else ""
                    )
                    print(
                        f"  {edge['source']} -> {edge['target']} [reason: {edge['reason']}{score_str}]"
                    )
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
