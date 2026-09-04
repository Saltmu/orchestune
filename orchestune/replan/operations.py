"""Idempotent mutation operations used by the replan apply workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from orchestune import STATUS_LABEL_PREFIX, StatusLabel
from orchestune.dag.models import SubTask
from orchestune.forge import RelationshipUnavailableError
from orchestune.models import IssueRecord
from orchestune.plan_writer import write_issue_numbers
from orchestune.provisioning.rendering import (
    build_subtask_issue_body,
    derive_subtask_labels,
    subtask_issue_title,
)
from orchestune.replan.audit import (
    comments_contain_marker,
    render_retirement_audit,
    retirement_marker,
)
from orchestune.replan.models import PlanGeneration, PlanRevision, RetirementCandidate

_RELATIONSHIP_DEGRADED = (
    RelationshipUnavailableError,
    AttributeError,
    NotImplementedError,
)


class ReplanOperationForge(Protocol):
    def get_issue(self, issue_number: int | str) -> IssueRecord | None: ...

    def create_issue(
        self, title: str, body: str, labels: tuple[str, ...] = ()
    ) -> int: ...

    def add_sub_issue(
        self, parent_issue_number: int | str, child_issue_number: int | str
    ) -> None: ...

    def remove_sub_issue(
        self, parent_issue_number: int | str, child_issue_number: int | str
    ) -> None: ...

    def set_blocked_by(
        self, issue_number: int | str, blocking_issue_number: int | str
    ) -> None: ...

    def list_comments(self, issue_number: int | str) -> list[dict[str, Any]]: ...

    def add_comment(self, issue_number: int | str, body: str) -> None: ...

    def get_issue_labels(self, issue_number: int | str) -> tuple[str, ...]: ...

    def add_label(self, issue_number: int | str, label: str) -> None: ...

    def remove_label(self, issue_number: int | str, label: str) -> None: ...

    def get_issue_state(self, issue_number: int | str) -> str: ...

    def close_issue(
        self, issue_number: int | str, reason: str, comment: str | None = None
    ) -> None: ...


@dataclass(frozen=True)
class GenerationOperationResult:
    issue_number: int
    created: bool


@dataclass(frozen=True)
class RetirementOperationResult:
    issue_number: int
    retired: bool
    degraded: bool


def _validated_existing_generation(
    forge: ReplanOperationForge, generation: PlanGeneration, issue_number: int
) -> int:
    issue = forge.get_issue(issue_number)
    if issue is None or not generation.matches_body(issue.body):
        raise ValueError(
            f"Issue #{issue_number} does not match generation {generation.subtask_id!r}"
        )
    return issue_number


def create_replan_generation(
    forge: ReplanOperationForge,
    subtask: SubTask,
    generation: PlanGeneration,
    *,
    template: str,
    repo_root: Path,
    plan_path: str | Path,
    parent_issue_number: int,
    existing_issue_number: int | None = None,
) -> GenerationOperationResult:
    """Create or reuse exactly one marked generation and persist its number."""

    if generation.subtask_id != subtask.id:
        raise ValueError("generation and SubTask IDs must match")
    created = existing_issue_number is None
    if existing_issue_number is None:
        body = build_subtask_issue_body(
            subtask,
            template,
            repo_root,
            parent_issue_number,
            generation=generation,
        )
        issue_number = forge.create_issue(
            subtask_issue_title(subtask),
            body,
            labels=derive_subtask_labels(subtask, dependencies_done=False),
        )
    else:
        issue_number = _validated_existing_generation(
            forge, generation, existing_issue_number
        )
    write_issue_numbers(plan_path, {subtask.id: issue_number})
    return GenerationOperationResult(issue_number, created)


def link_replan_generation(
    forge: ReplanOperationForge,
    parent_issue_number: int,
    issue_number: int,
    dependency_issue_numbers: tuple[int, ...],
) -> bool:
    """Ensure native generation relationships, returning degraded status."""

    degraded = False
    try:
        forge.add_sub_issue(parent_issue_number, issue_number)
    except _RELATIONSHIP_DEGRADED:
        degraded = True
    for dependency_number in dependency_issue_numbers:
        try:
            forge.set_blocked_by(issue_number, dependency_number)
        except _RELATIONSHIP_DEGRADED:
            degraded = True
    return degraded


def _ensure_retirement_comment(
    forge: ReplanOperationForge,
    candidate: RetirementCandidate,
    revision: PlanRevision,
    replacements: tuple[int, ...],
) -> None:
    marker = retirement_marker(revision)
    if not comments_contain_marker(forge.list_comments(candidate.issue_number), marker):
        forge.add_comment(
            candidate.issue_number,
            render_retirement_audit(revision, replacement_issue_numbers=replacements),
        )


def _transition_to_not_needed(forge: ReplanOperationForge, issue_number: int) -> None:
    labels = forge.get_issue_labels(issue_number)
    if StatusLabel.NOT_NEEDED not in labels:
        forge.add_label(issue_number, StatusLabel.NOT_NEEDED)
    for label in labels:
        if label.startswith(STATUS_LABEL_PREFIX) and label != StatusLabel.NOT_NEEDED:
            forge.remove_label(issue_number, label)


def retire_replan_generation(
    forge: ReplanOperationForge,
    parent_issue_number: int,
    candidate: RetirementCandidate,
    plan_revision: PlanRevision,
    *,
    replacement_issue_numbers: tuple[int, ...],
) -> RetirementOperationResult:
    """Retire one old Issue without altering its title, body, or Footprint."""

    _ensure_retirement_comment(
        forge, candidate, plan_revision, replacement_issue_numbers
    )
    _transition_to_not_needed(forge, candidate.issue_number)
    if forge.get_issue_state(candidate.issue_number).upper() != "CLOSED":
        forge.close_issue(candidate.issue_number, "not planned")
    degraded = False
    try:
        forge.remove_sub_issue(parent_issue_number, candidate.issue_number)
    except _RELATIONSHIP_DEGRADED:
        degraded = True
    return RetirementOperationResult(candidate.issue_number, True, degraded)


__all__ = [
    "GenerationOperationResult",
    "RetirementOperationResult",
    "ReplanOperationForge",
    "create_replan_generation",
    "link_replan_generation",
    "retire_replan_generation",
]
