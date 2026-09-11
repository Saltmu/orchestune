"""Issue #866: characterize dispatch phase, ordering, and failure visibility."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from orchestune.consistency.models import (
    ConsistencyScope,
    RepairCommand,
    RepairResult,
    RepairStatus,
)
from orchestune.consistency.supervisor import (
    ConsistencyCycleReport,
    ConsistencyMode,
    ConsistencyRepairPass,
)
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import (
    _completed_issue_numbers,
    _execute_cycle_pipeline,
    _pipeline_state_changes,
    _RepairCycleState,
)
from orchestune.dispatch.cycle_context import IssuesByStatus
from orchestune.dispatch.cycle_report import CycleReport, build_event_log_entry
from orchestune.dispatch.dependency_resolution import (
    REASON_MISSING,
    TaskDependencies,
    UnresolvedDependency,
)
from orchestune.dispatch.launch import TaskLaunchPlan, _record_successful_launch
from orchestune.dispatch.locks import ExternalLockConflict, ExternalLockScanResult
from orchestune.dispatch.phase_scheduling import (
    SchedulingPhaseResult,
    _determine_candidate_tasks,
    run_scheduling_phase,
)
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.scoring import (
    REASON_LAUNCH_FAILED,
    REASON_SELECTED,
    SCHEDULING_MODE_CRITICAL_PATH,
    SchedulingDecision,
    SchedulingResult,
    ScoreComponents,
    Task,
)
from orchestune.dispatch.state import RunState, TaskReclaimRecord
from orchestune.dispatch.summary import merge_skips
from orchestune.dispatch.worktree import LaunchResult
from tests.conftest import make_issue


def _task(
    issue_number: int,
    *,
    status: str = "status:queued",
    subtask_id: str | None = None,
) -> Task:
    return Task(
        issue_number=issue_number,
        subtask_id=(subtask_id if subtask_id is not None else f"task-{issue_number}"),
        footprint=(f"src/{issue_number}.py",),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=(status,),
        created_at="2026-01-01T00:00:00+00:00",
        parent_number=823,
    )


def _config(tmp_path, forge: MagicMock | None = None, **overrides) -> DispatcherConfig:
    values = {
        "apply": False,
        "max_concurrent": 10,
        "max_launches_per_window": 10,
        "run_state_path": tmp_path / "run_state.json",
        "events_log_path": tmp_path / "events.jsonl",
        "worktree_root": tmp_path / "worktrees",
        "forge": forge or MagicMock(),
    }
    values.update(overrides)
    return DispatcherConfig(**values)


def _context(config: DispatcherConfig, tasks: list[Task], **overrides) -> CycleContext:
    context = CycleContext(
        run_state=RunState(),
        tasks_by_issue={task.issue_number: task for task in tasks},
        issue_number_by_subtask_id={
            task.subtask_id: task.issue_number for task in tasks
        },
        dependency_resolution={},
        done_issue_numbers=set(),
        ci_passed_pr_issue_numbers=set(),
        changes_requested_issue_numbers=set(),
        branch_by_issue_number={},
        prs=[],
        pr_by_branch={},
        config=config,
    )
    for name, value in overrides.items():
        setattr(context, name, value)
    return context


def _empty_issues() -> IssuesByStatus:
    return IssuesByStatus([], [], [], [], [], [])


def test_cycle_phase_order_and_batch_selection_contract(tmp_path) -> None:
    """Completion facts reach reconciliation before the cycle's sole selection pass."""
    order: list[str] = []
    selected = _task(20)
    config = _config(tmp_path)
    ctx = _context(
        config,
        [],
        done_issue_numbers={7},
        prior_parent_merge_completed_issue_numbers=frozenset({7}),
    )
    lock_result = ExternalLockScanResult([], [])
    scheduling = SchedulingPhaseResult([selected], 1, [])

    def process(_ctx):
        order.append("active-completion")
        return ([{"issue_number": 5}], [], False, {5})

    def notify(_ctx, _config):
        order.append("pr-link-notification")

    def gc(_ctx, _config, events, _repair_cycle):
        order.append("gc-reclaim")
        return events

    def reconcile(**kwargs):
        order.append("pre-scheduling-reconciliation")
        assert kwargs["completed"] == {5, 7}
        return ([], lock_result)

    def schedule(*args):
        order.append("scheduling-and-launch")
        assert args[3] == {5}
        assert args[0].done_issue_numbers == {7}
        return scheduling

    with (
        patch("orchestune.dispatch.cycle._process_active_worktrees", process),
        patch("orchestune.dispatch.cycle._notify_pr_links", notify),
        patch("orchestune.dispatch.cycle._run_gc_reclaim_phase", gc),
        patch(
            "orchestune.dispatch.cycle._run_pre_scheduling_reconciliation", reconcile
        ),
        patch(
            "orchestune.dispatch.cycle.run_scheduling_phase", side_effect=schedule
        ) as mocked,
    ):
        report, completions = _execute_cycle_pipeline(
            ctx,
            _empty_issues(),
            ctx.run_state,
            config,
            100.0,
            _RepairCycleState(),
            ({"issue_number": 7},),
        )

    assert order == [
        "active-completion",
        "pr-link-notification",
        "gc-reclaim",
        "pre-scheduling-reconciliation",
        "scheduling-and-launch",
    ]
    assert mocked.call_count == 1
    assert report.selected == [selected]
    assert completions == frozenset({5, 7})


