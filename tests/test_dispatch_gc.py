import subprocess
from unittest.mock import patch

from orchestune.dispatch_gc import (
    _collect_zombies_and_timeouts,
    _decide_completed_worktree_outcome,
    _decide_not_needed_dirty_worktree,
    _decide_stale_active_entry,
    _finalize_completed_worktree,
    _finalize_not_needed_worktree,
    _rule_completed,
    is_process_alive,
    remote_branch_commit_sha_if_ahead,
    remove_worktree,
    worktree_has_new_commits,
    worktree_has_uncommitted_changes,
)
from orchestune.dispatch_rules import CycleContext
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree, RunState
from orchestune.dispatch_targets import (
    ClaudeCodeCloudRoutineDispatchTarget,
    DispatchHandle,
)
from orchestune.dispatcher import DispatcherConfig
from orchestune.github import PrRecord


def _ctx(**overrides):
    defaults = dict(
        run_state=RunState(active_worktrees={}),
        tasks_by_issue={},
        issue_number_by_subtask_id={},
        done_subtask_ids=set(),
        ci_passed_pr_subtask_ids=set(),
        changes_requested_subtask_ids=set(),
        subtask_branch_map={},
        prs=[],
        pr_by_branch={},
        config=DispatcherConfig(run_state_path="dummy.json", worktree_root="worktrees"),
    )
    defaults.update(overrides)
    return CycleContext(**defaults)


def _active(**overrides):
    defaults = dict(
        issue_number=280,
        branch="claude/issue-280-task-a",
        worktree_path="worktrees/w1",
        pid=111,
        started_at=1_699_999_000.0,
        declared_footprint=("src/foo.py",),
    )
    defaults.update(overrides)
    return ActiveWorktree(**defaults)


