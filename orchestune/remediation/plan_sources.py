"""Layer 2 helpers for resolving subtask acceptance criteria, footprints, and manifest comparisons."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from orchestune.dag.models import SubTask
from orchestune.dag.parsing import reject_retired_model_tier
from orchestune.issue_parsing import (
    DECOMPOSITION_PLAN_BLOCK_PATTERN,
    parse_footprint_block,
    parse_task_from_issue,
)
from orchestune.provisioning.body_parsing import (
    extract_subtask_section,
    parse_bullet_list,
)
from orchestune.provisioning.plan_manifest import project_single_subtask
from orchestune.remediation.models import (
    PreviewProblem,
    RemediationSnapshot,
    RemediationTask,
)


def _extract_child_acceptance(
    child: Any,
    sid: str,
    template: str | None,
    sources: dict[str, list[str]],
    problems: list[PreviewProblem],
) -> None:
    if not template:
        problems.append(
            PreviewProblem(
                "input",
                f"issue template required to extract acceptance criteria from Issue #{child.number}",
            )
        )
        return
    try:
        reject_retired_model_tier(
            (parse_footprint_block(child.body) or {}).get("model_tier")
        )
        section_text = extract_subtask_section(
            template, child.body, "acceptance_criteria"
        )
        for item in parse_bullet_list(section_text):
            sources.setdefault(" ".join(item.split()), []).append(
                f"subtask {sid}: {item}"
            )
    except (ValueError, KeyError) as exc:
        problems.append(
            PreviewProblem(
                "existing",
                f"cannot extract acceptance criteria from Issue #{child.number} for subtask '{sid}': {exc}",
            )
        )


def extract_subtask_acceptance_sources(
    snapshot: RemediationSnapshot,
    entries: list[dict[str, Any]],
    template: str | None = None,
) -> tuple[dict[str, tuple[str, ...]], list[PreviewProblem]]:
    """Extract checklist items from parent and acceptance criteria from child issues or plan entries."""
    sources: dict[str, list[str]] = {}
    problems: list[PreviewProblem] = []

    body = DECOMPOSITION_PLAN_BLOCK_PATTERN.sub("", snapshot.parent.body)
    for line in body.splitlines():
        match = re.fullmatch(r"\s*[-*+]\s+\[[ xX]\]\s+(.+)", line)
        if match:
            sources.setdefault(" ".join(match[1].split()), []).append(
                "parent checklist: " + match[1]
            )

    for entry in entries:
        sid = str(entry.get("id", "")).strip()
        issue_num = entry.get("issue_number")
        if issue_num is not None:
            child = next((c for c in snapshot.children if c.number == issue_num), None)
            if child is None:
                problems.append(
                    PreviewProblem(
                        "existing",
                        f"child Issue #{issue_num} for subtask '{sid}' was not found in snapshot",
                    )
                )
                continue
            _extract_child_acceptance(child, sid, template, sources, problems)
        else:
            values = entry.get("acceptance_criteria", [])
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, str):
                        sources.setdefault(" ".join(value.split()), []).append(
                            f"subtask {sid}: {value}"
                        )

    return {k: tuple(v) for k, v in sources.items()}, problems


def _entry_to_subtask(entry: dict[str, Any], snapshot: RemediationSnapshot) -> SubTask:
    reject_retired_model_tier(entry.get("model_tier"))
    sid = str(entry.get("id", "")).strip()
    issue_num = entry.get("issue_number")
    child = (
        next(
            (
                c
                for c in (*snapshot.children, *snapshot.existing)
                if c.number == issue_num
            ),
            None,
        )
        if issue_num is not None
        else None
    )
    if issue_num is not None and child is None:
        raise ValueError(
            f"child Issue #{issue_num} for subtask '{sid}' not found in snapshot; "
            "cannot determine Footprint for DAG validation"
        )
    if child is not None:
        reject_retired_model_tier(
            (parse_footprint_block(child.body) or {}).get("model_tier")
        )
        parsed = parse_task_from_issue(child)
        footprint = tuple(parsed.footprint or ())
        symbols = tuple(parsed.symbols or ())
    else:
        footprint = tuple(entry.get("footprint") or ())
        symbols = tuple(entry.get("symbols") or ())

    return SubTask(
        id=sid,
        description=str(entry.get("description", "")),
        footprint=footprint,
        symbols=symbols,
        depends_on=tuple(str(d) for d in entry.get("depends_on", ())),
        risk=bool(entry.get("risk", False)),
        risk_reasons=tuple(str(r) for r in entry.get("risk_reasons", ())),
        priority=str(entry.get("priority", "medium")),
        shared_contract=entry.get("shared_contract"),
        writes_shared_contract=bool(entry.get("writes_shared_contract", False)),
        issue_number=issue_num,
        execution_profile=entry.get("execution_profile"),
    )


def build_dag_subtasks(
    entries: list[dict[str, Any]],
    snapshot: RemediationSnapshot,
    spec_tasks: Sequence[RemediationTask],
) -> list[SubTask]:
    """Construct full SubTask models for DAG validation, sourcing footprint from children."""
    spec_map = {t.subtask.id: t.subtask for t in spec_tasks}
    result: list[SubTask] = []
    for entry in entries:
        sid = str(entry.get("id", "")).strip()
        if sid not in spec_map:
            result.append(_entry_to_subtask(entry, snapshot))
    for task in spec_tasks:
        result.append(task.subtask)
    return result


def is_plan_entry_ready(
    entry: dict[str, Any] | None,
    task: RemediationTask,
    issue_number: int,
    parent_number: int,
    spec_revision: str,
) -> bool:
    """Compare an existing plan entry to the expected remediation task via manifest projection."""
    if not entry or not isinstance(entry, dict):
        return False
    reject_retired_model_tier(entry.get("model_tier"))
    expected: dict[str, Any] = {
        "id": task.subtask.id,
        "description": task.subtask.description,
        "issue_number": issue_number,
        "remediation": {
            "parent_issue_number": parent_number,
            "subtask_id": task.subtask.id,
            "spec_revision": spec_revision,
            "kind": task.kind,
            "parent_acceptance": list(task.parent_acceptance),
            "origins": list(task.origins),
            "reason": task.reason,
        },
    }
    if task.subtask.depends_on or "depends_on" in entry:
        expected["depends_on"] = list(task.subtask.depends_on)
    if task.subtask.risk or "risk" in entry:
        expected["risk"] = task.subtask.risk
    if task.subtask.risk_reasons or "risk_reasons" in entry:
        expected["risk_reasons"] = list(task.subtask.risk_reasons)
    if task.subtask.priority != "medium" or "priority" in entry:
        expected["priority"] = task.subtask.priority
    if task.subtask.shared_contract is not None or "shared_contract" in entry:
        expected["shared_contract"] = task.subtask.shared_contract
    if task.subtask.writes_shared_contract or "writes_shared_contract" in entry:
        expected["writes_shared_contract"] = task.subtask.writes_shared_contract
    if task.subtask.execution_profile is not None or "execution_profile" in entry:
        expected["execution_profile"] = task.subtask.execution_profile
    try:
        return project_single_subtask(entry) == project_single_subtask(expected)
    except (ValueError, TypeError):
        return False


__all__ = [
    "build_dag_subtasks",
    "extract_subtask_acceptance_sources",
    "is_plan_entry_ready",
]
