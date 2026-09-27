"""Immutable contracts for decomposition-plan generation replacement."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from orchestune.dag.models import SubTask
from orchestune.plan_identity import PlanGeneration, PlanRevision


def _stable_hash(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _positive_issue_numbers(
    values: tuple[int, ...], *, field_name: str
) -> tuple[int, ...]:
    normalized = tuple(values)
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in normalized
    ):
        raise ValueError(f"{field_name} must contain positive Issue numbers")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicate Issue numbers")
    return tuple(sorted(normalized))


@dataclass(frozen=True)
class ReplanPlan:
    """Pure, validated decomposition-plan representation."""

    title: str
    parent_issue_number: int | None
    parent_issue_source: str | None
    subtasks: tuple[SubTask, ...]
    description: str = ""


@dataclass(frozen=True)
class RetirementCandidate:
    """An old-generation Issue recorded in the parent decomposition plan."""

    subtask_id: str
    issue_number: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.subtask_id, str)
            or not self.subtask_id.strip()
            or self.subtask_id != self.subtask_id.strip()
        ):
            raise ValueError("subtask_id must be a non-empty, trimmed string")
        if (
            isinstance(self.issue_number, bool)
            or not isinstance(self.issue_number, int)
            or self.issue_number <= 0
        ):
            raise ValueError("issue_number must be a positive integer")


@dataclass(frozen=True)
class ReplacementPreview:
    """Pure preview inputs shared by the replacement workflow."""

    plan_revision: PlanRevision
    generations: tuple[PlanGeneration, ...] = ()
    retirement_candidates: tuple[RetirementCandidate, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.plan_revision, PlanRevision):
            object.__setattr__(
                self, "plan_revision", PlanRevision(str(self.plan_revision))
            )
        generations = tuple(self.generations)
        retirements = tuple(self.retirement_candidates)
        if any(not isinstance(item, PlanGeneration) for item in generations):
            raise TypeError("generations must contain PlanGeneration values")
        if any(not isinstance(item, RetirementCandidate) for item in retirements):
            raise TypeError(
                "retirement_candidates must contain RetirementCandidate values"
            )
        if any(item.plan_revision != self.plan_revision for item in generations):
            raise ValueError("all generations must use the preview plan_revision")
        generation_ids = [item.subtask_id for item in generations]
        if len(set(generation_ids)) != len(generation_ids):
            raise ValueError("generations must not contain duplicate subtask IDs")
        retirement_numbers = [item.issue_number for item in retirements]
        if len(set(retirement_numbers)) != len(retirement_numbers):
            raise ValueError(
                "retirement_candidates must not contain duplicate Issue numbers"
            )
        object.__setattr__(
            self,
            "generations",
            tuple(sorted(generations, key=lambda item: item.subtask_id)),
        )
        object.__setattr__(
            self,
            "retirement_candidates",
            tuple(sorted(retirements, key=lambda item: item.subtask_id)),
        )


@dataclass(frozen=True)
class ReplacementResult:
    """Deterministic audit result of a generation replacement."""

    plan_revision: PlanRevision
    created_issue_numbers: tuple[int, ...] = ()
    reused_issue_numbers: tuple[int, ...] = ()
    retired_issue_numbers: tuple[int, ...] = ()
    degraded: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.plan_revision, PlanRevision):
            object.__setattr__(
                self, "plan_revision", PlanRevision(str(self.plan_revision))
            )
        created = _positive_issue_numbers(
            self.created_issue_numbers, field_name="created_issue_numbers"
        )
        reused = _positive_issue_numbers(
            self.reused_issue_numbers, field_name="reused_issue_numbers"
        )
        retired = _positive_issue_numbers(
            self.retired_issue_numbers, field_name="retired_issue_numbers"
        )
        if set(created) & set(reused):
            raise ValueError("created and reused Issue numbers must not overlap")
        if (set(created) | set(reused)) & set(retired):
            raise ValueError(
                "new-generation and retired Issue numbers must not overlap"
            )
        object.__setattr__(self, "created_issue_numbers", created)
        object.__setattr__(self, "reused_issue_numbers", reused)
        object.__setattr__(self, "retired_issue_numbers", retired)


__all__ = [
    "PlanGeneration",
    "PlanRevision",
    "ReplacementPreview",
    "ReplacementResult",
    "ReplanPlan",
    "RetirementCandidate",
]
