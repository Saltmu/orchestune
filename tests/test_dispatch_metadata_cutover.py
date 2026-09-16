"""#890: scoring and CycleContext consume immutable task metadata."""

from __future__ import annotations

from orchestune.dispatch.conflicts import build_task_conflict_graph
from orchestune.dispatch.critical_path import compute_precedence_ranks
from orchestune.dispatch.cycle_report import CycleReport
from orchestune.dispatch.dependency_resolution import build_legacy_dag_inputs
from orchestune.dispatch.report import _report_to_dict
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.scoring import select_tasks_with_decisions
from orchestune.dispatch.state import RunState
from orchestune.labels import StatusLabel
from orchestune.models import Task
from orchestune.task_metadata import CycleTask


def _task(issue_number: int, subtask_id: str, **overrides: object) -> Task:
    values: dict[str, object] = {
        "issue_number": issue_number,
        "subtask_id": subtask_id,
        "footprint": (f"src/{issue_number}.py",),
        "symbols": (),
        "risk": False,
        "priority": "medium",
        "progress_partial": False,
        "status_labels": (StatusLabel.QUEUED,),
        "created_at": "2026-01-01T00:00:00Z",
    }
    values.update(overrides)
    return Task(**values)  # type: ignore[arg-type]


def _context(tasks: list[Task]) -> CycleContext:
    return CycleContext(
        run_state=RunState(),
        tasks_by_issue={task.issue_number: task for task in tasks},
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


def test_context_returns_cycle_tasks_and_old_snapshot_stays_immutable() -> None:
    source_footprint = ["owned.py"]
    raw = _task(1, "one", footprint=source_footprint)
    ctx = _context([raw])

    before = ctx.task(1)
    assert isinstance(before, CycleTask)
    assert isinstance(ctx.tasks()[0], CycleTask)
    assert isinstance(ctx.queued_tasks()[0], CycleTask)

    source_footprint.append("mutated.py")
    assert before is not None and before.footprint == ("owned.py",)

    ctx.record_completion(1)
    after = ctx.task(1)

    assert before is not None and before.status_labels == (StatusLabel.QUEUED,)
    assert isinstance(after, CycleTask)
    assert after.status_labels == (StatusLabel.DONE,)


def test_duplicate_issue_candidate_behavior_matches_for_both_representations() -> None:
    task = _task(1, "same-issue")
    dag_inputs = build_legacy_dag_inputs((task,))

    def selected_and_reasons(candidates: list[Task] | list[CycleTask]) -> tuple:
        result = select_tasks_with_decisions(
            candidates,
            RunState(),
            1_800_000_000.0,
            2,
            2,
            3600,
            known_tasks=candidates,
            derived_inputs=dag_inputs,
        )
        return (
            tuple(selected.issue_number for selected in result.selected),
            tuple(decision.reason for decision in result.decisions),
        )

    assert selected_and_reasons([task, task]) == selected_and_reasons(
        [CycleTask.from_task(task), CycleTask.from_task(task)]
    )


def test_task_and_cycle_task_have_identical_rank_conflicts_and_selection() -> None:
    raw = [
        _task(1, "a", footprint=("shared.py",)),
        _task(2, "b", depends_on=("a",), footprint=("shared.py",)),
    ]
    metadata = [CycleTask.from_task(task) for task in raw]
    dag_inputs = build_legacy_dag_inputs(tuple(raw))
    durations = {"a": 2.0, "b": 1.0}

    raw_ranks = compute_precedence_ranks(raw, durations, derived_inputs=dag_inputs)
    metadata_ranks = compute_precedence_ranks(
        metadata, durations, derived_inputs=dag_inputs
    )
    assert metadata_ranks == raw_ranks

    raw_conflicts = build_task_conflict_graph(
        raw, threshold=0.5, derived_inputs=dag_inputs
    )
    metadata_conflicts = build_task_conflict_graph(
        metadata, threshold=0.5, derived_inputs=dag_inputs
    )
    assert metadata_conflicts.edges == raw_conflicts.edges

    raw_result = select_tasks_with_decisions(
        raw,
        RunState(),
        1_800_000_000.0,
        2,
        2,
        3600,
        conflict_graph=raw_conflicts,
        known_tasks=raw,
        derived_inputs=dag_inputs,
    )
    metadata_result = select_tasks_with_decisions(
        metadata,
        RunState(),
        1_800_000_000.0,
        2,
        2,
        3600,
        conflict_graph=metadata_conflicts,
        known_tasks=metadata,
        derived_inputs=dag_inputs,
    )
    assert [task.issue_number for task in metadata_result.selected] == [
        task.issue_number for task in raw_result.selected
    ]
    assert metadata_result.decisions == raw_result.decisions


def test_duplicate_subtask_edges_union_and_conflict_metadata_last_wins() -> None:
    first = _task(1, "dup", footprint=("first.py",))
    second = _task(2, "dup", footprint=("shared.py",))
    left = _task(3, "left", depends_on=("dup",), footprint=("left.py",))
    right = _task(4, "right", depends_on=("dup",), footprint=("shared.py",))
    raw = [first, second, left, right]
    metadata = [CycleTask.from_task(task) for task in raw]
    dag_inputs = build_legacy_dag_inputs(tuple(raw))

    ranks = compute_precedence_ranks(metadata, derived_inputs=dag_inputs)
    assert ranks.unlocked_count("dup") == 2

    conflicts = build_task_conflict_graph(
        metadata, threshold=0.5, derived_inputs=dag_inputs
    )
    assert any({edge.left, edge.right} == {"dup", "right"} for edge in conflicts.edges)
    assert not any(
        {edge.left, edge.right} == {"dup", "left"} for edge in conflicts.edges
    )


def test_empty_subtask_id_is_ignored_for_rank_and_conflicts() -> None:
    raw = [_task(1, ""), _task(2, "named")]
    metadata = [CycleTask.from_task(task) for task in raw]
    dag_inputs = build_legacy_dag_inputs(tuple(raw))

    ranks = compute_precedence_ranks(metadata, derived_inputs=dag_inputs)
    conflicts = build_task_conflict_graph(
        metadata, threshold=0.5, derived_inputs=dag_inputs
    )

    assert "" not in ranks.bottom_level
    assert conflicts.edges == ()


def test_cycle_report_serializes_metadata_without_raw_dependency_fields() -> None:
    task = CycleTask.from_task(_task(1, "one", depends_on=("raw",)))
    report = CycleReport(
        selected=[task],
        quota_slots_available=1,
        lock_changes={"to_lock": [task], "to_unlock": []},
        deviation_events=[],
        completion_events=[],
        promotion_events=[],
        applied=False,
    )

    payload = _report_to_dict(report)

    assert "depends_on" not in payload["selected"][0]
    assert "native_depends_on" not in payload["selected"][0]
    assert payload["lock_changes"]["to_lock"][0] == payload["selected"][0]
