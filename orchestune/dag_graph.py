"""Dependency graph assembly, validation, and runtime recomputation."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from orchestune.dag_contracts import find_unowned_shared_contract_hotspots
from orchestune.dag_models import (
    DagCycleError,
    DagEdge,
    DagResult,
    FootprintConflict,
    SubTask,
)
from orchestune.dag_parsing import detect_risk_from_values, parse_decomposition_plan
from orchestune.dag_similarity import (
    DEFAULT_SIMILARITY_THRESHOLD,
    build_similarity_edges,
)

logger = logging.getLogger(__name__)


def _collect_explicit_edges(subtasks: list[SubTask]) -> list[DagEdge]:
    return [
        DagEdge(source=dependency, target=subtask.id, reason="explicit")
        for subtask in subtasks
        for dependency in subtask.depends_on
    ]


def _merge_explicit_and_similarity(
    explicit_edges: list[DagEdge],
    similarity_edges: list[DagEdge],
) -> list[DagEdge]:
    explicit_pairs = {(edge.source, edge.target) for edge in explicit_edges}
    return [
        *explicit_edges,
        *(
            edge
            for edge in similarity_edges
            if (edge.source, edge.target) not in explicit_pairs
            and (edge.target, edge.source) not in explicit_pairs
        ),
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


def _cycle_edges(cycle: list[str], edges: list[DagEdge]) -> list[DagEdge]:
    by_pair = {(edge.source, edge.target): edge for edge in edges}
    return [
        by_pair[(source, target)]
        for source, target in zip(cycle, cycle[1:], strict=False)
    ]


def _resolve_cycles_if_possible(
    node_ids: list[str],
    edges: list[DagEdge],
) -> list[DagEdge]:
    resolved = list(edges)
    while cycle := _detect_cycle(node_ids, resolved):
        similarity_edges = [
            edge
            for edge in _cycle_edges(cycle, resolved)
            if edge.reason == "similarity"
        ]
        if not similarity_edges:
            raise DagCycleError(f"循環参照を検出しました: {' -> '.join(cycle)}")

        weakest = min(
            similarity_edges,
            key=lambda edge: edge.score if edge.score is not None else 0.0,
        )
        logger.warning(
            "循環参照を検出したため、類似度エッジを自動解消しました: "
            "%s -> %s (reason: %s, score: %s)",
            weakest.source,
            weakest.target,
            weakest.reason,
            weakest.score,
        )
        resolved.remove(weakest)
    return resolved


def _assemble_dag(subtasks: list[SubTask], edges: list[DagEdge]) -> DagResult:
    node_ids = [subtask.id for subtask in subtasks]
    resolved_edges = _resolve_cycles_if_possible(node_ids, edges)
    topological_order = _topological_sort(node_ids, resolved_edges)
    targets = {edge.target for edge in resolved_edges}
    return DagResult(
        subtasks={subtask.id: subtask for subtask in subtasks},
        edges=resolved_edges,
        topological_order=topological_order,
        parallel_leaves=sorted(node for node in node_ids if node not in targets),
        risky_subtask_ids=sorted(subtask.id for subtask in subtasks if subtask.risk),
        warnings=tuple(find_unowned_shared_contract_hotspots(subtasks, resolved_edges)),
    )


def build_dag(
    subtasks: list[SubTask],
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> DagResult:
    """Build a validated DAG from explicit and inferred dependencies."""
    explicit_edges = _collect_explicit_edges(subtasks)
    similarity_edges = build_similarity_edges(subtasks, threshold=threshold)
    return _assemble_dag(
        subtasks,
        _merge_explicit_and_similarity(explicit_edges, similarity_edges),
    )


def build_dag_from_plan(
    path: str | Path,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> dict[str, Any]:
    return build_dag(
        parse_decomposition_plan(path),
        threshold=threshold,
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
    return SubTask(
        id=subtask.id,
        description=subtask.description,
        footprint=footprint,
        symbols=symbols,
        depends_on=subtask.depends_on,
        risk=subtask.risk or heuristic_risk,
        risk_reasons=tuple(dict.fromkeys([*subtask.risk_reasons, *heuristic_reasons])),
        priority=subtask.priority,
    )


def recompute_dag_for_footprint_change(
    subtasks: dict[str, SubTask],
    subtask_id: str,
    updated_footprint: Iterable[str] | None = None,
    updated_symbols: Iterable[str] | None = None,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> tuple[DagResult, list[FootprintConflict]]:
    """Recompute the DAG after a running subtask changes its touch set."""
    if subtask_id not in subtasks:
        raise KeyError(f"未知のサブタスクIDです: {subtask_id}")

    previous_pairs = {
        frozenset((edge.source, edge.target))
        for edge in build_similarity_edges(list(subtasks.values()), threshold=threshold)
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

    conflicts: list[FootprintConflict] = []
    final_similarity_edges: list[DagEdge] = []
    for edge in build_similarity_edges(subtask_list, threshold=threshold):
        pair = frozenset((edge.source, edge.target))
        if (edge.source, edge.target) in explicit_pairs or (
            edge.target,
            edge.source,
        ) in explicit_pairs:
            continue

        if pair not in previous_pairs and subtask_id in pair:
            other_id = edge.target if edge.source == subtask_id else edge.source
            edge = DagEdge(
                source=subtask_id,
                target=other_id,
                reason="similarity",
                score=edge.score,
            )
            conflicts.append(
                FootprintConflict(
                    subtask_id=subtask_id,
                    other_subtask_id=other_id,
                    similarity=edge.score or 0.0,
                    blocked_subtask_id=other_id,
                )
            )
        final_similarity_edges.append(edge)

    result = _assemble_dag(subtask_list, [*explicit_edges, *final_similarity_edges])
    return result, conflicts
