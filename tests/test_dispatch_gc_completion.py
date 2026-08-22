"""dispatch_gc内の完了ワークツリー処理（dispatch_gc_completion）テスト。

`tests/test_dispatch_gc.py`の肥大化解消のため分割している（#345）。
Zombie・Timeout回収は`test_dispatch_gc_zombies.py`、gitプリミティブや
`dispatch_gc.py`自身のルール・エンドツーエンド統合テストは
`test_dispatch_gc.py`に残している。
"""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_gc_completion import (
    _decide_completed_worktree_outcome,
    _decide_not_needed_dirty_worktree,
    _finalize_abandoned_cloud_worktree,
    _finalize_completed_worktree,
    _finalize_not_needed_worktree,
    _is_worktree_complete,
    _local_pr_completion_status,
)
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree
from orchestune.dispatch_targets import (
    ClaudeCodeCloudRoutineDispatchTarget,
    CodexCloudDispatchTarget,
)
from orchestune.models import PrRecord
from orchestune.outcome_record import OutcomeRecord

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-state-"))


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


class TestFinalizeCompletedWorktree:
    """#74: プロセス終了検知後の完了処理。空コミット完了を実完了と誤判定しないこと。"""

    def test_no_new_commits_is_not_treated_as_completed(self, tmp_path):
        """#74再現: worktreeはcleanだがbase_branchに対して新規コミットが0件の場合、
        status:doneを付与せず、依存先タスクの誤昇格を防ぐためcompleted以外のアクションにする。"""
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)
        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
        ):
            event = _finalize_completed_worktree(active, task, config)

        assert event["action"] != "completed"
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        mock_remove_label.assert_called_once_with(280, "status:in-progress")
        mock_add_label.assert_called_once_with(280, "status:blocked-human-review")
        mock_add_comment.assert_called_once()
        assert mock_add_comment.call_args.args[0] == 280

    def test_new_commits_without_outcome_is_escalated_to_blocked_human_review(
        self, tmp_path
    ):
        """コミットがあってもoutcomeレコードが存在しない場合はcompletedにせずstatus:blocked-human-reviewに倒す。"""
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)
        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=True,
            ),
            patch("orchestune.dispatch_gc_git.subprocess.run") as mock_run,
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
            patch("orchestune.forge.GitHubForge.list_comments", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="deadbeef\n", stderr=""
            )
            event = _finalize_completed_worktree(active, task, config)

        assert event["action"] == "completed_without_outcome"
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        mock_remove_label.assert_called_once_with(280, "status:in-progress")
        mock_add_label.assert_called_once_with(280, "status:blocked-human-review")
        mock_add_comment.assert_called_once()
        assert (
            "完了宣言レコード（orchestune:outcome）が検出できませんでした"
            in mock_add_comment.call_args.args[1]
        )

    def test_new_commits_with_outcome_done_is_treated_as_completed(self, tmp_path):
        """base_branchに対する実コミットがありoutcome(done)があればcompleted+status:doneとする。"""
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)
        outcome = OutcomeRecord(result="done", issue=280)
        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=True,
            ),
            patch("orchestune.dispatch_gc_git.subprocess.run") as mock_run,
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
            patch(
                "orchestune.forge.GitHubForge.list_comments",
                return_value=[
                    {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
                ],
            ),
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
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

    def test_new_commits_with_outcome_not_needed_is_routed_to_not_needed(
        self, tmp_path
    ):
        """outcome(not-needed)の場合はnot-needed経路（クローズまたは検証レビュー）へ流す。"""
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)
        outcome = OutcomeRecord(result="not-needed", issue=280)
        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=True,
            ),
            patch("orchestune.dispatch_gc_git.subprocess.run") as mock_run,
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
            patch(
                "orchestune.forge.GitHubForge.list_comments",
                return_value=[
                    {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
                ],
            ),
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.close_issue") as mock_close_issue,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="deadbeef\n", stderr=""
            )
            event = _finalize_completed_worktree(active, task, config)

        assert event["action"] == "not_needed"
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        mock_remove_label.assert_called_once_with(280, "status:in-progress")
        mock_close_issue.assert_called_once()
        assert mock_close_issue.call_args.args[0] == 280

    def test_outcome_forge_error_skips_completion(self, tmp_path):
        """forgeエラー時は完了と判定せずスキップする（fail-closed）。"""
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)
        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=True,
            ),
            patch(
                "orchestune.forge.GitHubForge.list_comments",
                side_effect=RuntimeError("connection error"),
            ),
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
        ):
            event = _finalize_completed_worktree(active, task, config)

        assert event["action"] == "completion_skipped_forge_error"
        mock_remove_worktree.assert_not_called()

    def test_completed_also_removes_stale_queued_label(self, tmp_path):
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:queued", "status:in-progress"))
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)
        outcome = OutcomeRecord(result="done", issue=280)
        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=True,
            ),
            patch("orchestune.dispatch_gc_git.subprocess.run") as mock_run,
            patch("orchestune.dispatch_gc_completion.remove_worktree"),
            patch(
                "orchestune.forge.GitHubForge.list_comments",
                return_value=[
                    {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
                ],
            ),
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="deadbeef\n", stderr=""
            )
            event = _finalize_completed_worktree(active, task, config)

        assert event["action"] == "completed"
        mock_add_label.assert_called_once_with(280, "status:done")
        mock_remove_label.assert_any_call(280, "status:in-progress")
        mock_remove_label.assert_any_call(280, "status:queued")
        assert mock_remove_label.call_count == 2

    def test_completed_adds_done_before_removing_in_progress(self, tmp_path):
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)
        outcome = OutcomeRecord(result="done", issue=280)
        call_order: list[tuple[str, str]] = []
        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=True,
            ),
            patch("orchestune.dispatch_gc_git.subprocess.run") as mock_run,
            patch("orchestune.dispatch_gc_completion.remove_worktree"),
            patch(
                "orchestune.forge.GitHubForge.list_comments",
                return_value=[
                    {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
                ],
            ),
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch(
                "orchestune.forge.GitHubForge.add_label",
                side_effect=lambda issue, label: call_order.append(("add", label)),
            ),
            patch(
                "orchestune.forge.GitHubForge.remove_label",
                side_effect=lambda issue, label: call_order.append(("remove", label)),
            ),
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="deadbeef\n", stderr=""
            )
            _finalize_completed_worktree(active, task, config)

        assert call_order == [
            ("add", "status:done"),
            ("remove", "status:in-progress"),
        ]


