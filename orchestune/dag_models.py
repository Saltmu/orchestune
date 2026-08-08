"""Data models shared by DAG parsing and graph operations."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
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


def compile_extra_ignore_patterns(
    patterns: Iterable[str],
) -> tuple[re.Pattern[str], ...]:
    """Compile user-supplied regex strings for use as extra ignore patterns.

    Raises re.error if any pattern is not a valid regular expression.
    """
    return tuple(re.compile(pattern) for pattern in patterns)


def normalize_footprint_path(path: str) -> str:
    """Normalize a footprint path into a canonical repository-relative POSIX path.

    - Converts Windows backslashes '\\' to POSIX forward slashes '/'
    - Rejects absolute paths (starting with '/') and drive-qualified paths (e.g. 'C:')
    - Resolves redundant './', duplicate slashes '//', and relative parent references '..'
    - Raises ValueError if the path is absolute/drive-qualified, resolves to empty/root ('.'), or escapes the repository root
    """
    if not isinstance(path, str):
        path = str(path)

    clean_path = path.replace("\\", "/")

    if re.match(r"^[a-zA-Z]:", clean_path):
        raise ValueError(
            f"Invalid footprint path: '{path}' is a drive-qualified absolute path"
        )
    if clean_path.startswith("/"):
        raise ValueError(f"Invalid footprint path: '{path}' is an absolute path")

    normalized = posixpath.normpath(clean_path)

    if normalized in ("", "."):
        raise ValueError(
            f"Invalid footprint path: '{path}' resolves to empty or root path"
        )

    if normalized == ".." or normalized.startswith("../"):
        raise ValueError(f"Invalid footprint path: '{path}' escapes repository root")

    return normalized


def is_ignored_footprint(
    path: str, extra_patterns: Iterable[re.Pattern[str]] = ()
) -> bool:
    normalized = normalize_footprint_path(path)
    return any(
        pattern.search(normalized)
        for pattern in (*_IGNORED_FOOTPRINT_PATTERNS, *extra_patterns)
    )


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
    issue_number: int | None = None

    def __post_init__(self) -> None:
        normalized = tuple(normalize_footprint_path(p) for p in self.footprint)
        object.__setattr__(self, "footprint", normalized)

    def touch_set(
        self, extra_ignored_patterns: Iterable[re.Pattern[str]] = ()
    ) -> frozenset[str]:
        footprint = frozenset(
            path
            for path in self.footprint
            if not is_ignored_footprint(path, extra_ignored_patterns)
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
