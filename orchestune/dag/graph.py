"""Dependency graph assembly, validation, and runtime recomputation."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from orchestune.dag.contracts import (
    build_shared_contract_conflicts,
    find_unowned_shared_contract_hotspots,
)
from orchestune.dag.models import (
    ConflictEdge,
    ConflictGraph,
    DagCycleError,
    DagEdge,
    DagResult,
    FootprintConflict,
    SubTask,
)
from orchestune.dag.parsing import detect_risk_from_values, parse_decomposition_plan
from orchestune.dag.similarity import (
    DEFAULT_SIMILARITY_THRESHOLD,
    build_similarity_conflicts,
)
from orchestune.symbol_verification import (
    find_missing_footprint_paths,
    find_missing_symbols,
)


def _collect_explicit_edges(subtasks: list[SubTask]) -> list[DagEdge]:
    return [
        DagEdge(source=dependency, target=subtask.id, reason="explicit")
        for subtask in subtasks
        for dependency in subtask.depends_on
    ]


def _adjacency(node_ids: list[str], edges: list[DagEdge]) -> dict[str, list[str]]:
    graph: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for edge in edges:
        graph[edge.source].append(edge.target)
    return graph


def _detect_cycle(node_ids: list[str], edges: list[DagEdge]) -> list[str] | None:
    graph = _adjacency(node_ids, edges)
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
                cycle = visit(neighbor)
                if cycle:
                    return cycle
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
    graph = _adjacency(node_ids, edges)
    for edge in edges:
        in_degree[edge.target] += 1

    queue: deque[str] = deque(sorted(node for node in node_ids if in_degree[node] == 0))
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


def _footprint_and_symbol_warnings(
    subtasks: list[SubTask], repo_root: str | Path
) -> list[str]:
    """footprint/symbolsが`repo_root`上の実コードに実在するかを警告として返す。

    存在しないパス・シンボルはブロッキングエラーにはしない: footprintは
    このsubtaskがこれから新規作成するファイルを含みうるため、未検出＝
    誤りとは断定できない（`find_missing_footprint_paths`/`find_missing_symbols`
    のdocstring参照）。
    """
    warnings: list[str] = []
    for subtask in subtasks:
        missing_paths = find_missing_footprint_paths(subtask, repo_root)
        if missing_paths:
            warnings.append(
                f"{subtask.id}: footprintに実在しないパスがあります"
                "（新規作成予定でなければfootprintの記載漏れを確認してください）: "
                f"{', '.join(missing_paths)}"
            )
        missing_symbols = find_missing_symbols(subtask, repo_root)
        if missing_symbols:
            warnings.append(
                f"{subtask.id}: symbolsが実コードベースに見つかりません"
                "（このsubtaskで新規追加予定でなければ確認してください）: "
                f"{', '.join(missing_symbols)}"
            )
    return warnings


def _assemble_dag(
    subtasks: list[SubTask],
    edges: list[DagEdge],
    conflict_graph: ConflictGraph,
    repo_root: str | Path | None = None,
) -> DagResult:
    node_ids = [subtask.id for subtask in subtasks]
    if cycle := _detect_cycle(node_ids, edges):
        raise DagCycleError(f"循環参照を検出しました: {' -> '.join(cycle)}")
    topological_order = _topological_sort(node_ids, edges)
    targets = {edge.target for edge in edges}

    contract_warnings = list(find_unowned_shared_contract_hotspots(subtasks, edges))
    existence_warnings = (
        _footprint_and_symbol_warnings(subtasks, repo_root)
        if repo_root is not None
        else []
    )
    all_warnings = tuple(contract_warnings + existence_warnings)

    return DagResult(
        subtasks={subtask.id: subtask for subtask in subtasks},
        edges=edges,
        topological_order=topological_order,
        parallel_leaves=sorted(node for node in node_ids if node not in targets),
        risky_subtask_ids=sorted(subtask.id for subtask in subtasks if subtask.risk),
        conflict_graph=conflict_graph,
        warnings=all_warnings,
    )


def build_conflict_graph(
    subtasks: list[SubTask],
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ignore_patterns: Iterable[re.Pattern[str]] = (),
) -> ConflictGraph:
    """Build symmetric exclusion constraints independently of precedence."""
    ignore_patterns = tuple(ignore_patterns)
    similarity = build_similarity_conflicts(
        subtasks, threshold=threshold, ignore_patterns=ignore_patterns
    )
    shared_contracts = build_shared_contract_conflicts(subtasks, ignore_patterns)
    return ConflictGraph(tuple([*similarity, *shared_contracts]))


def build_dag(
    subtasks: list[SubTask],
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    repo_root: str | Path | None = None,
    ignore_patterns: Iterable[re.Pattern[str]] = (),
) -> DagResult:
    """Build an explicit precedence DAG and a separate conflict graph."""
    explicit_edges = _collect_explicit_edges(subtasks)
    conflict_graph = build_conflict_graph(
        subtasks, threshold=threshold, ignore_patterns=ignore_patterns
    )
    return _assemble_dag(
        subtasks,
        explicit_edges,
        conflict_graph,
        repo_root=repo_root,
    )


def build_dag_from_plan(
    path: str | Path,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    repo_root: str | Path | None = None,
    ignore_patterns: Iterable[re.Pattern[str]] = (),
) -> dict[str, Any]:
    return build_dag(
        parse_decomposition_plan(path),
        threshold=threshold,
        repo_root=repo_root,
        ignore_patterns=ignore_patterns,
    ).to_dict()


def _updated_subtask(
    subtask: SubTask,
    updated_footprint: Iterable[str] | None,
    updated_symbols: Iterable[str] | None,
) -> SubTask:
    footprint = (
        tuple(updated_footprint) if updated_footprint is not None else subtask.footprint
    )
    symbols = tuple(updated_symbols) if updated_symbols is not None else subtask.symbols
    heuristic_risk, heuristic_reasons = detect_risk_from_values(
        footprint,
        symbols,
        subtask.description,
    )
    return replace(
        subtask,
        footprint=footprint,
        symbols=symbols,
        risk=subtask.risk or heuristic_risk,
        risk_reasons=tuple(dict.fromkeys([*subtask.risk_reasons, *heuristic_reasons])),
    )


def _detect_new_footprint_conflicts(
    conflict_graph: ConflictGraph,
    subtask_id: str,
    previous_pairs: set[frozenset[str]],
    explicit_pairs: set[tuple[str, str]],
) -> list[FootprintConflict]:
    conflicts: list[FootprintConflict] = []
    new_edges_by_pair: dict[frozenset[str], list[ConflictEdge]] = {}
    for edge in conflict_graph.edges:
        if edge.pair not in previous_pairs and subtask_id in edge.pair:
            new_edges_by_pair.setdefault(edge.pair, []).append(edge)

    for pair, edges in sorted(
        new_edges_by_pair.items(), key=lambda item: sorted(item[0])
    ):
        if pair in previous_pairs or subtask_id not in pair:
            continue
        representative = max(
            edges,
            key=lambda edge: (edge.score is not None, edge.score or 0.0),
        )
        other_id = (
            representative.right
            if representative.left == subtask_id
            else representative.left
        )
        if (subtask_id, other_id) in explicit_pairs or (
            other_id,
            subtask_id,
        ) in explicit_pairs:
            continue
        conflicts.append(
            FootprintConflict(
                subtask_id=subtask_id,
                other_subtask_id=other_id,
                similarity=representative.score or 0.0,
                blocked_subtask_id=other_id,
                reason=representative.reason,
                resources=tuple(
                    sorted({resource for edge in edges for resource in edge.resources})
                ),
            )
        )
    return conflicts


def recompute_dag_for_footprint_change(
    subtasks: dict[str, SubTask],
    subtask_id: str,
    updated_footprint: Iterable[str] | None = None,
    updated_symbols: Iterable[str] | None = None,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ignore_patterns: Iterable[re.Pattern[str]] = (),
) -> tuple[DagResult, list[FootprintConflict]]:
    """Recompute the DAG after a running subtask changes its touch set."""
    if subtask_id not in subtasks:
        raise KeyError(f"未知のサブタスクIDです: {subtask_id}")

    ignore_patterns_tuple = tuple(ignore_patterns)
    previous_pairs = {
        edge.pair
        for edge in build_conflict_graph(
            list(subtasks.values()), threshold, ignore_patterns_tuple
        ).edges
    }
    updated_subtasks = dict(subtasks)
    updated_subtasks[subtask_id] = _updated_subtask(
        subtasks[subtask_id],
        updated_footprint,
        updated_symbols,
    )
    subtask_list = list(updated_subtasks.values())
    explicit_edges = _collect_explicit_edges(subtask_list)
    explicit_pairs = {(edge.source, edge.target) for edge in explicit_edges}
    conflict_graph = build_conflict_graph(
        subtask_list, threshold=threshold, ignore_patterns=ignore_patterns_tuple
    )
    conflicts = _detect_new_footprint_conflicts(
        conflict_graph,
        subtask_id,
        previous_pairs,
        explicit_pairs,
    )
    result = _assemble_dag(subtask_list, explicit_edges, conflict_graph)
    return result, conflicts
