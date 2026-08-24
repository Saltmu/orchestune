"""dispatch_gcの共有gitプリミティブ(dispatch_gc_git)のテスト。

test_dispatch_gc.py (1418行) からgitプリミティブ関連(#479)を分割。
stale entryルールは test_dispatch_gc_stale_rules.py、完了ルールは
test_dispatch_gc_completed_rule.py、`run_dispatch_cycle`経由のエンドツーエンド
統合テストは test_dispatch_gc_integration.py を参照。

Zombie・Timeout回収（dispatch_gc_zombies）は`test_dispatch_gc_zombies.py`、
完了ワークツリー処理（dispatch_gc_completion）は`test_dispatch_gc_completion.py`
へそれぞれ分割している（#345）。
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

from orchestune.dispatch.gc import (
    ZombieOrTimeoutReclaim,
    _collect_zombies_and_timeouts,
    _finalize_completed_worktree,
    backup_wip_commit,
    remote_branch_commit_sha_if_ahead,
    remove_worktree,
    worktree_has_new_commits,
    worktree_has_uncommitted_changes,
)
from orchestune.dispatch.gc.completion import (
    CompletedWorktreeDecision as ExtractedCompletedWorktreeDecision,
)
from orchestune.dispatch.gc.completion import (
    _finalize_completed_worktree as extracted_finalize_completed_worktree,
)
from orchestune.dispatch.gc.git import prune_stale_integration_temp_branches
from orchestune.dispatch.gc.zombies import (
    ZombieOrTimeoutReclaim as ExtractedZombieOrTimeoutReclaim,
)
from orchestune.dispatch.gc.zombies import (
    _collect_zombies_and_timeouts as extracted_collect_zombies_and_timeouts,
)
from orchestune.models import PrRecord


def test_extracted_gc_symbols_remain_available_from_legacy_module():
    """#330: 分割後も既存のdispatch_gc importを壊さない。"""
    from orchestune.dispatch.gc import CompletedWorktreeDecision

    assert CompletedWorktreeDecision is ExtractedCompletedWorktreeDecision
    assert _finalize_completed_worktree is extracted_finalize_completed_worktree
    assert ZombieOrTimeoutReclaim is ExtractedZombieOrTimeoutReclaim
    assert _collect_zombies_and_timeouts is extracted_collect_zombies_and_timeouts


class TestWorktreeHasUncommittedChanges:
    """#193: worktree削除前の未コミット変更確認（安全側フォールバック）。"""

    def test_clean_worktree_returns_false(self):
        with patch("orchestune.dispatch.gc.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            assert worktree_has_uncommitted_changes("worktrees/w1") is False

    def test_dirty_worktree_returns_true(self):
        with patch("orchestune.dispatch.gc.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=" M src/foo.py\n", stderr=""
            )
            assert worktree_has_uncommitted_changes("worktrees/w1") is True

    def test_git_error_defaults_to_clean(self):
        """存在しないworktreeなどgit statusが失敗する場合はクオータ解放を優先し、
        削除を妨げないようクリーン扱いとする。"""
        with patch(
            "orchestune.dispatch.gc.git.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, []),
        ):
            assert worktree_has_uncommitted_changes("worktrees/missing") is False


class TestPruneStaleIntegrationTempBranches:
    def test_deletes_only_old_temp_branches_without_open_pr(self, fake_forge):
        # #435: クラッシュ等で残ったtemp branchだけを回収し、レビュー中の
        # 統合PRのheadや作成直後の並行ランを誤って削除してはならない。
        with patch("orchestune.dispatch.gc.git.run_git") as run_git:
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
        with patch("orchestune.dispatch.gc.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            assert backup_wip_commit("worktrees/w1", "WIP: backup") is None
            # cleanなのでadd/commitは呼ばれない
            assert mock_run.call_count == 1

    def test_dirty_worktree_commits_and_returns_none(self):
        with patch("orchestune.dispatch.gc.git.subprocess.run") as mock_run:
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
            "orchestune.dispatch.gc.git.subprocess.run",
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
            "orchestune.dispatch.gc.git.subprocess.run",
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

        with patch("orchestune.dispatch.gc.git.subprocess.run", side_effect=run_mock):
            error = backup_wip_commit("worktrees/w1", "WIP: backup")
        assert error is not None
        assert "git executable not found" in error


class TestWorktreeHasNewCommits:
    """#74: base_branchに対する実コミットの有無確認（空コミット完了の誤判定防止）。"""

    def test_returns_true_when_commits_ahead(self):
        with patch("orchestune.dispatch.gc.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="2\n", stderr=""
            )
            assert worktree_has_new_commits("worktrees/w1", "origin/main") is True

    def test_returns_false_when_no_commits_ahead(self):
        with patch("orchestune.dispatch.gc.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="0\n", stderr=""
            )
            assert worktree_has_new_commits("worktrees/w1", "origin/main") is False

    def test_git_error_falls_back_to_false(self):
        """#135: 比較不能時（base_branch参照が解決できない等）は「新規コミット無し」
        と同じ安全側（False）にフォールバックし、実体のない完了確定を防ぐ。"""
        with patch(
            "orchestune.dispatch.gc.git.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, []),
        ):
            assert worktree_has_new_commits("worktrees/missing", "origin/main") is False

    def test_git_error_logs_warning_to_stderr(self, capsys):
        """#135: 比較失敗時にstderrへ警告を出力し、原因調査を容易にする。"""
        with patch(
            "orchestune.dispatch.gc.git.subprocess.run",
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
                "orchestune.dispatch.gc.git.fetch_remote_branch",
                side_effect=("origin/claude/issue-177-task-a", "origin/main"),
            ) as mock_fetch,
            patch("orchestune.dispatch.gc.git.subprocess.run") as mock_run,
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
                "orchestune.dispatch.gc.git.fetch_remote_branch",
                side_effect=("origin/claude/issue-177-task-a", "origin/main"),
            ) as mock_fetch,
            patch("orchestune.dispatch.gc.git.subprocess.run") as mock_run,
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
        with patch("orchestune.dispatch.gc.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            remove_worktree("worktrees/w1")
        args = mock_run.call_args.args[0]
        assert args == ["git", "worktree", "remove", "worktrees/w1"]
        assert "--force" not in args

    def test_swallows_error_when_already_removed(self):
        with patch(
            "orchestune.dispatch.gc.git.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, []),
        ):
            remove_worktree("worktrees/already-gone")  # 例外を送出しないこと


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
