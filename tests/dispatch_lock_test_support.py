"""Explicit dependency view for lower-level external-lock tests."""

from dataclasses import dataclass

from orchestune.branch_naming import build_task_branch_name
from orchestune.dispatch.dependency_assessment import (
    DependencyAssessment,
    assess_dependencies,
)
from orchestune.dispatch.dependency_resolution import (
    TaskDependencies,
    resolve_all_dependencies,
)
from orchestune.labels import StatusLabel
from orchestune.models import Task
from orchestune.task_branch_resolution import TaskBranchResolution
from orchestune.task_metadata import TaskMetadata


@dataclass(frozen=True)
class LockDependencyTestView:
    """Match the label-only dependency facts used by legacy unit fixtures."""

    tasks_by_issue: dict[int, Task]
    dependency_resolution: dict[int, TaskDependencies]

    @classmethod
    def from_tasks(cls, tasks: list[Task]) -> "LockDependencyTestView":
        tasks_by_issue = {task.issue_number: task for task in tasks}
        return cls(tasks_by_issue, resolve_all_dependencies(tasks_by_issue))

    def task(self, issue_number: int) -> TaskMetadata | None:
        return self.tasks_by_issue.get(issue_number)

    def is_effectively_done(self, issue_number: int) -> bool:
        task = self.tasks_by_issue.get(issue_number)
        return task is not None and bool(
            {StatusLabel.DONE, StatusLabel.NOT_NEEDED}.intersection(task.status_labels)
        )

    def has_changes_requested(self, issue_number: int) -> bool:
        return False

    def is_ci_passed(self, issue_number: int) -> bool:
        return False

    def assess_dependencies(self, issue_number: int) -> DependencyAssessment | None:
        dependencies = self.dependency_resolution.get(issue_number)
        if dependencies is None:
            return None
        return assess_dependencies(dependencies, self)

    def canonical_branch(self, issue_number: int) -> str | None:
        task = self.tasks_by_issue.get(issue_number)
        if task is None:
            return None
        return build_task_branch_name(issue_number, task.subtask_id)

    def branch_resolution(self, issue_number: int) -> TaskBranchResolution | None:
        return None
