"""Data models shared by DAG parsing and graph operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_IGNORED_FOOTPRINT_PATTERNS = (
    re.compile(r"(^|/)pyproject\.toml$"),
    re.compile(r"(^|/)poetry\.lock$"),
    re.compile(r"(^|/)logging\.py$"),
    re.compile(r"(^|/)logger\.py$"),
    re.compile(r"(^|/)config\.py$"),
    re.compile(r"(^|/)settings\.py$"),
)


def is_ignored_footprint(path: str) -> bool:
    return any(pattern.search(path) for pattern in _IGNORED_FOOTPRINT_PATTERNS)


class DagCycleError(ValueError):
    """Raised when a dependency graph contains an unresolvable cycle."""


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
    overview: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    proposed_changes: tuple[str, ...] = ()
    verification_plan: tuple[str, ...] = ()
    shared_contract: str | None = None
    writes_shared_contract: bool = False

    def touch_set(self) -> frozenset[str]:
        footprint = frozenset(
            path for path in self.footprint if not is_ignored_footprint(path)
        )
        return footprint | frozenset(self.symbols)


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
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "subtasks": {
                subtask_id: {
                    "description": subtask.description,
                    "footprint": list(subtask.footprint),
                    "symbols": list(subtask.symbols),
                    "depends_on": list(subtask.depends_on),
                    "risk": subtask.risk,
                    "risk_reasons": list(subtask.risk_reasons),
                    "overview": subtask.overview,
                    "acceptance_criteria": list(subtask.acceptance_criteria),
                    "proposed_changes": list(subtask.proposed_changes),
                    "verification_plan": list(subtask.verification_plan),
                    "shared_contract": subtask.shared_contract,
                    "writes_shared_contract": subtask.writes_shared_contract,
                }
                for subtask_id, subtask in self.subtasks.items()
            },
            "edges": [
                {
                    "source": edge.source,
                    "target": edge.target,
                    "reason": edge.reason,
                    "score": edge.score,
                }
                for edge in self.edges
            ],
            "topological_order": list(self.topological_order),
            "parallel_leaves": list(self.parallel_leaves),
            "risky_subtask_ids": list(self.risky_subtask_ids),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class FootprintConflict:
    subtask_id: str
    other_subtask_id: str
    similarity: float
    blocked_subtask_id: str