class TestFinalizeAbandonedCloudWorktree:
    """#381レビュー対応(Codex P2): PRがマージされずクローズされた際の
    再キューイングが、中断した以前の遷移で取り残された一次status:*ラベルを
    正しく後始末することを検証する。"""

    def test_removes_stale_blocked_label_alongside_in_progress(self, tmp_path):
        # stacked launch中断で取り残されたstatus:blockedも併せて除去し、
        # status:queuedへ確実に収束させなければならない。
        active = _active()
        task = _task(status_labels=("status:blocked", "status:in-progress"))
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)

        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc_completion.remove_worktree"),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment"),
        ):
            event = _finalize_abandoned_cloud_worktree(active, task, config)

        assert event["action"] == "abandoned_pr_requeued"
        mock_add_label.assert_called_once_with(280, "status:queued")
        mock_remove_label.assert_any_call(280, "status:in-progress")
        mock_remove_label.assert_any_call(280, "status:blocked")
        assert mock_remove_label.call_count == 2

    def test_does_not_overwrite_terminal_escalation_label(self, tmp_path):
        # 中断した以前の遷移でstatus:blocked-human-reviewが既に付与されている
        # 場合、status:queuedへの書き換えは人間の確認要求を握りつぶして
        # しまうため、ラベルには一切触れてはならない。
        active = _active()
        task = _task(
            status_labels=("status:blocked-human-review", "status:in-progress")
        )
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)

        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc_completion.remove_worktree"),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
        ):
            event = _finalize_abandoned_cloud_worktree(active, task, config)

        assert event["action"] == "abandoned_pr_requeued"
        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()
        mock_add_comment.assert_called_once()


