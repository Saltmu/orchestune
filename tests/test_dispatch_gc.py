"""dispatch_gcの共有gitプリミティブ・完了ルール本体・エンドツーエンド統合テスト。

Zombie・Timeout回収（dispatch_gc_zombies）は`test_dispatch_gc_zombies.py`、
完了ワークツリー処理（dispatch_gc_completion）は`test_dispatch_gc_completion.py`
へそれぞれ分割している（#345）。本ファイルには、両モジュールから共有される
gitプリミティブ（`dispatch_gc_git`）のテスト、`dispatch_gc.py`自身が持つ
`_rule_completed`/`_decide_stale_active_entry`のテスト、および
`run_dispatch_cycle`経由のエンドツーエンド統合テストを残している。
"""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_cycle import run_dispatch_cycle
from orchestune.dispatch_gc import (
    ZombieOrTimeoutReclaim,
    _apply_stale_active_entry_discard,
    _collect_zombies_and_timeouts,
    _decide_stale_active_entry,
    _finalize_completed_worktree,
    _rule_completed,
    _rule_stale_entry,
    backup_wip_commit,
    remote_branch_commit_sha_if_ahead,
    remove_worktree,
    worktree_has_new_commits,
    worktree_has_uncommitted_changes,
)
from orchestune.dispatch_gc_completion import (
    CompletedWorktreeDecision as ExtractedCompletedWorktreeDecision,
)
from orchestune.dispatch_gc_completion import (
    _finalize_completed_worktree as extracted_finalize_completed_worktree,
)
from orchestune.dispatch_gc_git import prune_stale_integration_temp_branches
from orchestune.dispatch_gc_zombies import (
    ZombieOrTimeoutReclaim as ExtractedZombieOrTimeoutReclaim,
)
from orchestune.dispatch_gc_zombies import (
    _collect_zombies_and_timeouts as extracted_collect_zombies_and_timeouts,
)
from orchestune.dispatch_rules import CycleContext
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import (
    ActiveWorktree,
    RunState,
    load_run_state,
    save_run_state,
)
from orchestune.models import PrRecord
from tests.conftest import make_issue

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-state-"))


def test_extracted_gc_symbols_remain_available_from_legacy_module():
    """#330: 分割後も既存のdispatch_gc importを壊さない。"""
    from orchestune.dispatch_gc import CompletedWorktreeDecision

    assert CompletedWorktreeDecision is ExtractedCompletedWorktreeDecision
    assert _finalize_completed_worktree is extracted_finalize_completed_worktree
    assert ZombieOrTimeoutReclaim is ExtractedZombieOrTimeoutReclaim
    assert _collect_zombies_and_timeouts is extracted_collect_zombies_and_timeouts


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
        config=DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        ),
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


def _issue(
    number,
    labels=("status:queued",),
    footprint=("src/foo.py",),
    symbols=("foo.Foo",),
    subtask_id="task-a",
    depends_on=(),
    created_at="2026-01-01T00:00:00+00:00",
    parent_number=181,
):
    """`tests/conftest.py`の`make_issue`に、このファイルの旧テスト群が前提と
    する`parent_number`（既定181）とtitleを合わせた薄いラッパー。"""
    parent = {"number": parent_number} if parent_number is not None else None
    return make_issue(
        number,
        title="t",
        labels=labels,
        footprint=footprint,
        symbols=symbols,
        subtask_id=subtask_id,
        depends_on=depends_on,
        created_at=created_at,
        parent=parent,
    )


class TestWorktreeHasUncommittedChanges:
    """#193: worktree削除前の未コミット変更確認（安全側フォールバック）。"""

    def test_clean_worktree_returns_false(self):
        with patch("orchestune.dispatch_gc_git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            assert worktree_has_uncommitted_changes("worktrees/w1") is False

    def test_dirty_worktree_returns_true(self):
        with patch("orchestune.dispatch_gc_git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=" M src/foo.py\n", stderr=""
            )
            assert worktree_has_uncommitted_changes("worktrees/w1") is True

    def test_git_error_defaults_to_clean(self):
        """存在しないworktreeなどgit statusが失敗する場合はクオータ解放を優先し、
        削除を妨げないようクリーン扱いとする。"""
        with patch(
            "orchestune.dispatch_gc_git.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, []),
        ):
            assert worktree_has_uncommitted_changes("worktrees/missing") is False


class TestPruneStaleIntegrationTempBranches:
    def test_deletes_only_old_temp_branches_without_open_pr(self, fake_forge):
        # #435: クラッシュ等で残ったtemp branchだけを回収し、レビュー中の
        # 統合PRのheadや作成直後の並行ランを誤って削除してはならない。
        with patch("orchestune.dispatch_gc_git.run_git") as run_git:
            run_git.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=(
                    "origin/integration/temp-parent-issue-1-old 100\n"
                    "origin/integration/temp-parent-issue-1-open 100\n"
                    "origin/integration/temp-parent-issue-1-fresh 950\n"
                ),
                stderr="",
            )
            fake_forge.list_open_prs.return_value = [
                PrRecord(
                    number=1,
                    head_ref="integration/temp-parent-issue-1-open",
                    changed_files=(),
                )
            ]

            removed = prune_stale_integration_temp_branches(
                Path("/repo"), forge=fake_forge, now=1_000, max_age_seconds=100
            )

        assert removed == ["integration/temp-parent-issue-1-old"]
        fake_forge.delete_branch.assert_called_once_with(
            "integration/temp-parent-issue-1-old"
        )


