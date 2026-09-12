from __future__ import annotations

from dataclasses import dataclass

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.dependency_assessment import (
    AssessedDependency,
    DependencyAssessment,
    DependencyState,
)
from orchestune.dispatch.rebase import _decide_rebase_target
from orchestune.dispatch.reconciliation import _resolve_base_branch_for_task
from tests.conftest import make_task


@dataclass
class FakeDependencyPolicyView:
    assessments: dict[int, DependencyAssessment | None]
    branches: dict[int, str | None]

    def assess_dependencies(self, issue_number: int) -> DependencyAssessment | None:
        return self.assessments.get(issue_number)

    def canonical_branch(self, issue_number: int) -> str | None:
        return self.branches.get(issue_number)


def _assessment(*dependencies: tuple[int, DependencyState]) -> DependencyAssessment:
    return DependencyAssessment(
        resolved=tuple(AssessedDependency(*dependency) for dependency in dependencies)
    )


def _task():
    return make_task(
        3,
        subtask_id="a",
        depends_on=("b",),
        footprint=(),
        parent_number=823,
    )


def test_rebase_and_base_wrappers_return_the_same_safe_stack_branch(tmp_path) -> None:
    view = FakeDependencyPolicyView(
        {
            3: _assessment((2, DependencyState.CI_PASSED_UNMERGED)),
            2: _assessment((1, DependencyState.COMPLETED)),
        },
        {2: "feat/issue-2-b"},
    )
    config = DispatcherConfig(
        parent_issue_number=823,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "state.json",
    )

    assert _decide_rebase_target(_task(), view) == "feat/issue-2-b"
    assert _resolve_base_branch_for_task(_task(), config, view) == "feat/issue-2-b"


@pytest.mark.parametrize(
    "view",
    [
        FakeDependencyPolicyView({3: None}, {}),
        FakeDependencyPolicyView(
            {3: _assessment((2, DependencyState.WAITING))},
            {2: "feat/issue-2-b"},
        ),
        FakeDependencyPolicyView(
            {3: _assessment((2, DependencyState.CI_PASSED_UNMERGED)), 2: None},
            {2: "feat/issue-2-b"},
        ),
    ],
    ids=["subject-unavailable", "dependency-not-ci-passed", "grand-unavailable"],
)
def test_no_safe_target_means_no_rebase_and_parent_base_fallback(
    tmp_path, view: FakeDependencyPolicyView
) -> None:
    config = DispatcherConfig(
        parent_issue_number=823,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "state.json",
    )

    assert _decide_rebase_target(_task(), view) is None
    assert _resolve_base_branch_for_task(_task(), config, view) == "parent/issue-823"
