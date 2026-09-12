"""Temporary adapter for confirmed same-cycle dependency completions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from orchestune.dispatch.dependency_assessment import (
    AssessedDependency,
    DependencyAssessment,
    DependencyState,
)
from orchestune.dispatch.dependency_policy import DependencyPolicyView


@dataclass(frozen=True, slots=True)
class _ConfirmedCompletionsView:
    source: DependencyPolicyView
    confirmed: frozenset[int]

    def assess_dependencies(self, issue_number: int) -> DependencyAssessment | None:
        assessment = self.source.assess_dependencies(issue_number)
        if assessment is None:
            return None
        return DependencyAssessment(
            resolved=tuple(
                AssessedDependency(
                    dependency.issue_number,
                    DependencyState.COMPLETED
                    if dependency.issue_number in self.confirmed
                    else dependency.state,
                )
                for dependency in assessment.resolved
            ),
            unresolved=assessment.unresolved,
        )

    def canonical_branch(self, issue_number: int) -> str | None:
        return self.source.canonical_branch(issue_number)


def with_confirmed_completions(
    view: DependencyPolicyView, completed_issue_numbers: Iterable[int]
) -> DependencyPolicyView:
    """Overlay explicitly verified completions without mutating or caching the view."""
    return _ConfirmedCompletionsView(view, frozenset(completed_issue_numbers))


__all__ = ["with_confirmed_completions"]
