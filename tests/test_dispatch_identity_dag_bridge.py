"""Tests for the identity-boundary DAG bridge (#888).

Covers `DependencyDeclarations`, `build_legacy_dag_inputs`, the `derived_inputs`
compat keyword on `compute_precedence_ranks`/`build_task_conflict_graph`, and
`CycleContext.dag_inputs`.
"""

from __future__ import annotations

import dataclasses

import pytest

from orchestune.dag.models import SubTask
from orchestune.dispatch.conflicts import build_task_conflict_graph, subtasks_from_tasks
from orchestune.dispatch.critical_path import compute_precedence_ranks
from orchestune.dispatch.dependency_resolution import (
    DependencyDeclarations,
    build_legacy_dag_inputs,
    legacy_merged_depends_on,
)
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.state import RunState
from orchestune.models import Task


def _task(issue_number: int, subtask_id: str, **overrides: object) -> Task:
    defaults: dict[str, object] = dict(
        issue_number=issue_number,
        subtask_id=subtask_id,
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:queued",),
        created_at="2026-01-01T00:00:00Z",
    )
    defaults.update(overrides)
    return Task(**defaults)  # type: ignore[arg-type]


def _context(tasks: list[Task]) -> CycleContext:
    tasks_by_issue = {task.issue_number: task for task in tasks}
    return CycleContext(
        run_state=RunState(),
        tasks_by_issue=tasks_by_issue,
        issue_number_by_subtask_id={},
        dependency_resolution={},
        done_issue_numbers=set(),
        ci_passed_pr_issue_numbers=set(),
        changes_requested_issue_numbers=set(),
        branch_by_issue_number={},
        prs=[],
        pr_by_branch={},
        config=None,  # type: ignore[arg-type]
    )


class TestDependencyDeclarations:
    def test_from_task_extracts_body_and_native(self) -> None:
        task = _task(1, "a", depends_on=("b", "c"), native_depends_on=(2, 3))
        declarations = DependencyDeclarations.from_task(task)
        assert declarations.body == ("b", "c")
        assert declarations.native == (2, 3)

    def test_defaults_to_empty(self) -> None:
        task = _task(1, "a")
        declarations = DependencyDeclarations.from_task(task)
        assert declarations.body == ()
        assert declarations.native == ()

    def test_is_frozen(self) -> None:
        declarations = DependencyDeclarations(body=("x",), native=(1,))
        with pytest.raises(dataclasses.FrozenInstanceError):
            declarations.body = ("y",)  # type: ignore[misc]

    def test_does_not_mutate_input_task(self) -> None:
        task = _task(1, "a", depends_on=("b",), native_depends_on=(2,))
        snapshot = dataclasses.replace(task)
        DependencyDeclarations.from_task(task)
        assert task == snapshot


class TestLegacyMergedDependsOnUnchanged:
    """Golden-equivalence: the refactor must not change this function's behavior."""

    def test_body_and_native_union_native_first(self) -> None:
        task = _task(2, "b", depends_on=("c",), native_depends_on=(1,))
        result = legacy_merged_depends_on(task, {1: "a", 3: "c"})
        assert result == ("a", "c")

    def test_unknown_native_is_dropped(self) -> None:
        task = _task(2, "b", native_depends_on=(99,))
        result = legacy_merged_depends_on(task, {})
        assert result == ()

    def test_no_duplication_when_native_and_body_name_the_same_dep(self) -> None:
        task = _task(2, "b", depends_on=("a",), native_depends_on=(1,))
        result = legacy_merged_depends_on(task, {1: "a"})
        assert result == ("a",)