class TestFinalizeNotNeededWorktree:
    """#280: status:not-neededラベル検知による完全自動クローズ。"""

    def test_apply_removes_worktree_and_closes_issue(self, tmp_path):
        active = _active()
        task = _task()
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)
        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.close_issue") as mock_close_issue,
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

    def test_dirty_worktree_is_not_closed(self, tmp_path):
        """未コミットの作業が残っている場合は、安全側に倒しクローズを見送る。"""
        active = _active()
        task = _task()
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)
        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.close_issue") as mock_close_issue,
        ):
            event = _finalize_not_needed_worktree(active, task, config)

        mock_remove_worktree.assert_not_called()
        mock_remove_label.assert_not_called()
        mock_close_issue.assert_not_called()
        assert event["action"] == "completion_skipped_dirty_worktree"

    def test_dry_run_does_not_call_github_or_mutate(self, tmp_path):
        active = _active()
        task = _task()
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl", apply=False
        )
        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.close_issue") as mock_close_issue,
        ):
            event = _finalize_not_needed_worktree(active, task, config)

        mock_remove_worktree.assert_not_called()
        mock_remove_label.assert_not_called()
        mock_close_issue.assert_not_called()
        assert event["action"] == "not_needed"

    def test_none_task_defaults_subtask_id_to_empty_string(self, tmp_path):
        active = _active()
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)
        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc_completion.remove_worktree"),
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch("orchestune.forge.GitHubForge.close_issue"),
        ):
            event = _finalize_not_needed_worktree(active, None, config)
        assert event["subtask_id"] == ""


class TestFinalizeNotNeededWorktreeCloudRoutineReview:
    """#282: クラウドルーチン利用可能時は即時クローズせず独立検証レビューへ委譲する。"""

    def _cloud_config(self, tmp_path, **overrides):
        defaults = dict(
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            dispatch_target=ClaudeCodeCloudRoutineDispatchTarget("rid", "rtok"),
        )
        defaults.update(overrides)
        return DispatcherConfig(**defaults)

    def test_dispatches_review_instead_of_closing(self, tmp_path):
        active = _active()
        task = _task()
        config = self._cloud_config(
            tmp_path,
            not_needed_review_state_path=tmp_path / "state.json",
        )
        dispatch_review = MagicMock()
        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.close_issue") as mock_close_issue,
        ):
            event = _finalize_not_needed_worktree(
                active,
                task,
                config,
                dispatch_not_needed_review=dispatch_review,
            )

        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        mock_remove_label.assert_called_once_with(280, "status:in-progress")
        mock_close_issue.assert_not_called()
        dispatch_review.assert_called_once_with(280, "task-a", config)
        assert event["action"] == "not_needed_review_dispatched"
        assert event["subtask_id"] == "task-a"

    def test_dirty_worktree_does_not_dispatch_review(self, tmp_path):
        active = _active()
        task = _task()
        config = self._cloud_config(
            tmp_path,
            not_needed_review_state_path=tmp_path / "state.json",
        )
        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
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
            "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
            return_value=True,
        ):
            decision = _decide_completed_worktree_outcome(active, task)
        assert decision.action == "completion_skipped_dirty_worktree"

    def test_no_new_commits_is_completed_no_commits(self):
        active = _active()
        task = _task()
        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=False,
            ),
        ):
            decision = _decide_completed_worktree_outcome(active, task)
        assert decision.action == "completed_no_commits"
        assert decision.subtask_id == "task-a"

    def test_clean_with_new_commits_without_outcome_is_completed_without_outcome(self):
        active = _active()
        task = _task()
        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=True,
            ),
        ):
            decision = _decide_completed_worktree_outcome(active, task)
        assert decision.action == "completed_without_outcome"
        assert decision.subtask_id == "task-a"

    def test_clean_with_new_commits_and_outcome_done_is_completed(self):
        active = _active()
        task = _task()
        fake_forge = MagicMock()
        outcome = OutcomeRecord(result="done", issue=280)
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
        ]
        fake_forge.list_prs.return_value = []
        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=True,
            ),
        ):
            decision = _decide_completed_worktree_outcome(
                active, task, forge=fake_forge
            )
        assert decision.action == "completed"
        assert decision.subtask_id == "task-a"

    def test_clean_with_new_commits_and_outcome_not_needed_is_not_needed(self):
        active = _active()
        task = _task()
        fake_forge = MagicMock()
        outcome = OutcomeRecord(result="not-needed", issue=280)
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
        ]
        fake_forge.list_prs.return_value = []
        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=True,
            ),
        ):
            decision = _decide_completed_worktree_outcome(
                active, task, forge=fake_forge
            )
        assert decision.action == "not_needed"
        assert decision.subtask_id == "task-a"

    def test_ignores_stale_closed_pr_outcome(self):
        active = _active(started_at=1700000100.0)
        task = _task()
        fake_forge = MagicMock()
        fake_forge.list_comments.side_effect = lambda num: (
            []
            if num == 280
            else [
                {
                    "body": OutcomeRecord(result="done", issue=280).render(),
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ]
        )
        stale_closed_pr = PrRecord(
            number=999,
            head_ref="claude/issue-280-task-a",
            changed_files=(),
            state="CLOSED",
            closed_at="2023-11-14T22:14:00Z",  # Before started_at (1700000100.0 is Nov 14 2023 22:15:00 UTC)
        )
        fake_forge.list_prs.return_value = [stale_closed_pr]
        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=True,
            ),
        ):
            decision = _decide_completed_worktree_outcome(
                active, task, forge=fake_forge
            )
        # Since the stale PR is ignored and Issue #280 has no comments, it is completed_without_outcome
        assert decision.action == "completed_without_outcome"
        assert decision.subtask_id == "task-a"


