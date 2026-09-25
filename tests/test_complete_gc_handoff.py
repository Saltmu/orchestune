"""Tests for Issue #1004: GC handoff-ready admission and worktree removal safety."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestune.complete.contracts import CompleteStage
from orchestune.consistency.models import RepairCommand, RepairStatus
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_records import CompletionReceipt
from orchestune.dispatch.gc.completion import (
    CompletedWorktreeDecision,
    _apply_completed_worktree_outcome,
)
from orchestune.dispatch.gc.zombies import (
    ZombieOrTimeoutReclaim,
    _apply_zombie_or_timeout_reclaim,
    execute_reclaim_repair_command,
)
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.models import Task


def _make_config(tmp_path: Path) -> DispatcherConfig:
    state_file = tmp_path / "run_state.json"
    state_file.write_text("{}")
    return DispatcherConfig(
        apply=True,
        parent_issue_number=894,
        run_state_path=state_file,
        worktree_root=tmp_path / "worktrees",
        events_log_path=tmp_path / "events.jsonl",
        forge=MagicMock(),
    )


class TestCompleteGcHandoff:
    """#1004: completing タスクの通常 reclaim 除外と handoff-ready 受理。"""

    def test_completing_task_is_excluded_from_zombie_and_timeout_reclaim(
        self, tmp_path
    ):
        """completing 中 (completion_id is not None) はプロセス終了やタイムアウトでも通常 reclaim から除外される。"""
        config = _make_config(tmp_path)
        run_state = RunState()

        active = ActiveWorktree(
            issue_number=1004,
            branch="claude/issue-1004-task",
            worktree_path=str(tmp_path / "worktrees" / "wt-1004"),
            pid=None,
            started_at=time.time() - 10000,
            declared_footprint=(),
            owner_kind="dispatch",
            completion_id="comp-1004-1",
            completion_stage=CompleteStage.JOURNALING.value,
        )
        run_state.active_worktrees["1004"] = active

        reclaim = ZombieOrTimeoutReclaim(
            key="1004",
            active=active,
            subtask_id="gc-handoff",
            reason="zombie_process",
            is_timeout=True,
            process_alive=False,
            status_labels=(),
            reclaim_count=0,
            escalate=False,
            now=time.time(),
        )

        event = _apply_zombie_or_timeout_reclaim(run_state, reclaim, config)
        assert event is not None
        assert event.get("action") == "gc_reclaim_excluded_completing"
        assert "1004" in run_state.active_worktrees

    def test_execute_reclaim_repair_command_skips_completing_task(self, tmp_path):
        """execute_reclaim_repair_command も completing 中のタスクを安全にスキップする。"""
        config = _make_config(tmp_path)
        run_state = RunState()

        active = ActiveWorktree(
            issue_number=1004,
            branch="claude/issue-1004-task",
            worktree_path=str(tmp_path / "worktrees" / "wt-1004"),
            pid=None,
            started_at=time.time() - 10000,
            declared_footprint=(),
            owner_kind="dispatch",
            completion_id="comp-1004-1",
            completion_stage=CompleteStage.JOURNALING.value,
        )
        run_state.active_worktrees["1004"] = active

        command = RepairCommand(
            code="execution.reclaim",
            scope="task",
            subject_id="1004",
            idempotency_key="execution:1004:reclaim",
        )

        reclaim = ZombieOrTimeoutReclaim(
            key="1004",
            active=active,
            subtask_id="gc-handoff",
            reason="zombie_process",
            is_timeout=True,
            process_alive=False,
            status_labels=(),
            reclaim_count=0,
            escalate=False,
            now=time.time(),
        )

        result = execute_reclaim_repair_command(
            command,
            run_state,
            reclaim,
            config=config,
            now=time.time(),
        )
        assert result.status == RepairStatus.SKIPPED
        assert "completing" in result.diagnostics[0]

    def test_handoff_ready_task_is_admitted_to_gc_completion(self, tmp_path):
        """handoff-ready な interactive タスクは通常除外されず、完了判定へ渡される。"""
        from orchestune.dispatch.gc.completion import _decide_completed_worktree_outcome

        wt_path = tmp_path / "worktrees" / "wt-1004"
        wt_path.mkdir(parents=True)

        active = ActiveWorktree(
            issue_number=1004,
            branch="claude/issue-1004-task",
            worktree_path=str(wt_path),
            pid=None,
            started_at=time.time() - 100,
            declared_footprint=(),
            owner_kind="interactive",
            completion_id="comp-1004-1",
            completion_result="done",
            completion_stage=CompleteStage.HANDED_OFF_TO_GC.value,
            completion_handoff_ready=True,
            completion_comment_id="comment-1",
            completion_comment_url="https://github.com/example/issues/1004#1",
        )

        forge = MagicMock()
        task = Task(
            issue_number=1004,
            subtask_id="gc-handoff",
            footprint=(),
            symbols=(),
            risk=False,
            priority="high",
            progress_partial=False,
            status_labels=(),
            created_at="2026-09-25T00:00:00Z",
        )

        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch.gc.completion._fetch_outcome_for_active"
            ) as mock_fetch,
        ):
            from orchestune.dispatch.gc.completion import (
                OutcomeLookupResult,
                OutcomeLookupState,
            )
            from orchestune.outcome_record import OutcomeRecord

            mock_fetch.return_value = OutcomeLookupResult(
                state=OutcomeLookupState.FOUND,
                record=OutcomeRecord(result="done", issue=1004, pr=1),
            )
            decision = _decide_completed_worktree_outcome(
                active,
                task,
                forge=forge,
            )
        assert decision.action == "completed"

    def test_not_needed_outcome_retains_dirty_worktree(self, tmp_path):
        """not-needed 報告は worktree が dirty でも受理され、worktree は削除されず保持される。"""
        config = _make_config(tmp_path)
        run_state = RunState()

        wt_path = tmp_path / "worktrees" / "wt-dirty-not-needed"
        wt_path.mkdir(parents=True)
        (wt_path / "dirty.txt").write_text("unsaved work")

        active = ActiveWorktree(
            issue_number=1004,
            branch="claude/issue-1004-task",
            worktree_path=str(wt_path),
            pid=None,
            started_at=time.time() - 100,
            declared_footprint=(),
            owner_kind="interactive",
            completion_id="comp-1004-not-needed",
            completion_result="not-needed",
            completion_stage=CompleteStage.HANDED_OFF_TO_GC.value,
            completion_handoff_ready=True,
            completion_comment_id="comment-2",
            completion_comment_url="https://github.com/example/issues/1004#2",
        )
        run_state.active_worktrees["1004"] = active

        decision = CompletedWorktreeDecision(
            action="not_needed",
            commit_sha=None,
        )

        task = Task(
            issue_number=1004,
            subtask_id="gc-handoff",
            footprint=(),
            symbols=(),
            risk=False,
            priority="high",
            progress_partial=False,
            status_labels=(),
            created_at="2026-09-25T00:00:00Z",
        )

        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch("orchestune.dispatch.gc.completion.remove_worktree") as mock_remove,
        ):
            event = _apply_completed_worktree_outcome(
                active,
                decision,
                config,
                task,
                run_state=run_state,
            )
            mock_remove.assert_not_called()

        assert event["action"] == "not_needed"
        assert wt_path.exists()

    def test_blocked_outcome_retains_dirty_worktree(self, tmp_path):
        """blocked 報告は worktree が dirty でも受理され、worktree は削除されず保持される。"""
        config = _make_config(tmp_path)
        run_state = RunState()

        wt_path = tmp_path / "worktrees" / "wt-dirty-blocked"
        wt_path.mkdir(parents=True)
        (wt_path / "dirty.txt").write_text("unsaved work")

        active = ActiveWorktree(
            issue_number=1004,
            branch="claude/issue-1004-task",
            worktree_path=str(wt_path),
            pid=None,
            started_at=time.time() - 100,
            declared_footprint=(),
            owner_kind="interactive",
            completion_id="comp-1004-blocked",
            completion_result="blocked",
            completion_stage=CompleteStage.HANDED_OFF_TO_GC.value,
            completion_handoff_ready=True,
            completion_comment_id="comment-3",
            completion_comment_url="https://github.com/example/issues/1004#3",
        )
        run_state.active_worktrees["1004"] = active

        decision = CompletedWorktreeDecision(
            action="blocked",
            commit_sha=None,
        )

        task = Task(
            issue_number=1004,
            subtask_id="gc-handoff",
            footprint=(),
            symbols=(),
            risk=False,
            priority="high",
            progress_partial=False,
            status_labels=(),
            created_at="2026-09-25T00:00:00Z",
        )

        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch("orchestune.dispatch.gc.completion.remove_worktree") as mock_remove,
        ):
            event = _apply_completed_worktree_outcome(
                active,
                decision,
                config,
                task,
                run_state=run_state,
            )
            mock_remove.assert_not_called()

        assert event["action"] == "blocked"
        assert wt_path.exists()

    def test_completion_receipt_not_minted_if_state_save_fails(self, tmp_path):
        """障害注入: 台帳保存が失敗した場合、CompletionReceipt は確定されない。"""
        from orchestune.dispatch.gc import _persist_and_confirm_completion

        config = _make_config(tmp_path)
        run_state = RunState()

        active = ActiveWorktree(
            issue_number=1004,
            branch="claude/issue-1004-task",
            worktree_path=str(tmp_path / "worktrees" / "wt-1004"),
            pid=None,
            started_at=time.time() - 100,
            declared_footprint=(),
        )

        ctx = MagicMock()
        ctx.run_state = run_state
        ctx.config = config
        ctx.prs = ()

        receipt = CompletionReceipt(issue_number=1004)

        with patch(
            "orchestune.dispatch.gc.save_run_state", side_effect=OSError("Disk full")
        ):
            success = _persist_and_confirm_completion(ctx, active, receipt)

        assert success is False
        ctx.record_completion.assert_not_called()

    def test_dirty_subtask_with_merged_parent_is_skipped_not_marked_done(
        self, tmp_path
    ):
        """親Issueがマージ済みでも、未コミット変更がある子タスクは already_merged で完了させずスキップする。"""
        from orchestune.dispatch.gc.completion import _decide_completed_worktree_outcome

        wt_path = tmp_path / "worktrees" / "wt-dirty-child"
        wt_path.mkdir(parents=True)
        (wt_path / "dirty.txt").write_text("uncommitted work")

        active = ActiveWorktree(
            issue_number=1005,
            branch="claude/issue-1005-task",
            worktree_path=str(wt_path),
            pid=None,
            started_at=time.time() - 100,
            declared_footprint=(),
        )

        task = Task(
            issue_number=1005,
            subtask_id="child-task",
            footprint=(),
            symbols=(),
            risk=False,
            priority="high",
            progress_partial=False,
            status_labels=(),
            created_at="2026-09-25T00:00:00Z",
            parent_number=1000,
        )

        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch.gc.completion._prior_merge_decision",
                return_value=CompletedWorktreeDecision(action="already_merged"),
            ) as mock_prior,
        ):
            decision = _decide_completed_worktree_outcome(active, task)

        assert decision.action == "completion_skipped_dirty_worktree"
        mock_prior.assert_not_called()

    def test_mid_journaling_completion_is_not_treated_as_handoff_ready(self):
        """JOURNALING中（completion_handoff_ready=False）は completion_result があっても handoff-ready とみなさない。"""
        from orchestune.dispatch.gc.outcome_decision import _is_handoff_ready

        active = ActiveWorktree(
            issue_number=1004,
            branch="claude/issue-1004-task",
            worktree_path="worktrees/w1",
            pid=None,
            started_at=time.time() - 100,
            declared_footprint=(),
            completion_id="comp-1004",
            completion_result="not-needed",
            completion_stage=CompleteStage.JOURNALING.value,
            completion_handoff_ready=False,
        )

        assert _is_handoff_ready(active) is False
