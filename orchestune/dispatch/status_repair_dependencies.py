"""Fresh dependency assessment boundary for status repair execution."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from orchestune.consistency.desired import TaskLifecycle
from orchestune.dispatch.dependency_assessment import (
    AssessedDependency,
    DependencyAssessment,
    DependencyState,
    assess_dependencies,
)
from orchestune.dispatch.dependency_resolution import (
    TaskDependencies,
    resolve_task_dependencies,
)
from orchestune.forge import IssueForge
from orchestune.issue_parsing import parse_task_from_issue
from orchestune.labels import StatusLabel
from orchestune.models import Task

_TERMINAL_LIFECYCLES = (TaskLifecycle.DONE, TaskLifecycle.NOT_NEEDED)


class CompletionEvidenceView(Protocol):
    """Minimal source of verified same-cycle or prior-merge completion evidence."""

    def is_completion_confirmed(self, issue_number: int) -> bool: ...


class DependencyAssessmentView(CompletionEvidenceView, Protocol):
    def assess_dependencies(self, issue_number: int) -> DependencyAssessment | None: ...


@dataclass(frozen=True, slots=True)
class ConfirmedCompletionView:
    """Overlay explicitly verified completions on a semantic dependency view."""

    source: DependencyAssessmentView
    confirmed: frozenset[int] = frozenset()

    def is_completion_confirmed(self, issue_number: int) -> bool:
        return issue_number in self.confirmed or self.source.is_completion_confirmed(
            issue_number
        )

    def assess_dependencies(self, issue_number: int) -> DependencyAssessment | None:
        assessment = self.source.assess_dependencies(issue_number)
        if assessment is None:
            return None
        return DependencyAssessment(
            resolved=tuple(
                AssessedDependency(
                    dependency.issue_number,
                    DependencyState.COMPLETED
                    if self.is_completion_confirmed(dependency.issue_number)
                    else dependency.state,
                )
                for dependency in assessment.resolved
            ),
            unresolved=assessment.unresolved,
        )

    def with_confirmed(self, issue_numbers: Iterable[int]) -> ConfirmedCompletionView:
        return ConfirmedCompletionView(
            self.source, self.confirmed | frozenset(issue_numbers)
        )


@dataclass(frozen=True, slots=True)
class FreshDependencyEvaluation:
    """Fresh subject, identity resolution, and lifecycle assessment as one value."""

    task: Task
    dependencies: TaskDependencies
    assessment: DependencyAssessment


def task_lifecycle(
    status_labels: tuple[str, ...], *, completed: bool = False
) -> TaskLifecycle:
    """Resolve lifecycle with an explicit confirmed-completion override."""
    if completed:
        return TaskLifecycle.DONE
    if StatusLabel.DONE in status_labels and StatusLabel.QUEUED in status_labels:
        return TaskLifecycle.OPEN
    if StatusLabel.DONE in status_labels:
        return TaskLifecycle.DONE
    if StatusLabel.NOT_NEEDED in status_labels:
        return TaskLifecycle.NOT_NEEDED
    if any(
        label in status_labels
        for label in (
            StatusLabel.BLOCKED_HUMAN_REVIEW,
            StatusLabel.MANUAL_MERGE_REQUIRED,
        )
    ):
        return TaskLifecycle.HUMAN_REVIEW
    return TaskLifecycle.OPEN


@dataclass(frozen=True, slots=True)
class _FreshDependencyStateView:
    """Classify only terminal completion; status repair does not inspect PR state."""

    completion_evidence: CompletionEvidenceView
    labels_by_issue: Mapping[int, tuple[str, ...]]

    def is_effectively_done(self, issue_number: int) -> bool:
        if self.completion_evidence.is_completion_confirmed(issue_number):
            return True
        return task_lifecycle(self.labels_by_issue.get(issue_number, ())) in (
            _TERMINAL_LIFECYCLES
        )

    def has_changes_requested(self, issue_number: int) -> bool:
        return False

    def is_ci_passed(self, issue_number: int) -> bool:
        return False


def evaluate_fresh_dependencies(
    task: Task,
    tasks_by_issue: Mapping[int, Task],
    *,
    completion_evidence: CompletionEvidenceView,
    forge: IssueForge,
) -> FreshDependencyEvaluation | None:
    """Re-fetch the subject and assess dependencies against live completion state."""
    issue = forge.get_issue(task.issue_number)
    if issue is None:
        return None
    fresh_task = parse_task_from_issue(issue)
    if fresh_task.issue_state.upper() != "OPEN":
        return None
    fresh_tasks = dict(tasks_by_issue)
    fresh_tasks[fresh_task.issue_number] = fresh_task
    dependencies = resolve_task_dependencies(fresh_task, fresh_tasks)
    labels_by_issue = {
        issue_number: tuple(forge.get_issue_labels(issue_number))
        for issue_number in dependencies.resolved
        if not completion_evidence.is_completion_confirmed(issue_number)
    }
    assessment = assess_dependencies(
        dependencies,
        _FreshDependencyStateView(completion_evidence, labels_by_issue),
    )
    return FreshDependencyEvaluation(fresh_task, dependencies, assessment)


__all__ = [
    "CompletionEvidenceView",
    "ConfirmedCompletionView",
    "DependencyAssessmentView",
    "FreshDependencyEvaluation",
    "evaluate_fresh_dependencies",
    "task_lifecycle",
]
