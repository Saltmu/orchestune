"""Shared fail-closed policy for selecting dependency stack targets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from orchestune.dispatch.dependency_assessment import (
    DependencyAssessment,
    DependencyState,
)


class DependencyPolicyView(Protocol):
    """Purpose-specific read-only input for stack and pending decisions."""

    def assess_dependencies(self, issue_number: int) -> DependencyAssessment | None: ...

    def canonical_branch(self, issue_number: int) -> str | None: ...


@dataclass(frozen=True, slots=True)
class StackTarget:
    issue_number: int
    branch: str


@dataclass(frozen=True, slots=True)
class StackDecision:
    target: StackTarget | None
    reason: str | None
    blocking_issue_number: int | None = None
    assessment: DependencyAssessment | None = None


def _rejected(
    reason: str,
    blocking_issue_number: int,
    assessment: DependencyAssessment | None,
) -> StackDecision:
    return StackDecision(
        target=None,
        reason=reason,
        blocking_issue_number=blocking_issue_number,
        assessment=assessment,
    )


def _decide_grand_target(
    dependency_issue: int,
    source_assessment: DependencyAssessment,
    view: DependencyPolicyView,
) -> StackDecision:
    grand_assessment = view.assess_dependencies(dependency_issue)
    if grand_assessment is None:
        return _rejected(
            "grand-assessment-unavailable", dependency_issue, grand_assessment
        )
    if grand_assessment.unresolved:
        return _rejected(
            "grand-unresolved-dependency", dependency_issue, grand_assessment
        )
    if any(
        dependency.state is not DependencyState.COMPLETED
        for dependency in grand_assessment.resolved
    ):
        return _rejected(
            "grand-dependency-incomplete", dependency_issue, grand_assessment
        )

    branch = view.canonical_branch(dependency_issue)
    if not branch:
        return _rejected("branch-unavailable", dependency_issue, grand_assessment)
    return StackDecision(
        target=StackTarget(dependency_issue, branch),
        reason=None,
        assessment=source_assessment,
    )


def decide_stack_target(issue_number: int, view: DependencyPolicyView) -> StackDecision:
    """Return the only safe direct dependency branch, or a diagnostic rejection."""
    assessment = view.assess_dependencies(issue_number)
    if assessment is None:
        return _rejected("assessment-unavailable", issue_number, None)
    if assessment.unresolved:
        return _rejected("unresolved-dependency", issue_number, assessment)
    if not assessment.resolved or all(
        dependency.state is DependencyState.COMPLETED
        for dependency in assessment.resolved
    ):
        return _rejected("no-stack-dependency", issue_number, assessment)

    not_ci_passed = [
        dependency.issue_number
        for dependency in assessment.resolved
        if dependency.state
        in (DependencyState.WAITING, DependencyState.CHANGES_REQUESTED)
    ]
    if not_ci_passed:
        return _rejected("dependency-not-ci-passed", min(not_ci_passed), assessment)

    candidates = [
        dependency.issue_number
        for dependency in assessment.resolved
        if dependency.state is DependencyState.CI_PASSED_UNMERGED
    ]
    if len(candidates) != 1:
        return _rejected("multiple-stack-dependencies", issue_number, assessment)
    dependency_issue = candidates[0]
    if dependency_issue == issue_number:
        return _rejected("self-dependency", issue_number, assessment)
    return _decide_grand_target(dependency_issue, assessment, view)


def has_pending_dependencies(assessment: DependencyAssessment | None) -> bool:
    """Return whether dependency completion is unknown or still outstanding."""
    return (
        assessment is None
        or bool(assessment.unresolved)
        or any(
            dependency.state is not DependencyState.COMPLETED
            for dependency in assessment.resolved
        )
    )


__all__ = [
    "DependencyPolicyView",
    "StackDecision",
    "StackTarget",
    "decide_stack_target",
    "has_pending_dependencies",
]