class TestBackupWipCommit:
    """#213: 破壊的操作（rebase/rmtree）の直前に呼ばれるWIP退避のfail-closed契約。
    `worktree_has_uncommitted_changes`と異なり、dirty確認自体が失敗した場合も
    「clean」とはみなさず、呼び出し側が破壊的操作を中止できるようエラーを返す。
    """

    def test_clean_worktree_returns_none(self):
        with patch("orchestune.dispatch_gc_git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            assert backup_wip_commit("worktrees/w1", "WIP: backup") is None
            # cleanなのでadd/commitは呼ばれない
            assert mock_run.call_count == 1

    def test_dirty_worktree_commits_and_returns_none(self):
        with patch("orchestune.dispatch_gc_git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=" M src/foo.py\n", stderr=""
            )
            assert backup_wip_commit("worktrees/w1", "WIP: backup") is None
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert any("add" in cmd for cmd in calls)
        assert any("commit" in cmd for cmd in calls)

    def test_status_check_failure_is_fail_closed(self):
        """dirty確認自体が失敗した場合、cleanと誤判定せずエラーを返す
        （`worktree_has_uncommitted_changes`のfail-open挙動とは異なる）。"""
        with patch(
            "orchestune.dispatch_gc_git.subprocess.run",
            side_effect=subprocess.CalledProcessError(
                1, [], stderr="fatal: not a git repository"
            ),
        ):
            error = backup_wip_commit("worktrees/w1", "WIP: backup")
        assert error is not None
        assert "fatal: not a git repository" in error

    def test_status_check_os_error_is_fail_closed(self):
        """git実行ファイル不在等のOSErrorも、確認不能としてエラーを返す。"""
        with patch(
            "orchestune.dispatch_gc_git.subprocess.run",
            side_effect=OSError("git executable not found"),
        ):
            error = backup_wip_commit("worktrees/w1", "WIP: backup")
        assert error is not None
        assert "git executable not found" in error

    def test_commit_os_error_is_reported(self):
        """add/commit自体のOSError（`CalledProcessError`以外）も捕捉して返す。"""

        def run_mock(args, **kwargs):
            if "status" in args:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=" M src/foo.py\n", stderr=""
                )
            raise OSError("git executable not found")

        with patch("orchestune.dispatch_gc_git.subprocess.run", side_effect=run_mock):
            error = backup_wip_commit("worktrees/w1", "WIP: backup")
        assert error is not None
        assert "git executable not found" in error


class TestWorktreeHasNewCommits:
    """#74: base_branchに対する実コミットの有無確認（空コミット完了の誤判定防止）。"""

    def test_returns_true_when_commits_ahead(self):
        with patch("orchestune.dispatch_gc_git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="2\n", stderr=""
            )
            assert worktree_has_new_commits("worktrees/w1", "origin/main") is True

    def test_returns_false_when_no_commits_ahead(self):
        with patch("orchestune.dispatch_gc_git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="0\n", stderr=""
            )
            assert worktree_has_new_commits("worktrees/w1", "origin/main") is False

    def test_git_error_falls_back_to_false(self):
        """#135: 比較不能時（base_branch参照が解決できない等）は「新規コミット無し」
        と同じ安全側（False）にフォールバックし、実体のない完了確定を防ぐ。"""
        with patch(
            "orchestune.dispatch_gc_git.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, []),
        ):
            assert worktree_has_new_commits("worktrees/missing", "origin/main") is False

    def test_git_error_logs_warning_to_stderr(self, capsys):
        """#135: 比較失敗時にstderrへ警告を出力し、原因調査を容易にする。"""
        with patch(
            "orchestune.dispatch_gc_git.subprocess.run",
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
                "orchestune.dispatch_gc_git.fetch_remote_branch",
                side_effect=("origin/claude/issue-177-task-a", "origin/main"),
            ) as mock_fetch,
            patch("orchestune.dispatch_gc_git.subprocess.run") as mock_run,
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
                "orchestune.dispatch_gc_git.fetch_remote_branch",
                side_effect=("origin/claude/issue-177-task-a", "origin/main"),
            ) as mock_fetch,
            patch("orchestune.dispatch_gc_git.subprocess.run") as mock_run,
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