class TestBuildLegacyDagInputs:
    def test_matches_subtasks_from_tasks_field_values(self) -> None:
        upstream = _task(1, "a")
        downstream = _task(2, "b", depends_on=("a",), native_depends_on=(1,))
        tasks = (upstream, downstream)

        dict_form = subtasks_from_tasks(tasks)
        tuple_form = build_legacy_dag_inputs(tasks)

        by_id = {subtask.id: subtask for subtask in tuple_form}
        assert set(by_id) == set(dict_form)
        for subtask_id, expected in dict_form.items():
            actual = by_id[subtask_id]
            assert actual.depends_on == expected.depends_on
            assert actual.footprint == expected.footprint
            assert actual.symbols == expected.symbols
            assert actual.risk == expected.risk
            assert actual.priority == expected.priority
            assert actual.shared_contract == expected.shared_contract
            assert actual.writes_shared_contract == expected.writes_shared_contract
            assert actual.issue_number == expected.issue_number

    def test_preserves_input_order(self) -> None:
        tasks = (_task(3, "c"), _task(1, "a"), _task(2, "b"))
        result = build_legacy_dag_inputs(tasks)
        assert [subtask.id for subtask in result] == ["c", "a", "b"]

    def test_skips_tasks_without_subtask_id(self) -> None:
        tasks = (_task(1, "a"), _task(2, ""))
        result = build_legacy_dag_inputs(tasks)
        assert [subtask.id for subtask in result] == ["a"]

    def test_does_not_collapse_duplicate_subtask_ids(self) -> None:
        tasks = (_task(1, "dup"), _task(2, "dup"))
        result = build_legacy_dag_inputs(tasks)
        assert len(result) == 2
        assert [subtask.issue_number for subtask in result] == [1, 2]

    def test_unknown_native_dependency_is_dropped(self) -> None:
        tasks = (_task(1, "a", native_depends_on=(999,)),)
        result = build_legacy_dag_inputs(tasks)
        assert result[0].depends_on == ()

    def test_does_not_mutate_input_tasks(self) -> None:
        tasks = (_task(1, "a", depends_on=("b",), native_depends_on=(2,)),)
        snapshot = dataclasses.replace(tasks[0])
        build_legacy_dag_inputs(tasks)
        assert tasks[0] == snapshot


class TestComputePrecedenceRanksDerivedInputs:
    def test_derived_inputs_matches_task_only_path(self) -> None:
        upstream = _task(1, "a")
        downstream = _task(2, "b", depends_on=("a",))
        tasks = [upstream, downstream]
        durations = {"a": 2.0, "b": 3.0}

        legacy = compute_precedence_ranks(tasks, durations)
        derived = compute_precedence_ranks(
            durations=durations,
            derived_inputs=build_legacy_dag_inputs(tuple(tasks)),
        )

        assert derived.bottom_level == legacy.bottom_level
        assert derived.unlocked == legacy.unlocked
        assert derived.downstream == legacy.downstream
        assert derived.exact_bottom_level == legacy.exact_bottom_level
        assert derived.exact_downstream == legacy.exact_downstream

    def test_existing_task_only_call_sites_are_unaffected(self) -> None:
        upstream = _task(1, "a")
        downstream = _task(2, "b", native_depends_on=(1,))
        result = compute_precedence_ranks([upstream, downstream])
        assert result.unlocked_count("a") == 1


class TestBuildTaskConflictGraphDerivedInputs:
    def test_derived_inputs_matches_task_only_path(self) -> None:
        a = _task(1, "a", footprint=("x.py",))
        b = _task(2, "b", footprint=("x.py",))
        tasks = [a, b]

        legacy = build_task_conflict_graph(tasks, threshold=0.5)
        derived = build_task_conflict_graph(
            tasks, threshold=0.5, derived_inputs=build_legacy_dag_inputs(tuple(tasks))
        )

        assert derived.edges == legacy.edges

    def test_existing_task_only_call_sites_are_unaffected(self) -> None:
        a = _task(1, "a", footprint=("x.py",))
        b = _task(2, "b", footprint=("x.py",))
        graph = build_task_conflict_graph([a, b], threshold=0.5)
        assert graph.has_conflict("a", "b")


class TestCycleContextDagInputs:
    def test_returns_subtasks_for_given_issue_numbers_in_order(self) -> None:
        upstream = _task(1, "a")
        downstream = _task(2, "b", depends_on=("a",))
        ctx = _context([upstream, downstream])

        result = ctx.dag_inputs((2, 1))

        assert [subtask.id for subtask in result] == ["b", "a"]
        assert result[0].depends_on == ("a",)

    def test_unknown_issue_number_raises(self) -> None:
        ctx = _context([_task(1, "a")])
        with pytest.raises(ValueError):
            ctx.dag_inputs((1, 999))

    def test_empty_input_returns_empty(self) -> None:
        ctx = _context([_task(1, "a")])
        assert ctx.dag_inputs(()) == ()


class TestSubTaskShape:
    def test_build_legacy_dag_inputs_returns_subtask_instances(self) -> None:
        result = build_legacy_dag_inputs((_task(1, "a"),))
        assert isinstance(result[0], SubTask)
