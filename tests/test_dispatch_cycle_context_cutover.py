"""Issue #873: the live cycle exposes one Context boundary and seven action ports."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from orchestune.consistency.models import RepairCommand, RepairResult, RepairStatus
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_action_contracts import (
    ActivePhaseResult,
    GcPhaseResult,
    StackBase,
)
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.scoring import SchedulingResult
from orchestune.dispatch.state import RunState


def _context(actions: MagicMock, tmp_path: Path) -> CycleContext:
    return CycleContext(
        run_state=RunState(),
        tasks_by_issue={},
        issue_number_by_subtask_id={},
        dependency_resolution={},
        done_issue_numbers=set(),
        ci_passed_pr_issue_numbers=set(),
        changes_requested_issue_numbers=set(),
        branch_by_issue_number={},
        prs=[],
        pr_by_branch={},
        config=DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        ),
        actions=actions,
    )


def test_context_delegates_all_seven_action_ports(tmp_path: Path) -> None:
    actions = MagicMock()
    active = ActivePhaseResult((), (), False)
    gc = GcPhaseResult([], MagicMock())
    locks = MagicMock()
    scheduling = SchedulingResult([], [])
    repair = RepairResult(
        RepairCommand("test", scope=MagicMock(), idempotency_key="test"),
        RepairStatus.SKIPPED,
    )
    actions.process_active_worktrees.return_value = active
    actions.run_gc.return_value = gc
    actions.reconcile_recovery.return_value = ()
    actions.scan_external_locks.return_value = locks
    actions.select_tasks.return_value = scheduling
    actions.launch_tasks.return_value = ()
    actions.execute_repair.return_value = repair
    ctx = _context(actions, tmp_path)
    command = repair.command

    assert ctx.process_active_worktrees() is active
    assert ctx.run_gc(({"issue_number": 1},)) is gc
    assert ctx.reconcile_recovery() == ()
    assert ctx.scan_external_locks() is locks
    assert ctx.select_tasks(()) is scheduling
    assert ctx.launch_tasks((), (StackBase(1, "parent/issue-1"),), ()) == ()
    assert ctx.execute_repair(command) is repair

    actions.process_active_worktrees.assert_called_once_with()
    actions.run_gc.assert_called_once_with(({"issue_number": 1},))
    actions.reconcile_recovery.assert_called_once_with()
    actions.scan_external_locks.assert_called_once_with()
    actions.select_tasks.assert_called_once_with(())
    actions.launch_tasks.assert_called_once_with(
        (), (StackBase(1, "parent/issue-1"),), ()
    )
    actions.execute_repair.assert_called_once_with(command)


def test_context_no_longer_exposes_legacy_raw_state_windows(tmp_path: Path) -> None:
    ctx = _context(MagicMock(), tmp_path)

    for name in (
        "run_state",
        "tasks_by_issue",
        "issue_number_by_subtask_id",
        "dependency_resolution",
        "done_issue_numbers",
        "ci_passed_pr_issue_numbers",
        "changes_requested_issue_numbers",
        "branch_by_issue_number",
        "prs",
        "pr_by_branch",
        "issue_records_by_number",
        "prior_parent_merge_hold_issue_numbers",
        "prior_parent_merge_completed_issue_numbers",
    ):
        assert not hasattr(ctx, name), name