class TestRemoveWorktree:
    """#193: 完了したworktreeの削除。"""

    def test_calls_git_worktree_remove_without_force(self):
        with patch("orchestune.dispatch_gc_git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            remove_worktree("worktrees/w1")
        args = mock_run.call_args.args[0]
        assert args == ["git", "worktree", "remove", "worktrees/w1"]
        assert "--force" not in args

    def test_swallows_error_when_already_removed(self):
        with patch(
            "orchestune.dispatch_gc_git.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, []),
        ):
            remove_worktree("worktrees/already-gone")  # 例外を送出しないこと


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


class TestApplyStaleActiveEntryDiscard:
    """#382: 帳簿(run_state)破棄の前に、実際に稼働中かもしれない物理
    worktree・プロセスの後始末を行うことを検証する。"""

    def test_kills_live_process_and_removes_worktree(self, tmp_path):
        active = _active(worktree_path=str(tmp_path), pid=12345)
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)

        with (
            patch(
                "orchestune.dispatch_gc.backup_wip_commit", return_value=None
            ) as mock_backup,
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch("orchestune.dispatch_gc.os.kill") as mock_kill,
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove,
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
            patch("orchestune.dispatch_gc.backup_wip_commit", return_value=None),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=False),
            patch("orchestune.dispatch_gc.os.kill") as mock_kill,
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove,
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
            patch("orchestune.dispatch_gc.backup_wip_commit") as mock_backup,
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch("orchestune.dispatch_gc.os.kill") as mock_kill,
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove,
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

    def test_backup_failure_skips_discard_and_preserves_entry(self, tmp_path):
        active = _active(worktree_path=str(tmp_path), pid=12345)
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True)

        with (
            patch(
                "orchestune.dispatch_gc.backup_wip_commit",
                return_value="fatal: unable to write new index file",
            ),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch("orchestune.dispatch_gc.os.kill") as mock_kill,
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_comment,
        ):
            discarded = _apply_stale_active_entry_discard(
                run_state, "280", active, "test reason", config
            )

        assert discarded is False
        mock_kill.assert_not_called()
        mock_remove.assert_not_called()
        mock_comment.assert_called_once()
        assert "test reason" in mock_comment.call_args.args[1]
        # バックアップ失敗時は帳簿を温存し、次サイクルでの再試行に委ねる。
        assert run_state.active_worktrees == {"280": active}

    def test_dry_run_does_not_touch_anything(self, tmp_path):
        active = _active(worktree_path=str(tmp_path), pid=12345)
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl", apply=False
        )

        with (
            patch("orchestune.dispatch_gc.backup_wip_commit") as mock_backup,
            patch("orchestune.dispatch_gc.os.kill") as mock_kill,
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove,
        ):
            discarded = _apply_stale_active_entry_discard(
                run_state, "280", active, "test reason", config
            )

        assert discarded is True
        mock_backup.assert_not_called()
        mock_kill.assert_not_called()
        mock_remove.assert_not_called()
        assert run_state.active_worktrees == {"280": active}


