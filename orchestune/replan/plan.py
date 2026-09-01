"""Load, validate, and semantically revision decomposition plans."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from orchestune.dag.models import SubTask
from orchestune.dag.parsing import (
    extract_frontmatter_and_body,
    parse_decomposition_plan,
)
from orchestune.replan.models import (
    EndpointRef,
    ExternalDependency,
    PlanRevision,
    ReplanPlan,
    _stable_hash,
)
from orchestune.validation import validate_issue_number

_ENDPOINT_KEYS = frozenset(("subtask_id", "issue_number"))
_EDGE_KEYS = frozenset(("blocked", "blocker"))
_PARENT_ISSUE_SOURCES = frozenset(("adopted", "derived"))


def _load_semantic_plan(
    path: Path,
) -> tuple[tuple[SubTask, ...], dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    raw, description = extract_frontmatter_and_body(text)
    parsed_subtasks = parse_decomposition_plan(path)
    issue_numbers = {
        str(entry["id"]).strip(): validate_issue_number(
            cast(int | str, entry["issue_number"])
        )
        for entry in raw.get("subtasks") or []
        if isinstance(entry, dict) and entry.get("issue_number") not in (None, "")
    }
    subtasks = tuple(
        dataclasses.replace(subtask, issue_number=issue_numbers.get(subtask.id))
        for subtask in parsed_subtasks
    )
    return subtasks, raw, description


def _parent_metadata(raw: Mapping[str, Any]) -> tuple[int | None, str | None]:
    raw_parent = raw.get("parent_issue_number")
    parent_number = (
        None
        if raw_parent in (None, "")
        else validate_issue_number(cast(int | str, raw_parent))
    )
    raw_source = raw.get("parent_issue_source")
    parent_source = str(raw_source).strip() if raw_source not in (None, "") else None
    if parent_source not in (None, *_PARENT_ISSUE_SOURCES):
        raise ValueError(
            "decomposition_plan.md parent_issue_source must be adopted or derived"
        )
    if parent_source == "adopted" and parent_number is None:
        raise ValueError("adopted decomposition plan requires parent_issue_number")
    return parent_number, parent_source


def _parse_endpoint(raw: object, *, field: str) -> EndpointRef:
    if not isinstance(raw, Mapping):
        raise ValueError(f"external dependency {field} endpoint must be a mapping")
    unknown = set(raw) - _ENDPOINT_KEYS
    if unknown:
        raise ValueError(
            f"external dependency {field} endpoint has unknown keys: {sorted(unknown)}"
        )
    present = [key for key in _ENDPOINT_KEYS if key in raw]
    if len(present) != 1:
        raise ValueError(
            f"external dependency {field} endpoint requires exactly one of "
            "subtask_id or issue_number"
        )
    key = present[0]
    value = raw[key]
    if key == "subtask_id":
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"external dependency {field} subtask_id is invalid")
        return EndpointRef(subtask_id=value)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(
            f"external dependency {field} issue_number must be a positive integer"
        )
    return EndpointRef(issue_number=value)


def _parse_external_dependencies(raw: object) -> tuple[ExternalDependency, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise ValueError("external_dependencies must be a list")
    dependencies: list[ExternalDependency] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValueError(f"external_dependencies[{index}] must be a mapping")
        unknown = set(item) - _EDGE_KEYS
        if unknown or set(item) != _EDGE_KEYS:
            raise ValueError(
                f"external_dependencies[{index}] requires only blocked and blocker"
            )
        dependencies.append(
            ExternalDependency(
                blocked=_parse_endpoint(item["blocked"], field="blocked"),
                blocker=_parse_endpoint(item["blocker"], field="blocker"),
            )
        )
    return tuple(dependencies)


def _validate_external_dependencies(
    dependencies: tuple[ExternalDependency, ...],
    subtasks: tuple[SubTask, ...],
    parent_issue_number: int | None,
) -> None:
    subtask_ids = {subtask.id for subtask in subtasks}
    internal_issue_numbers = {
        subtask.issue_number for subtask in subtasks if subtask.issue_number is not None
    }
    seen_edges: set[tuple[tuple[str, str | int], tuple[str, str | int]]] = set()
    for dependency in dependencies:
        endpoints = (dependency.blocked, dependency.blocker)
        internal_count = sum(item.subtask_id is not None for item in endpoints)
        if internal_count != 1:
            raise ValueError(
                "external dependency must connect one internal SubTask and one external Issue"
            )
        for endpoint in endpoints:
            if (
                endpoint.subtask_id is not None
                and endpoint.subtask_id not in subtask_ids
            ):
                raise ValueError(
                    f"external dependency references unknown subtask: {endpoint.subtask_id}"
                )
            if (
                parent_issue_number is not None
                and endpoint.issue_number == parent_issue_number
            ):
                raise ValueError(
                    "external dependency must not reference the parent Issue"
                )
            if endpoint.issue_number in internal_issue_numbers:
                raise ValueError(
                    "external dependency issue_number aliases an internal SubTask Issue"
                )
        if dependency.key in seen_edges:
            raise ValueError("external dependency edge is duplicated")
        seen_edges.add(dependency.key)


def _assert_acyclic(
    subtasks: tuple[SubTask, ...], dependencies: tuple[ExternalDependency, ...]
) -> None:
    graph: dict[tuple[str, str | int], set[tuple[str, str | int]]] = {}
    for subtask in subtasks:
        blocked = ("subtask_id", subtask.id)
        graph.setdefault(blocked, set()).update(
            ("subtask_id", dependency) for dependency in subtask.depends_on
        )
    for dependency in dependencies:
        graph.setdefault(dependency.blocked.key, set()).add(dependency.blocker.key)
        graph.setdefault(dependency.blocker.key, set())

    visiting: set[tuple[str, str | int]] = set()
    visited: set[tuple[str, str | int]] = set()

    def visit(node: tuple[str, str | int]) -> None:
        if node in visiting:
            raise ValueError("known dependency graph contains a cycle")
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph.get(node, ()):
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)


def load_replan_plan(path: str | Path) -> ReplanPlan:
    """Load the existing plan grammar plus strict replan external edges."""

    plan_path = Path(path)
    subtasks, raw, description = _load_semantic_plan(plan_path)
    parent_issue_number, parent_issue_source = _parent_metadata(raw)
    dependencies = (
        _parse_external_dependencies(raw["external_dependencies"])
        if "external_dependencies" in raw
        else ()
    )
    _validate_external_dependencies(dependencies, subtasks, parent_issue_number)
    _assert_acyclic(subtasks, dependencies)
    return ReplanPlan(
        title=str(raw.get("title") or "").strip(),
        parent_issue_number=parent_issue_number,
        parent_issue_source=parent_issue_source,
        subtasks=subtasks,
        external_dependencies=tuple(sorted(dependencies, key=lambda item: item.key)),
        description=description,
    )


def _canonical_subtask(subtask: SubTask) -> dict[str, Any]:
    return {
        "id": subtask.id,
        "description": subtask.description,
        "overview": subtask.overview,
        "proposed_changes": list(subtask.proposed_changes),
        "acceptance_criteria": list(subtask.acceptance_criteria),
        "verification_plan": list(subtask.verification_plan),
        "footprint": sorted(set(subtask.footprint)),
        "symbols": sorted(set(subtask.symbols)),
        "depends_on": sorted(set(subtask.depends_on)),
        "priority": subtask.priority,
        "risk": subtask.risk,
        "shared_contract": subtask.shared_contract,
        "writes_shared_contract": subtask.writes_shared_contract,
        "execution_profile": subtask.execution_profile,
        "model_tier": subtask.model_tier,
    }


def _canonical_endpoint(endpoint: EndpointRef) -> dict[str, str | int]:
    key, value = endpoint.key
    return {key: value}


def _canonical_plan(plan: ReplanPlan) -> dict[str, Any]:
    return {
        "title": plan.title,
        "description": plan.description,
        "parent_issue_number": plan.parent_issue_number,
        "parent_issue_source": plan.parent_issue_source,
        "subtasks": [
            _canonical_subtask(subtask)
            for subtask in sorted(plan.subtasks, key=lambda item: item.id)
        ],
        "external_dependencies": [
            {
                "blocked": _canonical_endpoint(dependency.blocked),
                "blocker": _canonical_endpoint(dependency.blocker),
            }
            for dependency in sorted(
                plan.external_dependencies, key=lambda item: item.key
            )
        ],
    }


def compute_plan_revision(plan: ReplanPlan | str | Path) -> PlanRevision:
    """Return the SHA-256 revision of the parsed semantic model."""

    loaded = load_replan_plan(plan) if isinstance(plan, str | Path) else plan
    digest = _stable_hash(_canonical_plan(loaded))
    return PlanRevision(f"replan-v1:sha256:{digest}")


__all__ = ["compute_plan_revision", "load_replan_plan"]