def test_candidate_and_skip_order_contract(tmp_path) -> None:
    """Population, scoring, pre-filter skips, and reported skips have distinct order."""
    forge = MagicMock()
    forge.get_label_actor.return_value = "trusted"
    forge.get_actor_permission.return_value = "write"
    config = _config(tmp_path, forge)
    candidates = [_task(30), _task(10), _task(20, status="status:blocked")]
    dependency_resolution = {
        20: TaskDependencies(resolved=(99,)),
        99: TaskDependencies(),
    }
    ctx = _context(
        config,
        [*candidates, _task(99, status="status:in-progress")],
        dependency_resolution=dependency_resolution,
        ci_passed_pr_issue_numbers={99},
        branch_by_issue_number={99: "claude/issue-99-task-99"},
    )
    issues = IssuesByStatus(
        queued=[make_issue(30), make_issue(10)],
        locked=[],
        in_progress=[],
        blocked=[make_issue(20, labels=("status:blocked",))],
        done=[],
        not_needed=[],
    )
    lock_result = ExternalLockScanResult([], [])

    population, _, skips = _determine_candidate_tasks(
        ctx, issues, lock_result, set(), False, 100.0
    )
    result = run_scheduling_phase(
        ctx, issues, lock_result, set(), False, [], 100.0, config
    )

    assert [task.issue_number for task in population] == [30, 10, 20]
    # Current scoring tie-break is Issue number ascending (#871 will also make
    # population/SkipRecord order ascending; these contracts remain distinct).
    assert [task.issue_number for task in result.selected] == [10, 20, 30]
    assert skips == []

    held = _task(40, status="status:blocked")
    backing_off = _task(30)
    unresolved = _task(20, status="status:blocked")
    skip_ctx = _context(
        config,
        [held, backing_off, unresolved],
        run_state=RunState(
            task_reclaim_counts={30: TaskReclaimRecord(early_death_retry_at=200.0)}
        ),
        dependency_resolution={
            20: TaskDependencies(
                unresolved=(UnresolvedDependency("missing", REASON_MISSING),)
            )
        },
    )
    skip_issues = IssuesByStatus(
        queued=[make_issue(30)],
        locked=[],
        in_progress=[],
        blocked=[make_issue(20, labels=("status:blocked",))],
        done=[],
        not_needed=[],
    )
    conflict = ExternalLockConflict("branch", "external/topic")
    _, _, phase_skips = _determine_candidate_tasks(
        skip_ctx,
        skip_issues,
        ExternalLockScanResult([], [], {40: (conflict,)}),
        set(),
        False,
        100.0,
    )

    assert [record.issue_number for record in phase_skips] == [40, 30, 20]
    assert [record.issue_number for record in merge_skips(phase_skips)] == [20, 30, 40]


def test_effective_completion_current_not_needed_and_subtask_id_contract_issue_868(
    tmp_path,
) -> None:
    """#868 will keep NOT_NEEDED but remove the current subtask_id requirement."""
    not_needed = _task(10, status="status:not-needed")
    missing_id = _task(20, status="status:done", subtask_id="")
    config = _config(tmp_path)
    ctx = _context(config, [not_needed, missing_id])

    completed = _completed_issue_numbers(ctx, set())

    assert completed == {10}