class TestRuleStaleEntry:
    def test_backup_failure_defers_terminal_outcome_to_next_cycle(self, tmp_path):
        # #382 Reproducer: WIPバックアップ失敗時は、破棄処理をスキップして
        # このサイクルではNoneを返し（terminalな完了イベントを発行しない）、
        # run_stateのエントリも温存して次サイクルでの再試行に委ねる。
        active = _active(worktree_path=str(tmp_path), pid=12345)
        task = _task(status_labels=("status:blocked",))
        run_state = RunState(active_worktrees={"280": active})
        ctx = _ctx(run_state=run_state)
        ctx.config.apply = True

        with (
            patch(
                "orchestune.dispatch_gc.backup_wip_commit",
                return_value="fatal: unable to write new index file",
            ),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch("orchestune.forge.GitHubForge.add_comment"),
        ):
            outcome = _rule_stale_entry(ctx, "280", active, task)

        assert outcome is None
        assert run_state.active_worktrees == {"280": active}


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
            patch(
                "orchestune.dispatch_gc_completion.is_process_alive", return_value=False
            ),
            patch(
                "orchestune.forge.GitHubForge.list_prs",
                return_value=ctx.prs,
            ) as mock_list_prs,
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc_completion.remove_worktree") as mock_remove,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
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

    def test_abandoned_requeue_adds_queued_before_removing_in_progress(self):
        # #381: 途中でクラッシュしてもIssueが必ずいずれかのstatus:*ラベルを
        # 持ち続けるよう、addがremoveより先に呼ばれなければならない。
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
        call_order: list[tuple[str, str]] = []
        with (
            patch(
                "orchestune.dispatch_gc_completion.is_process_alive", return_value=False
            ),
            patch("orchestune.forge.GitHubForge.list_prs", return_value=ctx.prs),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc_completion.remove_worktree"),
            patch(
                "orchestune.forge.GitHubForge.remove_label",
                side_effect=lambda issue, label: call_order.append(("remove", label)),
            ),
            patch(
                "orchestune.forge.GitHubForge.add_label",
                side_effect=lambda issue, label: call_order.append(("add", label)),
            ),
            patch("orchestune.forge.GitHubForge.add_comment"),
        ):
            _rule_completed(ctx, "1", active, task)

        assert call_order == [
            ("add", "status:queued"),
            ("remove", "status:in-progress"),
        ]

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
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc_completion.remove_worktree") as mock_remove,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
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
            patch(
                "orchestune.dispatch_gc_completion.is_process_alive", return_value=True
            ),
            patch("orchestune.forge.GitHubForge.list_prs") as mock_list_prs,
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
            patch(
                "orchestune.dispatch_gc_completion.is_process_alive", return_value=False
            ),
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[stale_pr]),
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
            patch(
                "orchestune.dispatch_gc_completion.is_process_alive", return_value=False
            ),
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[closed_pr]),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc_completion.remove_worktree"),
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch("orchestune.forge.GitHubForge.add_label"),
            patch("orchestune.forge.GitHubForge.add_comment"),
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
            patch(
                "orchestune.dispatch_gc_completion.is_process_alive", return_value=False
            ),
            patch(
                "orchestune.forge.GitHubForge.list_prs",
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
                "orchestune.forge.GitHubForge.list_prs", return_value=[closed_pr]
            ) as mock_list_prs,
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc_completion.remove_worktree"),
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch("orchestune.forge.GitHubForge.add_label"),
            patch("orchestune.forge.GitHubForge.add_comment"),
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
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
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
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
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
            patch("orchestune.forge.GitHubForge.list_prs", return_value=ctx.prs),
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


class TestGC:
    def test_gc_reclaim_zombie(self, tmp_path):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
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
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            # 完了判定によるdirty-worktree保留とは分離し、GC回収自体を検証する。
            patch("orchestune.dispatch_gc._is_worktree_complete", return_value=False),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                return_value=[],
            ),
            patch(
                "orchestune.dispatch_gc_zombies.is_process_alive", return_value=False
            ),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
            patch("orchestune.dispatch_gc_zombies.remove_worktree") as mock_remove_wt,
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
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

    def test_gc_reclaim_zombie_only(self, tmp_path):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
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
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            # 完了判定によるdirty-worktree保留とは分離し、GC回収自体を検証する。
            patch("orchestune.dispatch_gc._is_worktree_complete", return_value=False),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                return_value=[],
            ),
            patch(
                "orchestune.dispatch_gc_zombies.is_process_alive", return_value=False
            ),
            patch(
                "orchestune.dispatch_gc_zombies.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
            patch("orchestune.dispatch_gc_zombies.remove_worktree") as mock_remove_wt,
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
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

    def test_gc_reclaim_zombie_disabled(self, tmp_path):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
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
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch(
                "orchestune.dispatch_gc_zombies.is_process_alive", return_value=False
            ),
            patch(
                "orchestune.dispatch_gc_zombies.is_process_alive", return_value=False
            ),
            patch(
                "orchestune.dispatch_gc_zombies.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
            patch("orchestune.dispatch_gc_zombies.remove_worktree") as mock_remove_wt,
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
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

    def test_gc_reclaim_timeout(self, tmp_path):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
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
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.dispatch_gc_zombies.is_process_alive", return_value=True),
            patch("orchestune.dispatch_gc_zombies.is_process_alive", return_value=True),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
            patch("orchestune.dispatch_gc_zombies.remove_worktree") as mock_remove_wt,
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
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

    def test_gc_reclaim_backup_failure_skips_deletion(self, tmp_path):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
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
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            # 完了判定によるdirty-worktree保留とは分離し、GC失敗時の保護を検証する。
            patch("orchestune.dispatch_gc._is_worktree_complete", return_value=False),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                return_value=[],
            ),
            patch(
                "orchestune.dispatch_gc_zombies.is_process_alive", return_value=False
            ),
            patch(
                "orchestune.dispatch_gc_zombies.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
            patch("orchestune.dispatch_gc_zombies.remove_worktree") as mock_remove_wt,
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
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