class TestDecideNotNeededDirtyWorktree:
    def test_true_when_dirty(self):
        with patch(
            "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
            return_value=True,
        ):
            assert _decide_not_needed_dirty_worktree(_active()) is True

    def test_false_when_clean(self):
        with patch(
            "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
            return_value=False,
        ):
            assert _decide_not_needed_dirty_worktree(_active()) is False


class TestIsWorktreeComplete:
    """#239: external_id経由の完了判定に、issue_numberが正しく引き渡されること。"""

    def test_passes_issue_number_to_dispatch_target_handle(self, tmp_path):
        fake_target = MagicMock()
        fake_target.completion_status.return_value = "completed"
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            dispatch_target=fake_target,
        )
        active = ActiveWorktree(
            issue_number=218,
            branch="claude/issue-218-review-history-backend-api",
            worktree_path=str(tmp_path / "w1"),
            pid=None,
            started_at=1_699_999_000.0,
            declared_footprint=("src/foo.py",),
            external_id="session_1",
            external_url="https://claude.ai/code/session_1",
        )

        result = _is_worktree_complete(active, config)

        assert result is True
        handle = fake_target.completion_status.call_args.args[0]
        assert handle.issue_number == 218
        assert handle.branch_name == "claude/issue-218-review-history-backend-api"

    def test_codex_cloud_active_worktree_waits_for_pr(self, tmp_path):
        target = CodexCloudDispatchTarget("env_123")
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            dispatch_target=target,
        )
        active = ActiveWorktree(
            issue_number=1,
            branch="claude/issue-1-task-a",
            worktree_path=str(tmp_path / "w1"),
            pid=4242,
            started_at=1_699_999_000.0,
            declared_footprint=("src/foo.py",),
            external_id="codex-cloud:claude/issue-1-task-a",
        )

        with (
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch(
                "orchestune.dispatch_gc_completion.is_process_alive"
            ) as mock_is_alive,
        ):
            assert _is_worktree_complete(active, config) is False

        mock_is_alive.assert_not_called()

    def test_recovered_local_active_worktree_waits_for_pid_reconciliation(
        self, tmp_path
    ):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
        )
        active = ActiveWorktree(
            issue_number=1,
            branch="claude/issue-1-task-a",
            worktree_path=str(tmp_path / "missing-worktree"),
            pid=None,
            started_at=None,
            declared_footprint=(),
        )

        assert _is_worktree_complete(active, config) is False


