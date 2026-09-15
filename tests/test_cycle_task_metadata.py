"""Tests for the raw-dependency-free task metadata types (#887)."""

from __future__ import annotations

import dataclasses

import pytest

from orchestune.models import Task
from orchestune.task_metadata import CycleTask, TaskMetadata

_RAW_FIELDS = frozenset({"depends_on", "native_depends_on"})

_EXPECTED_PROPERTIES = (
    "issue_number",
    "subtask_id",
    "footprint",
    "symbols",
    "risk",
    "priority",
    "progress_partial",
    "status_labels",
    "created_at",
    "yaml_error",
    "parent_number",
    "issue_state",
    "parent_state",
    "shared_contract",
    "writes_shared_contract",
    "execution_profile",
    "model_tier",
)


def _sample_task(**overrides: object) -> Task:
    base: dict[str, object] = dict(
        issue_number=887,
        subtask_id="cycle-task-metadata-types",
        footprint=("orchestune/task_metadata.py",),
        symbols=("TaskMetadata", "CycleTask"),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:in-progress",),
        created_at="2026-09-12T09:21:15Z",
        depends_on=("integrate-cycle-context-cutover",),
        native_depends_on=(873,),
        yaml_error=False,
        parent_number=823,
        issue_state="OPEN",
        parent_state="OPEN",
        shared_contract="dispatcher-dependency-context",
        writes_shared_contract=True,
        execution_profile="deep-reasoning",
        model_tier="middle",
    )
    base.update(overrides)
    return Task(**base)  # type: ignore[arg-type]


def _non_raw_task_fields() -> tuple[dataclasses.Field, ...]:
    return tuple(f for f in dataclasses.fields(Task) if f.name not in _RAW_FIELDS)


class TestCycleTaskFieldShape:
    def test_field_names_and_order_match_task_minus_raw_fields(self) -> None:
        expected = tuple(f.name for f in _non_raw_task_fields())
        actual = tuple(f.name for f in dataclasses.fields(CycleTask))
        assert actual == expected
        assert len(actual) == 17

    def test_defaults_match_task_defaults(self) -> None:
        cycle_fields = {f.name: f for f in dataclasses.fields(CycleTask)}
        for task_field in _non_raw_task_fields():
            cycle_field = cycle_fields[task_field.name]
            assert cycle_field.default == task_field.default, task_field.name

    def test_is_frozen_dataclass_with_slots(self) -> None:
        assert dataclasses.is_dataclass(CycleTask)
        assert hasattr(CycleTask, "__slots__")


class TestFromTask:
    def test_copies_all_non_raw_fields(self) -> None:
        task = _sample_task()
        cycle_task = CycleTask.from_task(task)
        for field in _non_raw_task_fields():
            assert getattr(cycle_task, field.name) == getattr(
                task, field.name
            ), field.name

    def test_does_not_mutate_input_task(self) -> None:
        task = _sample_task()
        snapshot = dataclasses.replace(task)
        CycleTask.from_task(task)
        assert task == snapshot

    def test_owns_tuple_typed_fields(self) -> None:
        cycle_task = CycleTask.from_task(_sample_task())
        assert isinstance(cycle_task.footprint, tuple)
        assert isinstance(cycle_task.symbols, tuple)
        assert isinstance(cycle_task.status_labels, tuple)

    def test_raw_fields_raise_attribute_error(self) -> None:
        cycle_task = CycleTask.from_task(_sample_task())
        with pytest.raises(AttributeError):
            _ = cycle_task.depends_on  # type: ignore[attr-defined]
        with pytest.raises(AttributeError):
            _ = cycle_task.native_depends_on  # type: ignore[attr-defined]

    def test_defaults_round_trip_for_minimal_task(self) -> None:
        minimal_task = Task(
            issue_number=1,
            subtask_id="s",
            footprint=(),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=(),
            created_at="2026-01-01T00:00:00Z",
        )
        cycle_task = CycleTask.from_task(minimal_task)
        assert cycle_task.yaml_error is False
        assert cycle_task.parent_number is None
        assert cycle_task.issue_state == "OPEN"
        assert cycle_task.parent_state is None
        assert cycle_task.shared_contract is None
        assert cycle_task.writes_shared_contract is False
        assert cycle_task.execution_profile is None
        assert cycle_task.model_tier is None


class TestFrozen:
    def test_cannot_assign_existing_field(self) -> None:
        cycle_task = CycleTask.from_task(_sample_task())
        with pytest.raises(dataclasses.FrozenInstanceError):
            cycle_task.priority = "high"  # type: ignore[misc]

    def test_cannot_assign_new_attribute(self) -> None:
        # frozen+slots dataclasses reject an unknown attribute name before
        # reaching the frozen check, surfacing as a TypeError rather than
        # `FrozenInstanceError` — this is CPython dataclasses' own behavior
        # for this combination, not something `CycleTask` controls. Either
        # way the assignment must not succeed.
        cycle_task = CycleTask.from_task(_sample_task())
        with pytest.raises((dataclasses.FrozenInstanceError, TypeError)):
            cycle_task.extra = "nope"  # type: ignore[attr-defined]


class TestTaskMetadataProtocol:
    def test_declares_exactly_the_17_read_only_properties(self) -> None:
        for name in _EXPECTED_PROPERTIES:
            member = getattr(TaskMetadata, name)
            assert isinstance(member, property), name
            assert member.fset is None, f"{name} must be read-only"

    def test_does_not_declare_raw_dependency_fields(self) -> None:
        assert not hasattr(TaskMetadata, "depends_on")
        assert not hasattr(TaskMetadata, "native_depends_on")

    def test_does_not_declare_is_ready(self) -> None:
        assert not hasattr(TaskMetadata, "is_ready")
        assert not hasattr(CycleTask, "is_ready")
        assert not hasattr(Task, "is_ready")

    def test_existing_task_satisfies_the_protocol(self) -> None:
        task = _sample_task()
        assert isinstance(task, TaskMetadata)

    def test_cycle_task_satisfies_the_protocol(self) -> None:
        cycle_task = CycleTask.from_task(_sample_task())
        assert isinstance(cycle_task, TaskMetadata)
