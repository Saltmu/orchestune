"""Similarity scoring and edge inference for DAG subtasks.

サブタスク間の重なりから暗黙の依存エッジを推定する手法は、Co-Coder
(arXiv:2606.00953, "When Parallelism Pays Off: Cohesion-Aware Task
Partitioning for Multi-Agent Coding") を参考にしている。ただし目的が
異なるため、以下を変更している:

- グラフの由来: 既存リポジトリの構造ではなく、decomposition planで
  宣言されたfootprint/symbols（まだ存在しないファイルを含む）
- 出力: Infomapによるコミュニティ分割ではなく、閾値超過ペアへの
  依存エッジ（担当の割り当てではなく実行順序を決めるため）
- 重み付け: IDF（多くのサブタスクが触る項目の弁別力を下げる）と、
  footprint項目への1.5倍係数（マージを壊すのはファイル単位の衝突のため）
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable

from orchestune.dag_models import DagEdge, SubTask

DEFAULT_SIMILARITY_THRESHOLD = 0.2
_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def otsuka_ochiai(set_a: frozenset[str], set_b: frozenset[str]) -> float:
    """Return the Otsuka-Ochiai coefficient for two sets."""
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    if intersection == 0:
        return 0.0
    return intersection / math.sqrt(len(set_a) * len(set_b))


def _document_frequencies(
    subtasks: list[SubTask], ignore_patterns: Iterable[re.Pattern[str]] = ()
) -> dict[str, int]:
    frequencies: dict[str, int] = {}
    for subtask in subtasks:
        for item in subtask.touch_set(ignore_patterns):
            frequencies[item] = frequencies.get(item, 0) + 1
    return frequencies


def _idf_weights(
    subtasks: list[SubTask], ignore_patterns: Iterable[re.Pattern[str]] = ()
) -> dict[str, float]:
    count = len(subtasks)
    return {
        item: math.log((count + 1) / (frequency + 1)) + 1.0
        for item, frequency in _document_frequencies(subtasks, ignore_patterns).items()
    }


def _weighted_value(item: str, subtask: SubTask, weights: dict[str, float]) -> float:
    multiplier = 1.5 if item in subtask.footprint else 1.0
    return weights.get(item, 1.0) * multiplier


def _weighted_otsuka_ochiai(
    subtask_a: SubTask,
    subtask_b: SubTask,
    weights: dict[str, float],
    ignore_patterns: Iterable[re.Pattern[str]] = (),
) -> float:
    set_a = subtask_a.touch_set(ignore_patterns)
    set_b = subtask_b.touch_set(ignore_patterns)
    shared = set_a & set_b
    if not shared:
        return 0.0

    weighted_intersection = sum(
        _weighted_value(item, subtask_a, weights)
        * _weighted_value(item, subtask_b, weights)
        for item in shared
    )
    norm_a = math.sqrt(
        sum(_weighted_value(item, subtask_a, weights) ** 2 for item in set_a)
    )
    norm_b = math.sqrt(
        sum(_weighted_value(item, subtask_b, weights) ** 2 for item in set_b)
    )
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return weighted_intersection / (norm_a * norm_b)


def find_candidate_pairs(
    subtasks: list[SubTask], ignore_patterns: Iterable[re.Pattern[str]] = ()
) -> set[tuple[str, str]]:
    """Find only subtask pairs that share at least one touch-set item."""
    inverted_index: dict[str, list[str]] = {}
    for subtask in subtasks:
        for item in subtask.touch_set(ignore_patterns):
            inverted_index.setdefault(item, []).append(subtask.id)

    candidates: set[tuple[str, str]] = set()
    for ids_sharing_item in inverted_index.values():
        for index, first in enumerate(ids_sharing_item):
            for second in ids_sharing_item[index + 1 :]:
                if first != second:
                    low, high = sorted((first, second))
                    candidates.add((low, high))
    return candidates


def _similarity_edge(source: SubTask, target: SubTask, score: float) -> DagEdge:
    return DagEdge(
        source=source.id,
        target=target.id,
        reason="similarity",
        score=score,
    )


def _determine_edge_direction(
    subtask_a: SubTask,
    subtask_b: SubTask,
    score: float,
) -> DagEdge:
    priority_a = _PRIORITY_ORDER.get(subtask_a.priority.lower(), 1)
    priority_b = _PRIORITY_ORDER.get(subtask_b.priority.lower(), 1)
    if priority_a != priority_b:
        source, target = (
            (subtask_a, subtask_b)
            if priority_a < priority_b
            else (subtask_b, subtask_a)
        )
        return _similarity_edge(source, target, score)

    if len(subtask_a.footprint) != len(subtask_b.footprint):
        source, target = (
            (subtask_a, subtask_b)
            if len(subtask_a.footprint) > len(subtask_b.footprint)
            else (subtask_b, subtask_a)
        )
        return _similarity_edge(source, target, score)

    source, target = (
        (subtask_a, subtask_b)
        if subtask_a.id < subtask_b.id
        else (subtask_b, subtask_a)
    )
    return _similarity_edge(source, target, score)


def build_similarity_edges(
    subtasks: list[SubTask],
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ignore_patterns: Iterable[re.Pattern[str]] = (),
) -> list[DagEdge]:
    """Infer dependency edges for candidate pairs above the similarity threshold."""
    # ignore_patternsは複数回反復されるため、ジェネレータ等の使い捨てiterableが
    # 渡された場合に2回目以降の反復が空になる問題を避けてtupleへ実体化する。
    ignore_patterns = tuple(ignore_patterns)
    by_id = {subtask.id: subtask for subtask in subtasks}
    weights = _idf_weights(subtasks, ignore_patterns)
    edges: list[DagEdge] = []
    for first_id, second_id in sorted(find_candidate_pairs(subtasks, ignore_patterns)):
        first = by_id[first_id]
        second = by_id[second_id]
        score = _weighted_otsuka_ochiai(first, second, weights, ignore_patterns)
        if score > threshold:
            edges.append(_determine_edge_direction(first, second, score))
    return edges
