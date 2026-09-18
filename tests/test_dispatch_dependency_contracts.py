"""Issue #866: freeze Dispatcher dependency contracts across their consumers."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_context import _build_pr_mappings, _build_task_mappings
from orchestune.dispatch.dependency_assessment import (
    DependencyAssessment,
    assess_dependencies,
)
from orchestune.dispatch.dependency_policy import StackDecision
from orchestune.dispatch.dependency_resolution import (
    REASON_AMBIGUOUS,
    REASON_MISSING,
    REASON_UNKNOWN_PARENT,
    TaskDependencies,
    resolve_all_dependencies,
)
from orchestune.dispatch.launch import _is_task_stack_eligible
from orchestune.dispatch.phase_reconciliation import _MAIN_ACTIVE_WORKTREE_RULES
from orchestune.dispatch.rebase import _decide_rebase_target
from orchestune.dispatch.reconciliation import _resolve_base_branch_for_task
from orchestune.models import IssueRecord, PrRecord, Task
from orchestune.task_branch_resolution import (
    BranchCapability,
    CanonicalBranchState,
    ResolutionSource,
)
from tests.conftest import make_issue, make_pr, make_task


class _ContractPolicyView:
    def __init__(
        self,
        resolution: dict[int, TaskDependencies],
        branches: dict[int, str],
    ) -> None:
        self.resolution = resolution
        self.branches = branches

    def assess_dependencies(self, issue_number: int) -> DependencyAssessment | None:
        dependencies = self.resolution.get(issue_number)
        return None if dependencies is None else assess_dependencies(dependencies, self)

    def is_effectively_done(self, issue_number: int) -> bool:
        return False

    def has_changes_requested(self, issue_number: int) -> bool:
        return False

    def is_ci_passed(self, issue_number: int) -> bool:
        return issue_number == 2

    def canonical_branch(self, issue_number: int) -> str | None:
        return self.branches.get(issue_number)


@dataclass(frozen=True)
class DependencyContractScenario:
    """One reusable Issue-input scenario with purpose-specific expectations."""

    name: str
    issues: tuple[IssueRecord, ...]
    subject_issue_number: int
    resolved: tuple[int, ...]
    unresolved: tuple[tuple[str, str, tuple[int, ...]], ...] = ()


def _issue(
    number: int,
    subtask_id: str,
    *,
    parent_number: int | None = 100,
    depends_on: tuple[str, ...] = (),
    blocked_by: tuple[int, ...] = (),
    labels: tuple[str, ...] = ("status:blocked",),
) -> IssueRecord:
    parent = {"number": parent_number} if parent_number is not None else None
    return make_issue(
        number,
        subtask_id=subtask_id,
        parent=parent,
        depends_on=depends_on,
        blocked_by=blocked_by,
        labels=labels,
    )


@pytest.fixture
def dependency_contract_scenarios() -> tuple[DependencyContractScenario, ...]:
    same_parent = _issue(101, "setup")
    other_parent = _issue(201, "setup", parent_number=200)
    return (
        DependencyContractScenario(
            "body-only-same-epic",
            (same_parent, _issue(110, "body", depends_on=("setup",))),
            110,
            (101,),
        ),
        DependencyContractScenario(
            "native-only-cross-epic",
            (other_parent, _issue(120, "native", blocked_by=(201,))),
            120,
            (201,),
        ),
        DependencyContractScenario(
            "dual-representation-cross-epic-deduplicates",
            (
                other_parent,
                _issue(
                    130,
                    "dual",
                    depends_on=("setup",),
                    blocked_by=(201,),
                ),
            ),
            130,
            (201,),
        ),
        DependencyContractScenario(
            "same-name-cross-epic-keeps-distinct-native-and-body-targets",
            (
                same_parent,
                other_parent,
                _issue(
                    140,
                    "combined",
                    depends_on=("setup",),
                    blocked_by=(201,),
                ),
            ),
            140,
            (201, 101),
        ),
        DependencyContractScenario(
            "missing-body-target",
            (_issue(150, "missing", depends_on=("absent",)),),
            150,
            (),
            (("absent", REASON_MISSING, ()),),
        ),
        DependencyContractScenario(
            "ambiguous-same-epic-target",
            (
                same_parent,
                _issue(102, "setup"),
                _issue(160, "ambiguous", depends_on=("setup",)),
            ),
            160,
            (),
            (("setup", REASON_AMBIGUOUS, (101, 102)),),
        ),
        DependencyContractScenario(
            "unknown-parent",
            (
                same_parent,
                _issue(
                    170,
                    "unknown-parent",
                    parent_number=None,
                    depends_on=("setup",),
                ),
            ),
            170,
            (),
            (("setup", REASON_UNKNOWN_PARENT, ()),),
        ),
        DependencyContractScenario(
            "partially-resolved-native-plus-missing-body",
            (
                other_parent,
                _issue(
                    180,
                    "partial",
                    depends_on=("absent",),
                    blocked_by=(201,),
                ),
            ),
            180,
            (201,),
            (("absent", REASON_MISSING, ()),),
        ),
    )


def _unresolved_projection(
    dependencies: TaskDependencies,
) -> tuple[tuple[str, str, tuple[int, ...]], ...]:
    return tuple(
        (item.raw, item.reason, item.candidates) for item in dependencies.unresolved
    )


def test_dependency_contract_matrix(
    dependency_contract_scenarios: tuple[DependencyContractScenario, ...],
) -> None:
    """The same Issue fixture shape records every dependency-representation axis."""
    for scenario in dependency_contract_scenarios:
        tasks, _, _ = _build_task_mappings(list(scenario.issues))

        actual = resolve_all_dependencies(tasks)[scenario.subject_issue_number]

        assert actual.resolved == scenario.resolved, scenario.name
        assert _unresolved_projection(actual) == scenario.unresolved, scenario.name


def _task(number: int, subtask_id: str, depends_on: tuple[str, ...]) -> Task:
    return make_task(
        number,
        subtask_id=subtask_id,
        footprint=(),
        status_labels=("status:blocked",),
        depends_on=depends_on,
        parent_number=823,
    )


def _grand_dependency_contract() -> (
    tuple[
        Task,
        dict[int, TaskDependencies],
        dict[int, str],
    ]
):
    # A -> B -> C means A depends on B and B depends on C.
    task_a = _task(3, "a", ("b",))
    resolution = {
        3: TaskDependencies(resolved=(2,)),
        2: TaskDependencies(resolved=(1,)),
        1: TaskDependencies(),
    }
    return task_a, resolution, {2: "claude/issue-2-b"}


def _stack_consumer_results(
    task: Task,
    resolution: dict[int, TaskDependencies],
    branches: dict[int, str],
    tmp_path,
) -> tuple[StackDecision, str, str | None]:
    config = DispatcherConfig(
        parent_issue_number=823,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "run_state.json",
    )
    view = _ContractPolicyView(resolution, branches)
    launch = _is_task_stack_eligible(task, view)
    base = _resolve_base_branch_for_task(task, config, view)
    rebase = _decide_rebase_target(task, view)
    return launch, base, rebase


@pytest.mark.parametrize(
    "missing_b_resolution", [False, True], ids=["c-incomplete", "b-resolution-missing"]
)
def test_grand_dependency_contract_issue_870_871(
    tmp_path, missing_b_resolution
) -> None:
    """Base/rebase fail closed for incomplete or unavailable B assessment."""
    task_a, resolution, branches = _grand_dependency_contract()
    if missing_b_resolution:
        del resolution[2]

    launch, base, rebase = _stack_consumer_results(
        task_a, resolution, branches, tmp_path
    )

    assert launch.target is None
    assert base == "parent/issue-823"
    assert rebase is None


def test_resolver_selected_fallback_propagates_to_stack_rebase_and_recovery_base(
    tmp_path,
) -> None:
    task = _task(3, "consumer", ("dependency",))
    resolution = {
        3: TaskDependencies(resolved=(2,)),
        2: TaskDependencies(),
    }
    fallback = "feat/issue-2-dependency"

    launch, base, rebase = _stack_consumer_results(
        task, resolution, {2: fallback}, tmp_path
    )

    assert launch.target is not None
    assert launch.target.branch == fallback
    assert base == fallback
    assert rebase == fallback


def test_status_priority_is_derived_from_issue_and_pr_inputs() -> None:
    """DONE is independent; completion precedes review, and review excludes CI-pass."""
    issues = [
        _issue(1, "done-and-reviewed", labels=("status:done",)),
        _issue(2, "reviewed", labels=("status:in-progress",)),
        _issue(3, "ci-only", labels=("status:in-progress",)),
    ]
    tasks, _, done = _build_task_mappings(issues)
    prs: list[PrRecord] = [
        make_pr(
            11,
            head_ref="claude/issue-1-done-and-reviewed",
            is_cross_repository=False,
            review_decision="CHANGES_REQUESTED",
            is_ci_passing=True,
        ),
        make_pr(
            12,
            head_ref="claude/issue-2-reviewed",
            is_cross_repository=False,
            review_decision="CHANGES_REQUESTED",
            is_ci_passing=True,
        ),
        make_pr(
            13,
            head_ref="claude/issue-3-ci-only",
            is_cross_repository=False,
            review_decision="",
            is_ci_passing=True,
        ),
    ]

    ci_passed, changes_requested, _, _ = _build_pr_mappings(
        tasks, prs, canonical_state=lambda _branch: True
    )

    assert done == {1}
    assert changes_requested == {1, 2}
    assert ci_passed == {3}
    assert [rule.__name__ for rule in _MAIN_ACTIVE_WORKTREE_RULES.rules[:2]] == [
        "_rule_completed",
        "_rule_changes_requested",
    ]


def test_pr_mapping_uses_one_verified_fallback_resolution() -> None:
    tasks, _, _ = _build_task_mappings(
        [_issue(42, "task-a", labels=("status:in-progress",))]
    )
    fallback = make_pr(
        10,
        head_ref="feat/issue-42-task-a",
        closes_issue_numbers=(42,),
        is_cross_repository=False,
        review_decision="",
        is_ci_passing=True,
    )
    fork = make_pr(
        11,
        head_ref="feat/issue-42-task-a",
        closes_issue_numbers=(42,),
        is_cross_repository=True,
        review_decision="CHANGES_REQUESTED",
        is_ci_passing=False,
    )

    ci_passed, changes_requested, branches, resolutions = _build_pr_mappings(
        tasks,
        [fork, fallback],
        canonical_state=lambda _branch: False,
    )

    assert branches == {42: fallback.head_ref}
    assert ci_passed == {42}
    assert changes_requested == set()
    assert resolutions[42].source is ResolutionSource.PR_FALLBACK
    assert resolutions[42].pr is fallback


def test_pr_mapping_without_canonical_probe_fails_closed() -> None:
    tasks, _, _ = _build_task_mappings(
        [_issue(42, "task-a", labels=("status:in-progress",))]
    )
    fallback = make_pr(
        10,
        head_ref="feat/issue-42-task-a",
        closes_issue_numbers=(42,),
        is_cross_repository=False,
        is_ci_passing=True,
    )

    ci_passed, changes_requested, branches, resolutions = _build_pr_mappings(
        tasks, [fallback]
    )

    assert branches == {42: "claude/issue-42-task-a"}
    assert ci_passed == set()
    assert changes_requested == set()
    assert resolutions[42].canonical_state is CanonicalBranchState.INDETERMINATE
    assert not resolutions[42].allows(BranchCapability.FETCH_MERGE)