class TestLocalPrCompletionStatusWithFakeForge:
    """#292: `mock.patch`によるグローバルなクラスメソッド差し替えではなく、
    `DispatcherConfig(forge=...)`への注入だけでテストが書けることを示す。"""

    def test_uses_injected_fake_forge_instead_of_patching(self, tmp_path):
        fake_forge = MagicMock()
        fake_forge.list_prs.return_value = [
            PrRecord(
                number=1,
                head_ref="claude/issue-1-task-1",
                changed_files=(),
                state="MERGED",
            )
        ]
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
        )
        active = ActiveWorktree(
            issue_number=1,
            branch="claude/issue-1-task-1",
            worktree_path=str(tmp_path / "worktrees/claude-issue-1-task-1"),
            pid=123,
            started_at=1700000000.0,
            declared_footprint=(),
        )

        status = _local_pr_completion_status(active, config)

        assert status == "completed"
        fake_forge.list_prs.assert_called_once_with(state="all")

    def test_open_pr_without_outcome_is_pending(self, tmp_path):
        fake_forge = MagicMock()
        fake_forge.list_prs.return_value = [
            PrRecord(
                number=1,
                head_ref="claude/issue-1-task-1",
                changed_files=(),
                state="OPEN",
            )
        ]
        fake_forge.list_comments.return_value = []
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
        )
        active = ActiveWorktree(
            issue_number=1,
            branch="claude/issue-1-task-1",
            worktree_path=str(tmp_path / "worktrees/claude-issue-1-task-1"),
            pid=123,
            started_at=1700000000.0,
            declared_footprint=(),
        )

        status = _local_pr_completion_status(active, config)

        assert status == "pending"

    def test_open_pr_with_outcome_done_is_completed(self, tmp_path):
        fake_forge = MagicMock()
        outcome = OutcomeRecord(result="done", issue=1, pr=1)
        fake_forge.list_prs.return_value = [
            PrRecord(
                number=1,
                head_ref="claude/issue-1-task-1",
                changed_files=(),
                state="OPEN",
            )
        ]
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
        ]
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
        )
        active = ActiveWorktree(
            issue_number=1,
            branch="claude/issue-1-task-1",
            worktree_path=str(tmp_path / "worktrees/claude-issue-1-task-1"),
            pid=123,
            started_at=1700000000.0,
            declared_footprint=(),
        )

        status = _local_pr_completion_status(active, config)

        assert status == "completed"

    def test_open_pr_with_forge_error_is_unknown(self, tmp_path):
        fake_forge = MagicMock()
        fake_forge.list_prs.return_value = [
            PrRecord(
                number=1,
                head_ref="claude/issue-1-task-1",
                changed_files=(),
                state="OPEN",
            )
        ]
        fake_forge.list_comments.side_effect = RuntimeError("network error")
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
        )
        active = ActiveWorktree(
            issue_number=1,
            branch="claude/issue-1-task-1",
            worktree_path=str(tmp_path / "worktrees/claude-issue-1-task-1"),
            pid=123,
            started_at=1700000000.0,
            declared_footprint=(),
        )

        status = _local_pr_completion_status(active, config)

        assert status == "unknown"

    def test_open_pr_with_blocked_outcome_is_completed(self, tmp_path):
        fake_forge = MagicMock()
        fake_forge.list_prs.return_value = [
            PrRecord(
                number=1,
                head_ref="claude/issue-1-task-1",
                changed_files=(),
                state="OPEN",
            )
        ]
        outcome = OutcomeRecord(
            result="blocked",
            issue=1,
            reason="base-branch-red",
            base_sha="abc1234",
            attempt=1,
        )
        fake_forge.list_comments.return_value = [
            {
                "body": outcome.render(),
                "created_at": "2026-01-01T00:00:10Z",
            }
        ]
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
        )
        active = ActiveWorktree(
            issue_number=1,
            branch="claude/issue-1-task-1",
            worktree_path=str(tmp_path / "worktrees/claude-issue-1-task-1"),
            pid=123,
            started_at=1700000000.0,
            declared_footprint=(),
        )

        status = _local_pr_completion_status(active, config)

        assert status == "completed"


class TestFinalizeBaseBranchRedWorktree:
    def test_base_branch_red_attempt_1_blocks_and_adds_marker(self, tmp_path):
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        outcome = OutcomeRecord(
            result="blocked",
            issue=280,
            reason="base-branch-red",
            base_sha="abc1234",
            attempt=1,
        )
        fake_forge = MagicMock()
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:10Z"}
        ]
        fake_forge.list_prs.return_value = []
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            forge=fake_forge,
        )
        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
        ):
            event = _finalize_completed_worktree(active, task, config)

        assert event["action"] == "blocked_base_branch_red"
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        fake_forge.add_label.assert_any_call(280, "status:blocked")
        fake_forge.add_label.assert_any_call(280, "ci:base-branch-red")
        fake_forge.remove_label.assert_called_once_with(280, "status:in-progress")
        fake_forge.add_comment.assert_called_once()

    def test_base_branch_red_attempt_3_escalates_to_human_review(self, tmp_path):
        active = _active(base_branch="origin/main")
        task = _task(status_labels=("status:in-progress",))
        outcome = OutcomeRecord(
            result="blocked",
            issue=280,
            reason="base-branch-red",
            base_sha="abc1234",
            attempt=3,
        )
        fake_forge = MagicMock()
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:10Z"}
        ]
        fake_forge.list_prs.return_value = []
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            forge=fake_forge,
        )
        with (
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
        ):
            event = _finalize_completed_worktree(active, task, config)

        assert event["action"] == "escalated_base_branch_red"
        mock_remove_worktree.assert_called_once_with("worktrees/w1")
        fake_forge.add_label.assert_called_once_with(280, "status:blocked-human-review")
        fake_forge.remove_label.assert_any_call(280, "status:in-progress")
        fake_forge.add_comment.assert_called_once()
