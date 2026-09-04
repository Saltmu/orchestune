"""Load, validate, and revision decomposition-plan generations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestune.dag.models import SubTask
from orchestune.provisioning.plan_loading import load_plan
from orchestune.replan.models import (
    PlanGeneration,
    PlanRevision,
    ReplacementPreview,
    ReplanPlan,
    RetirementCandidate,
    _stable_hash,
)


def _validate_issue_number_ownership(
    subtasks: tuple[SubTask, ...], parent_issue_number: int | None
) -> None:
    values = tuple(
        subtask.issue_number for subtask in subtasks if subtask.issue_number is not None
    )
    if len(set(values)) != len(values):
        raise ValueError("subtask issue_number is duplicated")
    if parent_issue_number is not None and parent_issue_number in values:
        raise ValueError("parent Issue must not alias a subtask issue_number")


def _assert_acyclic(subtasks: tuple[SubTask, ...]) -> None:
    graph = {subtask.id: set(subtask.depends_on) for subtask in subtasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(subtask_id: str) -> None:
        if subtask_id in visiting:
            raise ValueError("known dependency graph contains a cycle")
        if subtask_id in visited:
            return
        visiting.add(subtask_id)
        for dependency in graph[subtask_id]:
            visit(dependency)
        visiting.remove(subtask_id)
        visited.add(subtask_id)

    for subtask_id in graph:
        visit(subtask_id)


def load_replan_plan(path: str | Path) -> ReplanPlan:
    """Load the established plan grammar without Forge or process dependencies."""

    plan_path = Path(path)
    loaded_subtasks, metadata = load_plan(plan_path)
    subtasks = tuple(loaded_subtasks)
    _validate_issue_number_ownership(subtasks, metadata.parent_issue_number)
    _assert_acyclic(subtasks)

    return ReplanPlan(
        title=metadata.title,
        parent_issue_number=metadata.parent_issue_number,
        parent_issue_source=metadata.parent_issue_source,
        subtasks=subtasks,
        description=metadata.description,
    )


def _canonical_subtask(subtask: SubTask) -> dict[str, Any]:
    return {
        "id": subtask.id,
        "description": subtask.description,
        "footprint": sorted(set(subtask.footprint)),
        "symbols": sorted(set(subtask.symbols)),
        "depends_on": sorted(set(subtask.depends_on)),
        "risk": subtask.risk,
        "risk_reasons": sorted(set(subtask.risk_reasons)),
        "priority": subtask.priority,
        "overview": subtask.overview,
        "acceptance_criteria": list(subtask.acceptance_criteria),
        "proposed_changes": list(subtask.proposed_changes),
        "verification_plan": list(subtask.verification_plan),
        "shared_contract": subtask.shared_contract,
        "writes_shared_contract": subtask.writes_shared_contract,
        "execution_profile": subtask.execution_profile,
        "model_tier": subtask.model_tier,
    }


def _canonical_plan(plan: ReplanPlan) -> dict[str, Any]:
    return {
        "title": plan.title,
        "parent_issue_number": plan.parent_issue_number,
        "parent_issue_source": plan.parent_issue_source,
        "description": plan.description,
        "subtasks": [
            _canonical_subtask(subtask)
            for subtask in sorted(plan.subtasks, key=lambda item: item.id)
        ],
    }


def compute_plan_revision(plan: ReplanPlan | str | Path) -> PlanRevision:
    """Return a semantic SHA-256 revision excluding SubIssue number writeback."""

    loaded = load_replan_plan(plan) if isinstance(plan, str | Path) else plan
    return PlanRevision(f"replan-v1:sha256:{_stable_hash(_canonical_plan(loaded))}")


def build_replacement_preview(
    old_plan: ReplanPlan, new_plan: ReplanPlan
) -> ReplacementPreview:
    """Build generation identities and old-plan retirement candidates."""

    revision = compute_plan_revision(new_plan)
    generations = tuple(
        PlanGeneration(revision, subtask.id) for subtask in new_plan.subtasks
    )
    retirements = tuple(
        RetirementCandidate(subtask.id, subtask.issue_number)
        for subtask in old_plan.subtasks
        if subtask.issue_number is not None
    )
    return ReplacementPreview(revision, generations, retirements)


__all__ = [
    "build_replacement_preview",
    "compute_plan_revision",
    "load_replan_plan",
]
