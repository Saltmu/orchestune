"""dispatch_gcのstale active entry破棄処理とSupervisor境界のテスト。

test_dispatch_gc.py (1418行) からstale entryルール関連(#479)を分割。
gitプリミティブは test_dispatch_gc_git_primitives.py、完了ルールは
test_dispatch_gc_completed_rule.py、エンドツーエンド統合テストは
test_dispatch_gc_integration.py を参照。
"""

from unittest.mock import patch

from orchestune.consistency.models import RepairStatus
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.gc import (
    _apply_stale_active_entry_discard,
    _rule_not_needed,
)
from orchestune.dispatch.phase_gc import run_gc_phase
from orchestune.dispatch.state import RunState
from orchestune.outcome_record import OutcomeRecord
from tests.dispatch_gc_test_support import _active, _ctx, _task


class TestApplyStaleActiveEntryDiscard:
    """#382: 帳簿(run_state)破棄の前に、実際に稼働中かもしれない物理
    worktree・プロセスの後始末を行うことを検証する。"""

    def test_kills_live_process_and_removes_worktree(self, tmp_path):
        active = _active(worktree_path=str(tmp_path), pid=12345)
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)

        with (
            patch(
                "orchestune.dispatch.gc.backup_wip_commit", return_value=None
            ) as mock_backup,
            patch("orchestune.dispatch.gc.is_process_alive", return_value=True),
            patch("orchestune.dispatch.gc.os.kill") as mock_kill,
            patch("orchestune.dispatch.gc.remove_worktree") as mock_remove,
        ):
            discarded = _apply_stale_active_entry_discard(
                run_state, "280", active, "test reason", config
            )

        assert discarded is True
        mock_backup.assert_called_once_with(
            str(tmp_path), "WIP: backup by Orchestune GC (stale active entry)"
        )
        mock_kill.assert_called_once_with(12345, 9)
        mock_remove.assert_called_once_with(str(tmp_path))
        assert run_state.active_worktrees == {}

    def test_dead_process_skips_kill_but_still_removes_worktree(self, tmp_path):
        active = _active(worktree_path=str(tmp_path), pid=12345)
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)

        with (
            patch("orchestune.dispatch.gc.backup_wip_commit", return_value=None),
            patch("orchestune.dispatch.gc.is_process_alive", return_value=False),
            patch("orchestune.dispatch.gc.os.kill") as mock_kill,
            patch("orchestune.dispatch.gc.remove_worktree") as mock_remove,
        ):
            discarded = _apply_stale_active_entry_discard(
                run_state, "280", active, "test reason", config
            )

        assert discarded is True
        mock_kill.assert_not_called()
        mock_remove.assert_called_once_with(str(tmp_path))
        assert run_state.active_worktrees == {}

    def test_missing_worktree_skips_backup_and_removal(self, tmp_path):
        active = _active(worktree_path="worktrees/does-not-exist", pid=12345)
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)

        with (
            patch("orchestune.dispatch.gc.backup_wip_commit") as mock_backup,
            patch("orchestune.dispatch.gc.is_process_alive", return_value=True),
            patch("orchestune.dispatch.gc.os.kill") as mock_kill,
            patch("orchestune.dispatch.gc.remove_worktree") as mock_remove,
        ):
            discarded = _apply_stale_active_entry_discard(
                run_state, "280", active, "test reason", config
            )

        assert discarded is True
        mock_backup.assert_not_called()
        mock_remove.assert_not_called()
        # プロセスが生存中なら、worktreeの有無に関わらず停止は行う。
        mock_kill.assert_called_once_with(12345, 9)
        assert run_state.active_worktrees == {}

    def test_backup_failure_skips_discard_and_preserves_entry(
        self, tmp_path, fake_forge
    ):
        active = _active(worktree_path=str(tmp_path), pid=12345)
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl", apply=True, forge=fake_forge
        )

        with (
            patch(
                "orchestune.dispatch.gc.backup_wip_commit",
                return_value="fatal: unable to write new index file",
            ),
            patch("orchestune.dispatch.gc.is_process_alive", return_value=True),
            patch("orchestune.dispatch.gc.os.kill") as mock_kill,
            patch("orchestune.dispatch.gc.remove_worktree") as mock_remove,
        ):
            discarded = _apply_stale_active_entry_discard(
                run_state, "280", active, "test reason", config
            )

        assert discarded is False
        mock_kill.assert_not_called()
        mock_remove.assert_not_called()
        fake_forge.add_comment.assert_called_once()
        assert "test reason" in fake_forge.add_comment.call_args.args[1]
        # バックアップ失敗時は帳簿を温存し、次サイクルでの再試行に委ねる。
        assert run_state.active_worktrees == {"280": active}

    def test_dry_run_does_not_touch_anything(self, tmp_path):
        active = _active(worktree_path=str(tmp_path), pid=12345)
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl", apply=False
        )

        with (
            patch("orchestune.dispatch.gc.backup_wip_commit") as mock_backup,
            patch("orchestune.dispatch.gc.os.kill") as mock_kill,
            patch("orchestune.dispatch.gc.remove_worktree") as mock_remove,
        ):
            discarded = _apply_stale_active_entry_discard(
                run_state, "280", active, "test reason", config
            )

        assert discarded is True
        mock_backup.assert_not_called()
        mock_kill.assert_not_called()
        mock_remove.assert_not_called()
        assert run_state.active_worktrees == {"280": active}


