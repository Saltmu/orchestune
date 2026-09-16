"""#889: metadata-only dispatch consumers accept both task representations."""

from __future__ import annotations

import inspect
from typing import get_type_hints

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.launch import _decide_task_launch_plan
from orchestune.dispatch.targets import (
    ClaudeCodeCloudRoutineDispatchTarget,
    default_dry_run_command_builder,
)
from orchestune.models import Task
from orchestune.task_metadata import CycleTask, TaskMetadata


def _task() -> Task:
    return Task(
        issue_number=889,
        subtask_id="cycle-launch-metadata-consumers",
        footprint=("orchestune/dispatch/launch.py",),
        symbols=("TaskMetadata",),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:queued",),
        created_at="2026-09-16T00:00:00Z",
        depends_on=("cycle-identity-dag-bridge",),
        native_depends_on=(888,),
        parent_number=823,
        execution_profile="deep-reasoning",
        model_tier="middle",
    )


def test_metadata_consumer_annotations_do_not_require_raw_task() -> None:
    from orchestune.dispatch import (
        escalation,
        filters,
        launch,
        locks,
        rebase,
        targets,
        worktree,
    )

    task_parameters = (
        (launch._is_task_stack_eligible, "task"),
        (launch.TaskLaunchPlan, "task"),
        (locks.LockDependencyView.task, "return"),
        (filters._candidate_conflicts_with_forced_serial_active, "candidate"),
        (escalation._decide_changes_requested_escalation, "active_task"),
        (rebase._decide_rebase_target, "active_task"),
        (worktree.create_worktree_and_launch, "task"),
        (targets.DispatchTarget.launch, "task"),
        (targets.default_dry_run_command_builder, "task"),
    )

    for consumer, parameter in task_parameters:
        if parameter == "task" and consumer is launch.TaskLaunchPlan:
            annotation = get_type_hints(consumer)[parameter]
        else:
            signature = inspect.signature(consumer)
            annotation = (
                signature.return_annotation
                if parameter == "return"
                else signature.parameters[parameter].annotation
            )
        if parameter == "return":
            assert annotation in (TaskMetadata | None, "TaskMetadata | None")
        else:
            assert (
                "TaskMetadata" in str(annotation)
                or getattr(annotation, "__bound__", None) is TaskMetadata
            )


def test_legacy_and_cycle_task_have_identical_branch_and_launch_plan(tmp_path) -> None:
    legacy_task = _task()
    cycle_task = CycleTask.from_task(legacy_task)
    config = DispatcherConfig(
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "run_state.json",
        worktree_root=tmp_path / "worktrees",
        parent_issue_number=823,
    )

    legacy_plan = _decide_task_launch_plan([legacy_task], {}, config)
    cycle_plan = _decide_task_launch_plan([cycle_task], {}, config)

    assert [
        (plan.branch_name, plan.base_branch_for_launch, plan.base_branch_for_state)
        for plan in legacy_plan
    ] == [
        (plan.branch_name, plan.base_branch_for_launch, plan.base_branch_for_state)
        for plan in cycle_plan
    ]
    assert default_dry_run_command_builder(
        legacy_task, tmp_path
    ) == default_dry_run_command_builder(cycle_task, tmp_path)
    target = ClaudeCodeCloudRoutineDispatchTarget("routine", "token")
    assert target._build_text(
        legacy_task, legacy_plan[0].branch_name
    ) == target._build_text(cycle_task, cycle_plan[0].branch_name)