def _decision(task: Task) -> SchedulingDecision:
    return SchedulingDecision(
        task.issue_number,
        task.subtask_id,
        SCHEDULING_MODE_CRITICAL_PATH,
        1.0,
        ScoreComponents(),
        selected=True,
        reason=REASON_SELECTED,
    )


def test_dry_run_and_launch_failure_observation_contract(tmp_path) -> None:
    """Dry-run predicts selection; an apply-time launch failure rewrites only its report."""
    task = _task(10)
    config = _config(tmp_path, apply=False)
    ctx = _context(config, [task])
    issues = IssuesByStatus([make_issue(10)], [], [], [], [], [])

    with patch(
        "orchestune.dispatch.phase_scheduling._select_tasks_for_cycle",
        return_value=SchedulingResult([task], [_decision(task)]),
    ):
        preview = run_scheduling_phase(
            ctx, issues, ExternalLockScanResult([], []), set(), False, [], 1.0, config
        )

    assert preview.selected == [task]
    assert preview.decisions[0].reason == REASON_SELECTED
    assert ctx.run_state.active_worktrees == {}

    config.apply = True
    with (
        patch(
            "orchestune.dispatch.phase_scheduling._select_tasks_for_cycle",
            return_value=SchedulingResult([task], [_decision(task)]),
        ),
        patch("orchestune.dispatch.phase_scheduling._finalize_launch", return_value=[]),
    ):
        failed = run_scheduling_phase(
            ctx, issues, ExternalLockScanResult([], []), set(), False, [], 1.0, config
        )

    assert failed.selected == []
    assert failed.decisions[0].selected is False
    assert failed.decisions[0].reason == REASON_LAUNCH_FAILED
    assert ctx.run_state.active_worktrees == {}


def test_skipped_and_failed_repairs_remain_observable_in_cycle_report() -> None:
    """SKIPPED/FAILED are diagnostics, not proof that the desired state was applied."""
    command = RepairCommand(
        code="test.repair",
        scope=ConsistencyScope.TASK,
        subject_id="10",
        idempotency_key="test:10",
    )
    consistency = ConsistencyCycleReport(
        mode=ConsistencyMode.REPAIR,
        repair_passes=(
            ConsistencyRepairPass(
                1,
                (
                    RepairResult(command, RepairStatus.SKIPPED, ("dry-run",)),
                    RepairResult(command, RepairStatus.FAILED, ("forge failed",)),
                ),
            ),
        ),
    )
    report = CycleReport([], 0, {}, [], [], [], False, consistency=consistency)

    serialized = build_event_log_entry(report, 1.0)

    results = serialized["consistency"]["repair_passes"][0]["results"]
    assert [result["status"] for result in results] == ["skipped", "failed"]
    assert _pipeline_state_changes(report, MagicMock(), 1.0) == ()


@pytest.mark.parametrize("failure_surface", ["run-state", "forge-label"])
def test_successful_launch_partial_update_contract(tmp_path, failure_surface) -> None:
    """Memory precedes RunState persistence, which precedes the Forge label update."""
    task = _task(10)
    plan = TaskLaunchPlan(task, "claude/issue-10-task-10", None, "origin/main")
    launch = LaunchResult(
        issue_number=10,
        branch=plan.branch_name,
        worktree_path=str(tmp_path / "worktrees" / "task-10"),
        pid=123,
        launched=True,
    )
    forge = MagicMock()
    config = _config(tmp_path, forge, apply=True)
    run_state = RunState()
    save_error = RuntimeError("run-state failed")
    label_error = RuntimeError("forge label failed")

    with patch("orchestune.dispatch.launch.save_run_state") as save:
        if failure_surface == "run-state":
            save.side_effect = save_error
            expected = save_error
        else:
            forge.add_label.side_effect = label_error
            expected = label_error
        with pytest.raises(RuntimeError, match=str(expected)):
            _record_successful_launch(
                task, plan, launch, run_state, 100.0, config, open_prs=[]
            )

    assert set(run_state.active_worktrees) == {"10"}
    assert run_state.launch_history == [100.0]
    if failure_surface == "run-state":
        forge.add_label.assert_not_called()
    else:
        save.assert_called_once()
        forge.add_label.assert_called_once_with(10, "status:in-progress")
        forge.remove_label.assert_not_called()