class TestSupervisorOwnedStaleEntry:
    def test_stale_entry_is_applied_by_the_typed_gc_repair(self, tmp_path, fake_forge):
        worktree = tmp_path / "active-worktree"
        worktree.mkdir()
        active = _active(worktree_path=str(worktree), pid=12345)
        task = _task(status_labels=("status:blocked",))
        run_state = RunState(active_worktrees={"280": active})
        fake_forge.branch_exists.return_value = True
        fake_forge.get_issue_state.return_value = "OPEN"
        fake_forge.get_issue_labels.return_value = ("status:blocked",)
        config = DispatcherConfig(
            apply=True,
            run_state_path=tmp_path / "state.json",
            events_log_path=tmp_path / "events.jsonl",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
        )

        with (
            patch(
                "orchestune.dispatch.execution_repair.is_process_alive",
                return_value=True,
            ),
            patch("orchestune.dispatch.gc.backup_wip_commit", return_value=None),
            patch("orchestune.dispatch.gc.is_process_alive", return_value=True),
            patch("orchestune.dispatch.gc.os.kill") as kill,
            patch("orchestune.dispatch.gc.remove_worktree") as remove,
        ):
            outcome = run_gc_phase(
                run_state, {280: task}, config, [], open_prs=[], now=1_000.0
            )

        results = outcome.consistency.repair_passes[0].results
        assert [(result.command.code, result.status) for result in results] == [
            ("execution.reclaim", RepairStatus.APPLIED)
        ]
        assert [event["action"] for event in outcome.completion_events] == [
            "stale_active_entry_discarded"
        ]
        assert run_state.active_worktrees == {}
        kill.assert_called_once_with(12345, 9)
        remove.assert_called_once_with(str(worktree))

    def test_live_in_progress_label_cancels_stale_cleanup(self, tmp_path, fake_forge):
        active = _active(worktree_path=str(tmp_path / "active"), pid=12345)
        cached_task = _task(status_labels=("status:blocked",))
        run_state = RunState(active_worktrees={"280": active})
        fake_forge.branch_exists.return_value = True
        fake_forge.get_issue_state.return_value = "OPEN"
        fake_forge.get_issue_labels.return_value = ("status:in-progress",)
        config = DispatcherConfig(
            apply=True,
            run_state_path=tmp_path / "state.json",
            events_log_path=tmp_path / "events.jsonl",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
        )

        with (
            patch(
                "orchestune.dispatch.execution_repair.is_process_alive",
                return_value=True,
            ),
            patch("orchestune.dispatch.gc.os.kill") as kill,
            patch("orchestune.dispatch.gc.remove_worktree") as remove,
        ):
            outcome = run_gc_phase(
                run_state,
                {280: cached_task},
                config,
                [],
                open_prs=[],
                now=1_000.0,
            )

        (result,) = outcome.consistency.repair_passes[0].results
        assert result.status is RepairStatus.SKIPPED
        assert result.diagnostics == (
            "stale cleanup precondition no longer holds: status:in-progress is live",
        )
        assert run_state.active_worktrees == {"280": active}
        assert outcome.completion_events == []
        fake_forge.get_issue_labels.assert_called_with(280)
        kill.assert_not_called()
        remove.assert_not_called()

    def test_forge_error_defers_stale_cleanup(self, tmp_path, fake_forge):
        active = _active(worktree_path=str(tmp_path / "active"), pid=12345)
        cached_task = _task(status_labels=("status:blocked",))
        run_state = RunState(active_worktrees={"280": active})
        fake_forge.branch_exists.return_value = True
        fake_forge.get_issue_state.side_effect = RuntimeError("Forge unavailable")
        config = DispatcherConfig(
            apply=True,
            run_state_path=tmp_path / "state.json",
            events_log_path=tmp_path / "events.jsonl",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
        )

        with (
            patch(
                "orchestune.dispatch.execution_repair.is_process_alive",
                return_value=True,
            ),
            patch("orchestune.dispatch.gc.os.kill") as kill,
            patch("orchestune.dispatch.gc.remove_worktree") as remove,
        ):
            outcome = run_gc_phase(
                run_state,
                {280: cached_task},
                config,
                [],
                open_prs=[],
                now=1_000.0,
            )

        (result,) = outcome.consistency.repair_passes[0].results
        assert result.status is RepairStatus.FAILED
        assert result.diagnostics == ("RuntimeError: Forge unavailable",)
        assert run_state.active_worktrees == {"280": active}
        assert outcome.completion_events == []
        fake_forge.get_issue_labels.assert_not_called()
        kill.assert_not_called()
        remove.assert_not_called()


class TestRuleNotNeededOutcomeStaleness:
    def test_ignores_stale_not_needed_outcome_comment(self):
        active = _active(started_at=1787270400.0)  # 2026-08-21T00:00:00Z
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        ctx.config.resolved_forge.list_comments = lambda issue_num: [  # type: ignore[method-assign]
            {
                "body": OutcomeRecord(result="not-needed", issue=280).render(),
                "created_at": "2026-08-20T00:00:00Z",  # Before started_at
            }
        ]

        outcome = _rule_not_needed(ctx, "280", active, task)

        # Stale not-needed comment is ignored, so _rule_not_needed returns None
        assert outcome is None

    def test_accepts_fresh_not_needed_outcome_comment(self):
        active = _active(started_at=1787270400.0)  # 2026-08-21T00:00:00Z
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        ctx.config.resolved_forge.list_comments = lambda issue_num: [  # type: ignore[method-assign]
            {
                "body": OutcomeRecord(result="not-needed", issue=280).render(),
                "created_at": "2026-08-22T00:00:00Z",  # After started_at
            }
        ]

        with patch(
            "orchestune.dispatch.gc._finalize_not_needed_worktree",
            return_value={"action": "not_needed", "issue_number": 280},
        ):
            outcome = _rule_not_needed(ctx, "280", active, task)

        assert outcome is not None
        assert outcome.completion_event["action"] == "not_needed"