def _task(**overrides):
    defaults = dict(
        issue_number=280,
        subtask_id="task-a",
        footprint=("src/foo.py",),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:not-needed",),
        created_at="2026-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return Task(**defaults)


class TestIsProcessAlive:
    """#193: pidのプロセス生存確認による完了判定。"""

    def test_none_pid_is_not_alive(self):
        assert is_process_alive(None) is False

    def test_alive_pid_returns_true(self):
        with patch("orchestune.dispatch_gc.os.kill") as mock_kill:
            mock_kill.return_value = None
            assert is_process_alive(12345) is True

    def test_missing_pid_returns_false(self):
        with patch("orchestune.dispatch_gc.os.kill", side_effect=ProcessLookupError):
            assert is_process_alive(12345) is False

    def test_permission_error_is_treated_as_alive(self):
        with patch("orchestune.dispatch_gc.os.kill", side_effect=PermissionError):
            assert is_process_alive(1) is True


class TestCollectZombiesAndTimeouts:
    def test_unknown_start_time_is_not_timed_out(self, tmp_path):
        active = _active(
            started_at=None,
            worktree_path=str(tmp_path / "missing-worktree"),
            pid=None,
        )
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(apply=True, task_timeout_seconds=60)

        with (
            patch("orchestune.dispatch_gc.time.time", return_value=2_000.0),
            patch("orchestune.dispatch_gc.github.remove_label") as mock_remove_label,
        ):
            events = _collect_zombies_and_timeouts(run_state, {}, config)

        assert events == []
        assert run_state.active_worktrees == {"280": active}
        mock_remove_label.assert_not_called()

    def test_timeout_without_physical_worktree_requeues_issue(self, tmp_path):
        """#198: run_stateを削除するGC回収は、worktreeの有無にかかわらず
        GitHubのprimary stateもqueuedへ遷移させる。"""
        active = _active(
            started_at=1_000.0,
            worktree_path=str(tmp_path / "missing-worktree"),
            pid=None,
        )
        run_state = RunState(active_worktrees={"280": active})
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(apply=True, task_timeout_seconds=60)

        with (
            patch("orchestune.dispatch_gc.time.time", return_value=2_000.0),
            patch("orchestune.dispatch_gc.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_gc.github.add_label") as mock_add_label,
            patch("orchestune.dispatch_gc.github.add_comment"),
        ):
            events = _collect_zombies_and_timeouts(
                run_state, {active.issue_number: task}, config
            )

        assert events[0]["reason"] == "timeout exceeded"
        assert run_state.active_worktrees == {}
        mock_remove_label.assert_called_once_with(280, "status:in-progress")
        mock_add_label.assert_called_once_with(280, "status:queued")

    def test_held_worktree_is_not_reclaimed(self):
        """同一サイクルで人間確認待ちになったworktreeはGC対象から除外する。"""
        active = _active(pid=None)
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(apply=True, zombie_gc=True)

        with (
            patch("orchestune.dispatch_gc.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_gc.github.add_label") as mock_add_label,
            patch("orchestune.dispatch_gc.github.add_comment") as mock_add_comment,
        ):
            events = _collect_zombies_and_timeouts(
                run_state,
                {},
                config,
                held_worktree_paths={active.worktree_path},
            )

        assert events == []
        assert run_state.active_worktrees == {"280": active}
        mock_remove_label.assert_not_called()
        mock_add_label.assert_not_called()
        mock_add_comment.assert_not_called()


class TestWorktreeHasUncommittedChanges:
    """#193: worktree削除前の未コミット変更確認（安全側フォールバック）。"""

    def test_clean_worktree_returns_false(self):
        with patch("orchestune.dispatch_gc.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            assert worktree_has_uncommitted_changes("worktrees/w1") is False

    def test_dirty_worktree_returns_true(self):
        with patch("orchestune.dispatch_gc.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=" M src/foo.py\n", stderr=""
            )
            assert worktree_has_uncommitted_changes("worktrees/w1") is True

    def test_git_error_defaults_to_clean(self):
        """存在しないworktreeなどgit statusが失敗する場合はクオータ解放を優先し、
        削除を妨げないようクリーン扱いとする。"""
        with patch(
            "orchestune.dispatch_gc.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, []),
        ):
            assert worktree_has_uncommitted_changes("worktrees/missing") is False


class TestWorktreeHasNewCommits:
    """#74: base_branchに対する実コミットの有無確認（空コミット完了の誤判定防止）。"""

    def test_returns_true_when_commits_ahead(self):
        with patch("orchestune.dispatch_gc.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="2\n", stderr=""
            )
            assert worktree_has_new_commits("worktrees/w1", "origin/main") is True

    def test_returns_false_when_no_commits_ahead(self):
        with patch("orchestune.dispatch_gc.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="0\n", stderr=""
            )
            assert worktree_has_new_commits("worktrees/w1", "origin/main") is False

    def test_git_error_falls_back_to_false(self):
        """#135: 比較不能時（base_branch参照が解決できない等）は「新規コミット無し」
        と同じ安全側（False）にフォールバックし、実体のない完了確定を防ぐ。"""
        with patch(
            "orchestune.dispatch_gc.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, []),
        ):
            assert worktree_has_new_commits("worktrees/missing", "origin/main") is False

    def test_git_error_logs_warning_to_stderr(self, capsys):
        """#135: 比較失敗時にstderrへ警告を出力し、原因調査を容易にする。"""
        with patch(
            "orchestune.dispatch_gc.subprocess.run",
            side_effect=subprocess.CalledProcessError(
                1, [], stderr="fatal: bad revision"
            ),
        ):
            worktree_has_new_commits("worktrees/w1", "origin/main")
        captured = capsys.readouterr()
        assert "worktrees/w1" in captured.err
        assert "origin/main" in captured.err


class TestRemoteBranchCommitChecks:
    """#177: クラウド実行の成果はリモート追跡ブランチで検証する。"""

    def test_fetches_fresh_base_and_returns_no_sha_when_branch_is_merged(self):
        with (
            patch(
                "orchestune.dispatch_gc.github.fetch_remote_branch",
                side_effect=("origin/claude/issue-177-task-a", "origin/main"),
            ) as mock_fetch,
            patch("orchestune.dispatch_gc.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="0\n", stderr=""
            )
            assert (
                remote_branch_commit_sha_if_ahead(
                    "repository", "claude/issue-177-task-a", "origin/main"
                )
                is None
            )

        assert mock_fetch.call_args_list == [
            (("repository", "claude/issue-177-task-a"), {}),
            (("repository", "main"), {}),
        ]
        assert mock_run.call_args.args[0][-1] == (
            "origin/main..origin/claude/issue-177-task-a"
        )

    def test_returns_sha_from_the_verified_remote_snapshot(self):
        with (
            patch(
                "orchestune.dispatch_gc.github.fetch_remote_branch",
                side_effect=("origin/claude/issue-177-task-a", "origin/main"),
            ) as mock_fetch,
            patch("orchestune.dispatch_gc.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = (
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="1\n", stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="abc123\n", stderr=""
                ),
            )
            assert (
                remote_branch_commit_sha_if_ahead(
                    "repository", "claude/issue-177-task-a", "main"
                )
                == "abc123"
            )

        assert mock_fetch.call_count == 2
        assert mock_run.call_args.args[0][-1] == "origin/claude/issue-177-task-a"


class TestFinalizeCompletedWorktree:
    """#74: プロセス終了検知後の完了処理。空コミット完了を実完了と誤判定しないこと。"""

    def test_no_new_commits_is_not_treated_as_completed(self):
        """#74再現: worktreeはcleanだがbase_branchに対して新規コミットが0件の場合、
        status:doneを付与せず、依存先タスクの誤昇格を防ぐためcompleted以外のアクションにする。"""
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(apply=True)
        with (
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc.worktree_has_new_commits",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_worktree,
            patch("orchestune.dispatch_gc.github.add_label") as mock_add_label,
            patch("orchestune.dispatch_gc.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_gc.github.add_comment") as mock_add_comment,
        ):
            event = _finalize_completed_worktree(active, task, config)

        assert event["action"] != "completed"
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        mock_remove_label.assert_called_once_with(280, "status:in-progress")
        mock_add_label.assert_called_once_with(280, "status:blocked-human-review")
        mock_add_comment.assert_called_once()
        assert mock_add_comment.call_args.args[0] == 280

    def test_new_commits_present_is_treated_as_completed(self):
        """base_branchに対する実コミットがあれば従来通りcompleted+status:doneとする。"""
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(apply=True)
        with (
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc.worktree_has_new_commits",
                return_value=True,
            ),
            patch("orchestune.dispatch_gc.subprocess.run") as mock_run,
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_worktree,
            patch("orchestune.dispatch_gc.github.add_label") as mock_add_label,
            patch("orchestune.dispatch_gc.github.remove_label") as mock_remove_label,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="deadbeef\n", stderr=""
            )
            event = _finalize_completed_worktree(active, task, config)

        assert event["action"] == "completed"
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        mock_remove_label.assert_called_once_with(280, "status:in-progress")
        mock_add_label.assert_called_once_with(280, "status:done")
        assert event["commit_sha"] == "deadbeef"


class TestRemoveWorktree:
    """#193: 完了したworktreeの削除。"""

    def test_calls_git_worktree_remove_without_force(self):
        with patch("orchestune.dispatch_gc.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            remove_worktree("worktrees/w1")
        args = mock_run.call_args.args[0]
        assert args == ["git", "worktree", "remove", "worktrees/w1"]
        assert "--force" not in args

    def test_swallows_error_when_already_removed(self):
        with patch(
            "orchestune.dispatch_gc.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, []),
        ):
            remove_worktree("worktrees/already-gone")  # 例外を送出しないこと


class TestFinalizeNotNeededWorktree:
    """#280: status:not-neededラベル検知による完全自動クローズ。"""

    def test_apply_removes_worktree_and_closes_issue(self):
        active = _active()
        task = _task()
        config = DispatcherConfig(apply=True)
        with (
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_worktree,
            patch("orchestune.dispatch_gc.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_gc.github.close_issue") as mock_close_issue,
        ):
            event = _finalize_not_needed_worktree(active, task, config)

        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        mock_remove_label.assert_called_once_with(280, "status:in-progress")
        mock_close_issue.assert_called_once()
        close_args = mock_close_issue.call_args.args
        assert close_args[0] == 280
        assert close_args[1] == "not planned"
        assert event == {
            "issue_number": 280,
            "worktree_path": "worktrees/w1",
            "action": "not_needed",
            "subtask_id": "task-a",
        }

    def test_dirty_worktree_is_not_closed(self):
        """未コミットの作業が残っている場合は、安全側に倒しクローズを見送る。"""
        active = _active()
        task = _task()
        config = DispatcherConfig(apply=True)
        with (
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_worktree,
            patch("orchestune.dispatch_gc.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_gc.github.close_issue") as mock_close_issue,
        ):
            event = _finalize_not_needed_worktree(active, task, config)

        mock_remove_worktree.assert_not_called()
        mock_remove_label.assert_not_called()
        mock_close_issue.assert_not_called()
        assert event["action"] == "completion_skipped_dirty_worktree"

    def test_dry_run_does_not_call_github_or_mutate(self):
        active = _active()
        task = _task()
        config = DispatcherConfig(apply=False)
        with (
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_worktree,
            patch("orchestune.dispatch_gc.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_gc.github.close_issue") as mock_close_issue,
        ):
            event = _finalize_not_needed_worktree(active, task, config)

        mock_remove_worktree.assert_not_called()
        mock_remove_label.assert_not_called()
        mock_close_issue.assert_not_called()
        assert event["action"] == "not_needed"

    def test_none_task_defaults_subtask_id_to_empty_string(self):
        active = _active()
        config = DispatcherConfig(apply=True)
        with (
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc.remove_worktree"),
            patch("orchestune.dispatch_gc.github.remove_label"),
            patch("orchestune.dispatch_gc.github.close_issue"),
        ):
            event = _finalize_not_needed_worktree(active, None, config)
        assert event["subtask_id"] == ""


class TestFinalizeNotNeededWorktreeCloudRoutineReview:
    """#282: クラウドルーチン利用可能時は即時クローズせず独立検証レビューへ委譲する。"""

    def _cloud_config(self, **overrides):
        defaults = dict(
            apply=True,
            dispatch_target=ClaudeCodeCloudRoutineDispatchTarget("rid", "rtok"),
        )
        defaults.update(overrides)
        return DispatcherConfig(**defaults)

    def test_dispatches_review_instead_of_closing(self, tmp_path):
        active = _active()
        task = _task()
        config = self._cloud_config(
            not_needed_review_state_path=tmp_path / "state.json"
        )
        handle = DispatchHandle(
            external_id="sess-1", external_url="https://claude.ai/code/s/sess-1"
        )
        with (
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_worktree,
            patch("orchestune.dispatch_gc.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_gc.github.close_issue") as mock_close_issue,
            patch(
                "orchestune.integration_coordinator.ClaudeCodeCloudRoutineDispatchTarget.fire_text",
                return_value=handle,
            ) as mock_fire_text,
        ):
            event = _finalize_not_needed_worktree(active, task, config)

        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        mock_remove_label.assert_called_once_with(280, "status:in-progress")
        mock_close_issue.assert_not_called()
        mock_fire_text.assert_called_once()
        assert "#280" in mock_fire_text.call_args.args[0]
        assert event["action"] == "not_needed_review_dispatched"
        assert event["subtask_id"] == "task-a"

        from orchestune.not_needed_review_state import load_not_needed_review_state

        state = load_not_needed_review_state(config.not_needed_review_state_path)
        assert len(state.pending) == 1
        assert state.pending[0].issue_number == 280
        assert state.pending[0].subtask_id == "task-a"
        assert state.pending[0].session_external_id == "sess-1"

    def test_dirty_worktree_does_not_dispatch_review(self, tmp_path):
        active = _active()
        task = _task()
        config = self._cloud_config(
            not_needed_review_state_path=tmp_path / "state.json"
        )
        with (
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch(
                "orchestune.integration_coordinator.ClaudeCodeCloudRoutineDispatchTarget.fire_text"
            ) as mock_fire_text,
        ):
            event = _finalize_not_needed_worktree(active, task, config)

        mock_fire_text.assert_not_called()
        assert event["action"] == "completion_skipped_dirty_worktree"


class TestDecideCompletedWorktreeOutcome:
    """decide層: worktree_has_uncommitted_changes/worktree_has_new_commitsの
    読み取りのみで方針を判定し、github/worktreeへの書き込みは行わない。"""

    def test_dirty_worktree_is_skipped(self):
        active = _active()
        task = _task()
        with patch(
            "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
            return_value=True,
        ):
            decision = _decide_completed_worktree_outcome(active, task)
        assert decision.action == "completion_skipped_dirty_worktree"

    def test_no_new_commits_is_completed_no_commits(self):
        active = _active()
        task = _task()
        with (
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc.worktree_has_new_commits",
                return_value=False,
            ),
        ):
            decision = _decide_completed_worktree_outcome(active, task)
        assert decision.action == "completed_no_commits"
        assert decision.subtask_id == "task-a"

    def test_clean_with_new_commits_is_completed(self):
        active = _active()
        task = _task()
        with (
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc.worktree_has_new_commits",
                return_value=True,
            ),
        ):
            decision = _decide_completed_worktree_outcome(active, task)
        assert decision.action == "completed"
        assert decision.subtask_id == "task-a"


class TestDecideNotNeededDirtyWorktree:
    def test_true_when_dirty(self):
        with patch(
            "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
            return_value=True,
        ):
            assert _decide_not_needed_dirty_worktree(_active()) is True

    def test_false_when_clean(self):
        with patch(
            "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
            return_value=False,
        ):
            assert _decide_not_needed_dirty_worktree(_active()) is False


class TestDecideStaleActiveEntry:
    """decide層: githubラベルの読み取りのみでstale判定を行い、run_stateは変更しない。"""

    def test_none_when_still_in_progress(self):
        task = _task(status_labels=("status:in-progress",))
        assert _decide_stale_active_entry(_active(), task) is None

    def test_none_when_no_matching_task(self):
        assert _decide_stale_active_entry(_active(), None) is None

    def test_stale_when_label_no_longer_in_progress(self):
        task = _task(status_labels=("status:blocked",))
        event = _decide_stale_active_entry(_active(), task)
        assert event is not None
        assert event["action"] == "stale_active_entry_discarded"


class TestRuleCompleted:
    def test_closed_unmerged_local_pr_is_requeued_without_completing_dependency(self):
        active = _active(pid=123, started_at=1_699_999_000.0)
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        ctx.config.apply = True
        ctx.run_state.active_worktrees["1"] = active
        ctx.prs = [
            PrRecord(
                number=210,
                head_ref=active.branch,
                changed_files=(),
                closes_issue_numbers=(active.issue_number,),
                state="CLOSED",
            )
        ]
        with (
            patch("orchestune.dispatch_gc.is_process_alive", return_value=False),
            patch(
                "orchestune.dispatch_gc.github.list_prs",
                return_value=ctx.prs,
            ) as mock_list_prs,
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove,
            patch("orchestune.dispatch_gc.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_gc.github.add_label") as mock_add_label,
            patch("orchestune.dispatch_gc.github.add_comment") as mock_add_comment,
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is not None
        assert outcome.terminal is True
        assert outcome.completed_subtask_id is None
        assert outcome.completion_event["action"] == "abandoned_pr_requeued"
        assert "1" not in ctx.run_state.active_worktrees
        mock_list_prs.assert_called_once_with(state="all")
        mock_remove.assert_called_once_with(active.worktree_path)
        mock_remove_label.assert_called_once_with(280, "status:in-progress")
        mock_add_label.assert_called_once_with(280, "status:queued")
        mock_add_comment.assert_called_once()

    def test_closed_unmerged_cloud_pr_is_requeued_without_completing_dependency(
        self,
    ):
        active = _active(external_id="session-1")
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        ctx.config.apply = True
        ctx.run_state.active_worktrees["1"] = active

        with (
            patch.object(
                ctx.config.dispatch_target,
                "completion_status",
                return_value="abandoned",
                create=True,
            ),
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove,
            patch("orchestune.dispatch_gc.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_gc.github.add_label") as mock_add_label,
            patch("orchestune.dispatch_gc.github.add_comment") as mock_add_comment,
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is not None
        assert outcome.terminal is True
        assert outcome.completed_subtask_id is None
        assert outcome.completion_event["action"] == "abandoned_pr_requeued"
        assert "1" not in ctx.run_state.active_worktrees
        mock_remove.assert_called_once_with(active.worktree_path)
        mock_remove_label.assert_called_once_with(280, "status:in-progress")
        mock_add_label.assert_called_once_with(280, "status:queued")
        mock_add_comment.assert_called_once()

    def test_local_pr_waits_for_process_before_using_open_pr_as_completion(self):
        active = _active(pid=123, started_at=1_699_999_000.0)
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        with (
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch("orchestune.dispatch_gc.github.list_prs") as mock_list_prs,
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is None
        mock_list_prs.assert_not_called()

    def test_local_closed_pr_closed_before_launch_is_ignored_as_stale(self):
        active = _active(pid=123, started_at=1_800_000_000.0)
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        ctx.config.apply = True
        ctx.run_state.active_worktrees["1"] = active
        stale_pr = PrRecord(
            number=210,
            head_ref=active.branch,
            changed_files=(),
            closes_issue_numbers=(active.issue_number,),
            created_at="2026-01-01T00:00:00Z",
            closed_at="2026-01-02T00:00:00Z",
            state="CLOSED",
        )
        with (
            patch("orchestune.dispatch_gc.is_process_alive", return_value=False),
            patch("orchestune.dispatch_gc.github.list_prs", return_value=[stale_pr]),
            patch(
                "orchestune.dispatch_gc._finalize_completed_worktree",
                return_value={"action": "completed_no_commits"},
            ),
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is not None
        assert outcome.completion_event["action"] == "completed_no_commits"
        assert "1" not in ctx.run_state.active_worktrees

    def test_local_existing_pr_closed_after_launch_is_requeued(self):
        active = _active(pid=123, started_at=1_800_000_000.0)
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        ctx.config.apply = True
        ctx.run_state.active_worktrees["1"] = active
        closed_pr = PrRecord(
            number=210,
            head_ref=active.branch,
            changed_files=(),
            closes_issue_numbers=(active.issue_number,),
            created_at="2026-01-01T00:00:00Z",
            closed_at="2030-01-01T00:00:00Z",
            state="CLOSED",
        )
        with (
            patch("orchestune.dispatch_gc.is_process_alive", return_value=False),
            patch("orchestune.dispatch_gc.github.list_prs", return_value=[closed_pr]),
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc.remove_worktree"),
            patch("orchestune.dispatch_gc.github.remove_label"),
            patch("orchestune.dispatch_gc.github.add_label"),
            patch("orchestune.dispatch_gc.github.add_comment"),
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is not None
        assert outcome.completion_event["action"] == "abandoned_pr_requeued"
        assert outcome.completed_subtask_id is None

    def test_all_state_lookup_failure_holds_local_completion_for_retry(self):
        active = _active(pid=123)
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        with (
            patch("orchestune.dispatch_gc.is_process_alive", return_value=False),
            patch(
                "orchestune.dispatch_gc.github.list_prs",
                side_effect=RuntimeError("temporary GitHub failure"),
            ),
            patch(
                "orchestune.dispatch_gc._finalize_completed_worktree",
                return_value={"action": "completed_no_commits"},
            ) as mock_finalize,
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is None
        mock_finalize.assert_not_called()

    def test_recovered_entry_uses_all_state_prs_and_requeues_closed_pr(self):
        active = _active(pid=None, started_at=None)
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        ctx.config.apply = True
        ctx.run_state.active_worktrees["1"] = active
        closed_pr = PrRecord(
            number=210,
            head_ref=active.branch,
            changed_files=(),
            closes_issue_numbers=(active.issue_number,),
            state="CLOSED",
        )
        with (
            patch(
                "orchestune.dispatch_gc.github.list_prs", return_value=[closed_pr]
            ) as mock_list_prs,
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc.remove_worktree"),
            patch("orchestune.dispatch_gc.github.remove_label"),
            patch("orchestune.dispatch_gc.github.add_label"),
            patch("orchestune.dispatch_gc.github.add_comment"),
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is not None
        assert outcome.completion_event["action"] == "abandoned_pr_requeued"
        assert outcome.completed_subtask_id is None
        mock_list_prs.assert_called_once_with(state="all")

    def test_pending_cloud_completion_status_returns_none(self):
        active = _active(external_id="session-1")
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        with patch.object(
            ctx.config.dispatch_target,
            "completion_status",
            return_value="pending",
            create=True,
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is None

    def test_dirty_worktree_is_terminal(self):
        active = _active()
        task = _task()
        ctx = _ctx()
        with (
            patch(
                "orchestune.dispatch_gc._is_worktree_complete",
                return_value=True,
            ),
            patch("orchestune.dispatch_gc.github.list_prs", return_value=[]),
            patch(
                "orchestune.dispatch_gc._finalize_completed_worktree",
                return_value={"action": "completion_skipped_dirty_worktree"},
            ),
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is not None
        assert outcome.terminal is True
        assert outcome.completion_event["action"] == "completion_skipped_dirty_worktree"

    def test_completed_worktree_inherits_base_branch(self):
        active = _active(base_branch="parent-branch")
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        ctx.config.apply = True
        ctx.run_state.active_worktrees["1"] = active

        with (
            patch(
                "orchestune.dispatch_gc._is_worktree_complete",
                return_value=True,
            ),
            patch("orchestune.dispatch_gc.github.list_prs", return_value=[]),
            patch(
                "orchestune.dispatch_gc._finalize_completed_worktree",
                return_value={"action": "completed", "commit_sha": "abc123d"},
            ),
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is not None
        assert len(ctx.run_state.completed_worktrees) == 1
        assert ctx.run_state.completed_worktrees[0].base_branch == "parent-branch"

    def test_completed_worktree_preserves_unknown_start_time(self):
        active = _active(started_at=None)
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        ctx.config.apply = True
        ctx.run_state.active_worktrees["1"] = active
        ctx.prs = [
            PrRecord(
                number=281,
                head_ref="agent/issue-280-task-a",
                changed_files=(),
                closes_issue_numbers=(280,),
            )
        ]

        with (
            patch("orchestune.dispatch_gc.github.list_prs", return_value=ctx.prs),
            patch(
                "orchestune.dispatch_gc._finalize_completed_worktree",
                return_value={"action": "completed", "commit_sha": "abc123d"},
            ),
        ):
            _rule_completed(ctx, "1", active, task)

        assert ctx.run_state.completed_worktrees[0].started_at is None


class TestWorktreeHasNewCommitsIntegration:
    """#172回帰テスト: ローカルに parent/issue-<N> ブランチがなく、
    origin/parent/issue-<N> のみ存在する状況で、子ブランチへコミットが積まれている場合に
    worktree_has_new_commits が正しく True を返すことを検証する。"""

    def test_worktree_has_new_commits_parent_remote_only(self, tmp_path):
        import subprocess

        # 1. リモートとローカルのリポジトリをセットアップ
        remote_dir = tmp_path / "remote"
        remote_dir.mkdir()
        local_dir = tmp_path / "local"
        local_dir.mkdir()

        # リモート初期化
        subprocess.run(["git", "init", "--bare"], cwd=str(remote_dir), check=True)

        # ローカル初期化と最初のコミット
        subprocess.run(["git", "init"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "checkout", "-b", "main"], cwd=str(local_dir), check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"], cwd=str(local_dir), check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(local_dir),
            check=True,
        )

        # initial commit
        (local_dir / "file.txt").write_text("initial")
        subprocess.run(["git", "add", "file.txt"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial commit"], cwd=str(local_dir), check=True
        )

        # リモートを登録
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote_dir)],
            cwd=str(local_dir),
            check=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"], cwd=str(local_dir), check=True
        )

        # 2. 親ブランチの作成とリモートへのプッシュ
        subprocess.run(
            ["git", "checkout", "-b", "parent/issue-129"],
            cwd=str(local_dir),
            check=True,
        )
        (local_dir / "file.txt").write_text("parent commit")
        subprocess.run(["git", "add", "file.txt"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "parent commit"], cwd=str(local_dir), check=True
        )
        subprocess.run(
            ["git", "push", "origin", "parent/issue-129"],
            cwd=str(local_dir),
            check=True,
        )

        # 3. ローカルの parent/issue-129 ブランチを削除（リモート追跡のみ残す）
        subprocess.run(["git", "checkout", "main"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "branch", "-D", "parent/issue-129"], cwd=str(local_dir), check=True
        )

        # 4. 子タスク用ブランチを origin/parent/issue-129 から作成し、新規コミットを追加
        subprocess.run(
            [
                "git",
                "checkout",
                "-b",
                "claude/issue-130-task",
                "origin/parent/issue-129",
            ],
            cwd=str(local_dir),
            check=True,
        )
        (local_dir / "file.txt").write_text("child commit")
        subprocess.run(["git", "add", "file.txt"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "child commit"], cwd=str(local_dir), check=True
        )

        # 5. 検証: worktree_has_new_commits に "parent/issue-129" を渡したときに、True が返ることを確認する。
        assert worktree_has_new_commits(local_dir, "parent/issue-129") is True

    def test_prefers_local_parent_when_remote_parent_is_stale(self, tmp_path):
        import subprocess

        # 1. リモートとローカルのリポジトリをセットアップ
        remote_dir = tmp_path / "remote"
        remote_dir.mkdir()
        local_dir = tmp_path / "local"
        local_dir.mkdir()

        # リモート初期化
        subprocess.run(["git", "init", "--bare"], cwd=str(remote_dir), check=True)

        # ローカル初期化と最初のコミット
        subprocess.run(["git", "init"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "checkout", "-b", "main"], cwd=str(local_dir), check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"], cwd=str(local_dir), check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(local_dir),
            check=True,
        )

        # initial commit
        (local_dir / "file.txt").write_text("initial")
        subprocess.run(["git", "add", "file.txt"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial commit"], cwd=str(local_dir), check=True
        )

        # リモートを登録し、main を push
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote_dir)],
            cwd=str(local_dir),
            check=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"], cwd=str(local_dir), check=True
        )

        # 2. 親ブランチ作成し、コミット A を作成
        subprocess.run(
            ["git", "checkout", "-b", "parent/issue-129"],
            cwd=str(local_dir),
            check=True,
        )
        (local_dir / "file.txt").write_text("commit A")
        subprocess.run(["git", "add", "file.txt"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "commit A"], cwd=str(local_dir), check=True
        )

        # リモートへ push (リモートの parent/issue-129 はコミット A を指す)
        subprocess.run(
            ["git", "push", "origin", "parent/issue-129"],
            cwd=str(local_dir),
            check=True,
        )

        # 3. ローカルの parent/issue-129 に追加のコミット B を積む (ローカル parent/issue-129: A - B)
        # (リモートへは push しない。これによりリモート追跡はコミット A を指したまま)
        (local_dir / "file.txt").write_text("commit B")
        subprocess.run(["git", "add", "file.txt"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "commit B"], cwd=str(local_dir), check=True
        )

        # 4. 子ブランチをローカルの parent/issue-129 から作成 (子固有のコミットはなし、HEAD はコミット B)
        subprocess.run(
            [
                "git",
                "checkout",
                "-b",
                "claude/issue-130-task",
                "parent/issue-129",
            ],
            cwd=str(local_dir),
            check=True,
        )

        # 5. 検証: ローカルを優先して解決するため、HEAD (B) と ローカル parent (B) を比較し、新規コミットなし (False) となることを確認。
        # (もしリモート優先バグがあると、HEAD (B) と origin/parent (A) を比較して新規コミットあり (True) と判定されてしまう)
        assert worktree_has_new_commits(local_dir, "parent/issue-129") is False
