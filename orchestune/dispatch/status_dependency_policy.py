"""Fail-closed dependency completion policy for status promotion."""

from __future__ import annotations

from collections.abc import Iterable

from orchestune.dispatch.dependency_assessment import (
    DependencyAssessment,
    DependencyState,
)


def dependencies_completed(assessment: DependencyAssessment | None) -> bool:
    """Return whether all declared dependencies are resolved and completed."""
    return (
        assessment is not None
        and not assessment.unresolved
        and all(
            dependency.state is DependencyState.COMPLETED
            for dependency in assessment.resolved
        )
    )


def desired_dependency_ids(
    issue_number: int, assessment: DependencyAssessment | None
) -> tuple[str, ...]:
    """Translate an assessment without losing unavailable/unresolved dependencies."""
    if assessment is None:
        return (f"unresolved-dependency:{issue_number}:assessment-unavailable",)
    return (
        *(str(dependency.issue_number) for dependency in assessment.resolved),
        *(
            f"unresolved-dependency:{issue_number}:{index}"
            for index in range(len(assessment.unresolved))
        ),
    )


def completed_dependency_ids(
    assessments: Iterable[DependencyAssessment | None],
) -> frozenset[str]:
    """Collect completed IDs while preserving each assessment's fail-closed policy."""
    completed_ids: set[str] = set()
    for assessment in assessments:
        if assessment is None:
            continue
        completed = (
            assessment.resolved
            if dependencies_completed(assessment)
            else tuple(
                dependency
                for dependency in assessment.resolved
                if dependency.state is DependencyState.COMPLETED
            )
        )
        completed_ids.update(str(dependency.issue_number) for dependency in completed)
    return frozenset(completed_ids)


__all__ = [
    "completed_dependency_ids",
    "dependencies_completed",
    "desired_dependency_ids",
]
