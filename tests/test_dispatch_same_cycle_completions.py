"""Confirmed same-cycle completion facts flow through CycleContext (#873)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from orchestune.consistency.supervisor import ConsistencyMode
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import _finish_consistency_runtime, _RepairCycleState
from orchestune.dispatch.cycle_actions import CycleActionAdapter
from orchestune.dispatch.cycle_report import CycleReport
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.models import Task

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-same-cycle-"))


def _task(issue_number: int, *, subtask_id: str = "task-a", status_labels=()) -> Task:
    return Task(
        issue_number=issue_number,
        subtask_id=subtask_id,
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=tuple(status_labels),
        created_at="2026-01-01T00:00:00+00:00",
        parent_number=100,
    )


def _ctx(**overrides: Any) -> CycleContext:
    defaults: dict[str, Any] = dict(
        run_state=RunState(active_worktrees={}),
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
    )
    defaults.update(overrides)
    actions = CycleActionAdapter(defaults["run_state"], defaults["config"], now=0.0)
    ctx = CycleContext(**defaults, actions=actions)
    actions.bind_context(ctx)
    return ctx


class TestConfirmedCompletionFacts:
    def test_prior_parent_merge_is_available_from_initial_context(self):
        ctx = _ctx(
            tasks_by_issue={7: _task(7)},
            prior_parent_merge_completed_issue_numbers=frozenset({7}),
        )

        assert ctx.is_completion_confirmed(7) is True

    def test_verified_active_completion_is_recorded_in_context(self):
        task = _task(280, subtask_id="", status_labels=("status:in-progress",))
        active = ActiveWorktree(
            issue_number=280,
            branch="claude/issue-280-task-a",
            worktree_path="worktrees/w1",
            pid=111,
            started_at=1_699_999_000.0,
            declared_footprint=(),
        )
        run_state = RunState(active_worktrees={"280": active})
        ctx = _ctx(tasks_by_issue={280: task}, run_state=run_state)
        ctx.config.apply = True

        with (
            patch(
                "orchestune.dispatch.gc._is_worktree_complete",
                autospec=True,
                return_value=True,
            ),
            patch(
                "orchestune.dispatch.gc._local_pr_completion_status",
                autospec=True,
                return_value="completed",
            ),
            patch(
                "orchestune.dispatch.gc._finalize_completed_worktree",
                autospec=True,
                return_value={"action": "already_merged", "subtask_id": ""},
            ),
            patch("orchestune.dispatch.gc.save_run_state", autospec=True),
        ):
            result = ctx.process_active_worktrees()

        assert result.completion_events[0]["action"] == "already_merged"
        assert ctx.is_completion_confirmed(280) is True

    def test_unverified_completion_outcome_does_not_create_a_fact(self):
        task = _task(280, status_labels=("status:in-progress",))
        active = ActiveWorktree(
            issue_number=280,
            branch="claude/issue-280-task-a",
            worktree_path="worktrees/w1",
            pid=111,
            started_at=1_699_999_000.0,
            declared_footprint=(),
        )
        ctx = _ctx(
            tasks_by_issue={280: task},
            run_state=RunState(active_worktrees={"280": active}),
        )

        with patch(
            "orchestune.dispatch.gc._is_worktree_complete",
            autospec=True,
            return_value=False,
        ):
            ctx.process_active_worktrees()

        assert ctx.is_completion_confirmed(280) is False


class TestFinalConsistencyRepairExecutor:
    def test_uses_the_same_cycle_context_as_completion_evidence(self):
        ctx = _ctx(done_issue_numbers={1})
        runtime = MagicMock()
        report = CycleReport(
            selected=[],
            quota_slots_available=0,
            lock_changes={"to_lock": [], "to_unlock": []},
            deviation_events=[],
            completion_events=[],
            promotion_events=[],
            applied=False,
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            consistency_mode=ConsistencyMode.REPAIR,
            apply=False,
        )

        with patch("orchestune.dispatch.cycle._ContextRepairExecutor") as factory:
            _finish_consistency_runtime(
                runtime, report, ctx, 0.0, config, _RepairCycleState()
            )

        factory.assert_called_once_with(ctx)
