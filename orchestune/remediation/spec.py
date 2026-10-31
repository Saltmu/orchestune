"""Validate remediation frontmatter using the established subtask grammar."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import yaml

from orchestune.dag.parsing import (
    _parse_subtask,
    extract_frontmatter,
    reject_retired_model_tier,
)
from orchestune.provisioning.body_parsing import validate_reversible_subtask
from orchestune.remediation.models import RemediationSpec, RemediationTask

_REMEDIATION_KEYS = {"kind", "parent_acceptance", "origins", "reason"}
_SUBTASK_KEYS = {
    "id",
    "description",
    "footprint",
    "symbols",
    "depends_on",
    "risk",
    "priority",
    "overview",
    "acceptance_criteria",
    "proposed_changes",
    "verification_plan",
    "shared_contract",
    "writes_shared_contract",
    "execution_profile",
    "issue_number",
}


def positive_number(value: object, field: str) -> int:
    """Require a positive integer, rejecting YAML booleans and coercions."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _texts(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty list")
    return tuple(_text(item, field) for item in value)


def _task(raw: object) -> RemediationTask:
    if not isinstance(raw, dict):
        raise ValueError("subtask must be a mapping")
    reject_retired_model_tier(raw.get("model_tier"))
    raw = {k: v for k, v in raw.items() if k != "model_tier"}
    if unknown := raw.keys() - _SUBTASK_KEYS - _REMEDIATION_KEYS:
        raise ValueError(f"unknown task keys: {sorted(map(str, unknown))}")
    subtask = _parse_subtask({k: v for k, v in raw.items() if k in _SUBTASK_KEYS})
    if "issue_number" in raw:
        subtask = replace(
            subtask, issue_number=positive_number(raw["issue_number"], "issue_number")
        )
    return RemediationTask(
        subtask,
        _text(raw.get("kind"), "kind"),
        _texts(raw.get("parent_acceptance"), "parent_acceptance"),
        _texts(raw.get("origins"), "origins"),
        _text(raw.get("reason"), "reason"),
    )


def stable_hash(value: object) -> str:
    """Hash normalized JSON without depending on mapping insertion order."""
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _canonical(task: RemediationTask) -> dict[str, Any]:
    result = asdict(task)
    result["subtask"].pop("issue_number")
    for key in ("footprint", "symbols", "depends_on", "risk_reasons"):
        result["subtask"][key] = sorted(set(result["subtask"][key]))
    return result


def _validate_internal_cycles(tasks: tuple[RemediationTask, ...]) -> None:
    graph = {task.subtask.id: task.subtask.depends_on for task in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise ValueError("spec dependency graph contains a cycle")
        if name in visited:
            return
        visiting.add(name)
        for dependency in graph[name]:
            if dependency in graph:
                visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in graph:
        visit(name)


def load_remediation_spec(path: str | Path) -> RemediationSpec:
    """Read and validate a spec without writing it or contacting a Forge."""
    try:
        raw = extract_frontmatter(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid spec YAML: {exc}") from exc
    if unknown := raw.keys() - {"parent_issue_number", "subtasks"}:
        raise ValueError(f"unknown top-level keys: {sorted(map(str, unknown))}")
    parent = (
        positive_number(raw["parent_issue_number"], "parent_issue_number")
        if "parent_issue_number" in raw
        else None
    )
    entries = raw.get("subtasks")
    if not isinstance(entries, list) or not entries:
        raise ValueError("subtasks must be a non-empty list")
    tasks = tuple(_task(entry) for entry in entries)
    for t in tasks:
        validate_reversible_subtask(t.subtask)
    ids = [task.subtask.id for task in tasks]
    numbers = [
        t.subtask.issue_number for t in tasks if t.subtask.issue_number is not None
    ]
    if (
        len(set(ids)) != len(ids)
        or len(set(numbers)) != len(numbers)
        or parent in numbers
    ):
        raise ValueError(
            "duplicate subtask id/issue_number or parent aliases a subtask"
        )
    _validate_internal_cycles(tasks)
    revision = "sha256:" + stable_hash(
        {
            "parent_issue_number": parent,
            "subtasks": [
                _canonical(t) for t in sorted(tasks, key=lambda t: t.subtask.id)
            ],
        }
    )
    return RemediationSpec(parent, tasks, revision)
