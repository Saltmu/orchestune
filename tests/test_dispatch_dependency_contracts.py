"""Issue #866: freeze Dispatcher dependency contracts across their consumers."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_context import _build_pr_mappings, _build_task_mappings
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
from tests.conftest import make_issue, make_pr


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
    return Task(
        issue_number=number,
        subtask_id=subtask_id,
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:blocked",),
        created_at="2026-01-01T00:00:00+00:00",
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
) -> tuple[tuple[bool, list[int]], str, str | None]:
    launch = _is_task_stack_eligible(
        task,
        resolution,
        done_issue_numbers=set(),
        ci_passed_pr_issue_numbers={2},
        resolved_grand_deps=set(),
    )
    config = DispatcherConfig(
        parent_issue_number=823,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "run_state.json",
    )
    base = _resolve_base_branch_for_task(
        task,
        config,
        branches,
        set(),
        resolution,
        {2},
    )
    rebase = _decide_rebase_target(task, set(), {2}, branches, resolution)
    return launch, base, rebase


def test_grand_dependency_contract_c_incomplete_issue_870_871(tmp_path) -> None:
    """Current asymmetry: launch checks C; stack base and rebase only inspect B."""
    task_a, resolution, branches = _grand_dependency_contract()

    launch, base, rebase = _stack_consumer_results(
        task_a, resolution, branches, tmp_path
    )

    assert launch == (False, [])
    assert base == "claude/issue-2-b"
    assert rebase == "claude/issue-2-b"


def test_grand_dependency_contract_missing_b_resolution_issue_870_871(tmp_path) -> None:
    """Current asymmetry: missing B metadata lets launch's grand-dep check pass."""
    task_a, resolution, branches = _grand_dependency_contract()
    del resolution[2]

    launch, base, rebase = _stack_consumer_results(
        task_a, resolution, branches, tmp_path
    )

    assert launch == (True, [2])
    assert base == "claude/issue-2-b"
    assert rebase == "claude/issue-2-b"


def test_status_priority_is_derived_from_issue_and_pr_inputs() -> None:
    """DONE remains terminal while CHANGES_REQUESTED outranks a passing CI flag."""
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
            review_decision="CHANGES_REQUESTED",
            is_ci_passing=True,
        ),
        make_pr(
            12,
            head_ref="claude/issue-2-reviewed",
            review_decision="CHANGES_REQUESTED",
            is_ci_passing=True,
        ),
        make_pr(
            13,
            head_ref="claude/issue-3-ci-only",
            review_decision="",
            is_ci_passing=True,
        ),
    ]

    _, ci_passed, changes_requested, _ = _build_pr_mappings(tasks, prs)

    assert done == {1}
    assert changes_requested == {1, 2}
    assert ci_passed == {3}
    assert [rule.__name__ for rule in _MAIN_ACTIVE_WORKTREE_RULES.rules[:2]] == [
        "_rule_completed",
        "_rule_changes_requested",
    ]
