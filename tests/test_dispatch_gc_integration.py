"""`run_dispatch_cycle`経由のGCエンドツーエンド統合テスト。

test_dispatch_gc.py (1418行) から統合テスト関連(#479)を分割。gitプリミティブは
test_dispatch_gc_git_primitives.py、stale entryルールは
test_dispatch_gc_stale_rules.py、完了ルールは test_dispatch_gc_completed_rule.py
を参照。
"""

import subprocess
from unittest.mock import patch

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import run_dispatch_cycle
from orchestune.dispatch.state import (
    ActiveWorktree,
    RunState,
    load_run_state,
    save_run_state,
)
from tests.dispatch_gc_test_support import _issue


class TestGC:
    def test_gc_reclaim_zombie(self, tmp_path, fake_forge):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
            forge=fake_forge,
            task_timeout_seconds=3600,
        )
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")

        wt_path = tmp_path / "worktrees/claude-issue-1-task-1"
        wt_path.mkdir(parents=True, exist_ok=True)

        run_state = RunState(
            active_worktrees={
                "1": ActiveWorktree(
                    issue_number=1,
                    branch="claude/issue-1-task-1",
                    worktree_path=str(wt_path),
                    pid=12345,
                    started_at=1700000000.0,
                    declared_footprint=(),
                )
            }
        )
        save_run_state(run_state, config.run_state_path)

        with (
            patch.object(fake_forge, "list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
            patch.object(fake_forge, "list_open_prs", return_value=[]),
            # 完了判定によるdirty-worktree保留とは分離し、GC回収自体を検証する。
            patch("orchestune.dispatch.gc._is_worktree_complete", return_value=False),
            patch(
                "orchestune.dispatch.rebase.check_footprint_deviation",
                return_value=[],
            ),
            patch(
                "orchestune.dispatch.gc.zombies.is_process_alive", return_value=False
            ),
            patch.object(fake_forge, "add_label") as mock_add_label,
            patch.object(fake_forge, "remove_label") as mock_remove_label,
            patch.object(fake_forge, "add_comment") as mock_add_comment,
            patch("orchestune.dispatch.gc.zombies.remove_worktree") as mock_remove_wt,
            patch("orchestune.dispatch.worktree.subprocess.run") as mock_run,
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue_a] if label == "status:in-progress" else []
            )

            def run_mock(args, **kwargs):
                if "status" in args:
                    return subprocess.CompletedProcess(
                        args=args, returncode=0, stdout=" M src/foo.py\n"
                    )
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="")

            mock_run.side_effect = run_mock

            run_dispatch_cycle(config)

        git_calls = [call.args[0] for call in mock_run.call_args_list]
        assert any("add" in cmd for cmd in git_calls)
        assert any("commit" in cmd for cmd in git_calls)

        mock_remove_wt.assert_called_once_with(str(wt_path))
        mock_remove_label.assert_called_with(1, "status:in-progress")
        mock_add_label.assert_called_with(1, "status:queued")
        mock_add_comment.assert_called_once()

        loaded = load_run_state(config.run_state_path)
        assert "1" not in loaded.active_worktrees

    def test_gc_reclaim_zombie_only(self, tmp_path, fake_forge):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
            forge=fake_forge,
            task_timeout_seconds=0,
            zombie_gc=True,
        )
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")
        wt_path = tmp_path / "worktrees/claude-issue-1-task-1"
        wt_path.mkdir(parents=True, exist_ok=True)

        run_state = RunState(
            active_worktrees={
                "1": ActiveWorktree(
                    issue_number=1,
                    branch="claude/issue-1-task-1",
                    worktree_path=str(wt_path),
                    pid=12345,
                    started_at=1700000000.0,
                    declared_footprint=(),
                )
            }
        )
        save_run_state(run_state, config.run_state_path)

        with (
            patch.object(fake_forge, "list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
            patch.object(fake_forge, "list_open_prs", return_value=[]),
            # 完了判定によるdirty-worktree保留とは分離し、GC回収自体を検証する。
            patch("orchestune.dispatch.gc._is_worktree_complete", return_value=False),
            patch(
                "orchestune.dispatch.rebase.check_footprint_deviation",
                return_value=[],
            ),
            patch(
                "orchestune.dispatch.gc.zombies.is_process_alive", return_value=False
            ),
            patch.object(fake_forge, "add_label") as mock_add_label,
            patch.object(fake_forge, "remove_label") as mock_remove_label,
            patch.object(fake_forge, "add_comment") as mock_add_comment,
            patch("orchestune.dispatch.gc.zombies.remove_worktree") as mock_remove_wt,
            patch("orchestune.dispatch.worktree.subprocess.run") as mock_run,
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue_a] if label == "status:in-progress" else []
            )
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=""
            )

            run_dispatch_cycle(config)

        mock_remove_wt.assert_called_once_with(str(wt_path))
        mock_remove_label.assert_called_with(1, "status:in-progress")
        mock_add_label.assert_called_with(1, "status:queued")
        mock_add_comment.assert_called_once()

        loaded = load_run_state(config.run_state_path)
        assert "1" not in loaded.active_worktrees

    def test_gc_reclaim_zombie_disabled(self, tmp_path, fake_forge):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
            forge=fake_forge,
            task_timeout_seconds=0,
            zombie_gc=False,
        )
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")
        wt_path = tmp_path / "worktrees/claude-issue-1-task-1"
        wt_path.mkdir(parents=True, exist_ok=True)

        run_state = RunState(
            active_worktrees={
                "1": ActiveWorktree(
                    issue_number=1,
                    branch="claude/issue-1-task-1",
                    worktree_path=str(wt_path),
                    pid=12345,
                    started_at=1700000000.0,
                    declared_footprint=(),
                )
            }
        )
        save_run_state(run_state, config.run_state_path)

        with (
            patch.object(fake_forge, "list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
            patch.object(fake_forge, "list_open_prs", return_value=[]),
            patch("orchestune.dispatch.gc._is_worktree_complete", return_value=False),
            patch(
                "orchestune.dispatch.gc.zombies.is_process_alive", return_value=False
            ),
            patch(
                "orchestune.dispatch.gc.zombies.is_process_alive", return_value=False
            ),
            patch.object(fake_forge, "add_label") as mock_add_label,
            patch.object(fake_forge, "remove_label") as mock_remove_label,
            patch.object(fake_forge, "add_comment") as mock_add_comment,
            patch("orchestune.dispatch.gc.zombies.remove_worktree") as mock_remove_wt,
            patch("orchestune.dispatch.worktree.subprocess.run") as mock_run,
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue_a] if label == "status:in-progress" else []
            )
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=""
            )

            run_dispatch_cycle(config)

        mock_remove_wt.assert_not_called()
        mock_remove_label.assert_not_called()
        mock_add_label.assert_not_called()
        mock_add_comment.assert_not_called()

        loaded = load_run_state(config.run_state_path)
        assert "1" in loaded.active_worktrees

    def test_gc_reclaim_timeout(self, tmp_path, fake_forge):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
            forge=fake_forge,
            task_timeout_seconds=600,
        )
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")
        wt_path = tmp_path / "worktrees/claude-issue-1-task-1"
        wt_path.mkdir(parents=True, exist_ok=True)

        import time

        old_time = time.time() - 1000

        run_state = RunState(
            active_worktrees={
                "1": ActiveWorktree(
                    issue_number=1,
                    branch="claude/issue-1-task-1",
                    worktree_path=str(wt_path),
                    pid=12345,
                    started_at=old_time,
                    declared_footprint=(),
                )
            }
        )
        save_run_state(run_state, config.run_state_path)

        with (
            patch.object(fake_forge, "list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
            patch.object(fake_forge, "list_open_prs", return_value=[]),
            patch("orchestune.dispatch.gc._is_worktree_complete", return_value=False),
            patch("orchestune.dispatch.gc.zombies.is_process_alive", return_value=True),
            patch("orchestune.dispatch.gc.zombies.is_process_alive", return_value=True),
            patch.object(fake_forge, "add_label") as mock_add_label,
            patch.object(fake_forge, "remove_label") as mock_remove_label,
            patch.object(fake_forge, "add_comment") as mock_add_comment,
            patch("orchestune.dispatch.gc.zombies.remove_worktree") as mock_remove_wt,
            patch("orchestune.dispatch.worktree.subprocess.run") as mock_run,
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue_a] if label == "status:in-progress" else []
            )
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=""
            )

            run_dispatch_cycle(config)

        mock_remove_wt.assert_called_once_with(str(wt_path))
        mock_remove_label.assert_called_with(1, "status:in-progress")
        mock_add_label.assert_called_with(1, "status:queued")
        mock_add_comment.assert_called_once()

        loaded = load_run_state(config.run_state_path)
        assert "1" not in loaded.active_worktrees

    def test_gc_reclaim_backup_failure_skips_deletion(self, tmp_path, fake_forge):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
            forge=fake_forge,
            task_timeout_seconds=3600,
        )
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")
        wt_path = tmp_path / "worktrees/claude-issue-1-task-1"
        wt_path.mkdir(parents=True, exist_ok=True)

        run_state = RunState(
            active_worktrees={
                "1": ActiveWorktree(
                    issue_number=1,
                    branch="claude/issue-1-task-1",
                    worktree_path=str(wt_path),
                    pid=12345,
                    started_at=1700000000.0,
                    declared_footprint=(),
                )
            }
        )
        save_run_state(run_state, config.run_state_path)

        with (
            patch.object(fake_forge, "list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]
            ),
            patch.object(fake_forge, "list_open_prs", return_value=[]),
            # 完了判定によるdirty-worktree保留とは分離し、GC失敗時の保護を検証する。
            patch("orchestune.dispatch.gc._is_worktree_complete", return_value=False),
            patch(
                "orchestune.dispatch.rebase.check_footprint_deviation",
                return_value=[],
            ),
            patch(
                "orchestune.dispatch.gc.zombies.is_process_alive", return_value=False
            ),
            patch.object(fake_forge, "add_label") as mock_add_label,
            patch.object(fake_forge, "remove_label") as mock_remove_label,
            patch.object(fake_forge, "add_comment") as mock_add_comment,
            patch("orchestune.dispatch.gc.zombies.remove_worktree") as mock_remove_wt,
            patch("orchestune.dispatch.worktree.subprocess.run") as mock_run,
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue_a] if label == "status:in-progress" else []
            )
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=1,
                cmd="git commit",
                stderr="fatal: unable to write new index file",
            )

            run_dispatch_cycle(config)

        mock_remove_wt.assert_not_called()
        mock_remove_label.assert_not_called()
        mock_add_label.assert_not_called()
        mock_add_comment.assert_called_once()
        assert (
            "WIPバックアップコミットの作成に失敗しました"
            in mock_add_comment.call_args[0][1]
        )

        loaded = load_run_state(config.run_state_path)
        assert "1" in loaded.active_worktrees
