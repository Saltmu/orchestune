import subprocess
from io import StringIO
from unittest.mock import patch

import pytest

from orchestune.git_cli import (
    GitResult,
    branch_changed_files,
    ensure_parent_branch,
    list_remote_branches,
    resolve_local_or_remote_branch,
    run_git,
)

pytestmark = pytest.mark.integration


class TestRunGit:
    def test_prefixes_git_and_returns_git_result(self, tmp_path):
        with patch("orchestune.git_cli.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="out", stderr="err"
            )
            result = run_git(["status"], cwd=tmp_path)
        assert mock_run.call_args.args[0] == ["git", "status"]
        assert mock_run.call_args.kwargs["cwd"] == tmp_path
        assert mock_run.call_args.kwargs["text"] is True
        assert result == GitResult(returncode=0, stdout="out", stderr="err")

    def test_check_true_propagates_called_process_error(self):
        with patch("orchestune.git_cli.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, ["git", "status"])
            try:
                run_git(["status"], cwd=None)
            except subprocess.CalledProcessError:
                pass
            else:
                raise AssertionError("CalledProcessError should propagate")

    def test_check_false_does_not_raise_on_nonzero_exit(self):
        with patch("orchestune.git_cli.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="fatal"
            )
            result = run_git(["status"], cwd=None, check=False)
        assert result.returncode == 1


class TestListRemoteBranches:
    def test_parses_branch_lines(self):
        with patch("orchestune.git_cli.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="origin/main\norigin/feat/foo\n",
                stderr="",
            )
            branches = list_remote_branches()
        assert branches == ["origin/main", "origin/feat/foo"]

    def test_fetches_all_branches_before_listing(self):
        with patch("orchestune.git_cli.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="origin/main\n", stderr=""
            )
            list_remote_branches()
        called_commands = [call.args[0] for call in mock_run.call_args_list]
        assert [
            "git",
            "fetch",
            "--prune",
            "origin",
            "+refs/heads/*:refs/remotes/origin/*",
        ] in called_commands

    def test_fetch_failure_falls_back_to_local_refs(self):
        with patch("orchestune.git_cli.subprocess.run") as mock_run:

            def side_effect(args, **kwargs):
                if args[:2] == ["git", "fetch"]:
                    raise subprocess.CalledProcessError(
                        1, args, stderr="network unreachable"
                    )
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="origin/main\n", stderr=""
                )

            mock_run.side_effect = side_effect
            branches = list_remote_branches()
        assert branches == ["origin/main"]

    def test_discovers_branch_never_fetched_locally(self, tmp_path):
        import os

        # 1. bare originと、mainのみをtrackするローカルcloneを用意する
        remote_dir = tmp_path / "remote.git"
        remote_dir.mkdir()
        subprocess.run(["git", "init", "--bare"], cwd=str(remote_dir), check=True)
        subprocess.run(
            ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
            cwd=str(remote_dir),
            check=True,
        )

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "checkout", "-b", "main"], cwd=str(local_dir), check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(local_dir),
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(local_dir),
            check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote_dir)],
            cwd=str(local_dir),
            check=True,
        )
        (local_dir / "dummy.txt").write_text("commit 1")
        subprocess.run(["git", "add", "dummy.txt"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "commit 1"], cwd=str(local_dir), check=True
        )
        subprocess.run(
            ["git", "push", "-u", "origin", "main"], cwd=str(local_dir), check=True
        )

        # 2. 別クローンからfeature-xブランチをoriginへ直接pushする
        #    (local_dir側は一度もfetchしていない状態を再現する)
        other_dir = tmp_path / "other"
        subprocess.run(["git", "clone", str(remote_dir), str(other_dir)], check=True)
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(other_dir),
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(other_dir),
            check=True,
        )
        subprocess.run(
            ["git", "checkout", "-b", "feature-x"], cwd=str(other_dir), check=True
        )
        (other_dir / "feature.txt").write_text("feature")
        subprocess.run(["git", "add", "feature.txt"], cwd=str(other_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "feature commit"], cwd=str(other_dir), check=True
        )
        subprocess.run(
            ["git", "push", "origin", "feature-x"], cwd=str(other_dir), check=True
        )

        # 3. local_dir側でlist_remote_branches()を実行する
        original_cwd = os.getcwd()
        os.chdir(str(local_dir))
        try:
            branches = list_remote_branches()
        finally:
            os.chdir(original_cwd)

        assert "origin/feature-x" in branches


class TestBranchChangedFiles:
    def test_calls_git_diff_name_only(self):
        with patch("orchestune.git_cli.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="src/a.py\nsrc/b.py\n", stderr=""
            )
            files = branch_changed_files("origin/feat/x")
        called_args = mock_run.call_args.args[0]
        assert called_args[:2] == ["git", "diff"]
        assert "origin/main...origin/feat/x" in called_args
        assert files == ["src/a.py", "src/b.py"]

    def test_returns_empty_list_for_invalid_branch_name(self):
        stderr = StringIO()
        with (
            patch("orchestune.git_cli.subprocess.run") as mock_run,
            patch("sys.stderr", stderr),
        ):
            files = branch_changed_files("--upload-pack=evil")
            mock_run.assert_not_called()
        assert files == []
        assert "Warning: invalid ref name for branch_changed_files" in stderr.getvalue()

    def test_returns_empty_list_when_branch_has_no_merge_base(self):
        """#232: mainと共通の祖先を持たない(orphanな)ブランチとの3点diffは
        `fatal: no merge base`でexit 128になる。dispatch-cycle全体をクラッシュ
        させず、footprint差分なし（ロック対象外）として扱うべき。"""
        stderr = StringIO()
        with (
            patch("orchestune.git_cli.subprocess.run") as mock_run,
            patch("sys.stderr", stderr),
        ):
            mock_run.side_effect = subprocess.CalledProcessError(
                128,
                ["git", "diff", "--name-only", "origin/main...origin/orphan"],
                stderr="fatal: no merge base\n",
            )
            files = branch_changed_files("origin/orphan")
        assert files == []
        assert "Warning: failed to diff changed files" in stderr.getvalue()
        assert "origin/orphan" in stderr.getvalue()
        assert "fatal: no merge base" in stderr.getvalue()

    def test_returns_none_when_git_diff_cannot_run(self):
        """#245: git実行障害（OSError）は「変更なし」ではなく「差分不明」。
        呼び出し側がfail closedに判定できるようNoneを返す。"""
        stderr = StringIO()
        with (
            patch("orchestune.git_cli.subprocess.run") as mock_run,
            patch("sys.stderr", stderr),
        ):
            mock_run.side_effect = OSError("git binary missing")
            files = branch_changed_files("origin/feat/x")

        assert files is None
        assert "Warning: unable to inspect changed files" in stderr.getvalue()
        assert "git binary missing" in stderr.getvalue()

    def test_returns_none_when_ref_is_missing(self):
        """#245 Reproducer: ref欠落等のno merge base以外のgit diff失敗（rc 128）を
        空footprintに潰すと、外部ロック同期が非衝突と誤解釈して既存lockを
        解除してしまう。差分不明としてNoneを返す。"""
        stderr = StringIO()
        with (
            patch("orchestune.git_cli.subprocess.run") as mock_run,
            patch("sys.stderr", stderr),
        ):
            mock_run.side_effect = subprocess.CalledProcessError(
                128,
                ["git", "diff", "--name-only", "origin/main...origin/feat/x"],
                stderr="fatal: bad revision 'origin/feat/x'\n",
            )
            files = branch_changed_files("origin/feat/x")

        assert files is None
        assert "Warning: failed to diff changed files" in stderr.getvalue()
        assert "fatal: bad revision" in stderr.getvalue()


class TestEnsureParentBranch:
    def test_ensure_parent_branch_does_not_checkout_and_pushes_directly(self):
        # リモートに親ブランチが存在しない場合、checkoutを行わずに直接pushする
        with patch("orchestune.git_cli.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ]

            ensure_parent_branch(129)

            called_commands = [call.args[0] for call in mock_run.call_args_list]

            # git checkout は一度も呼ばれないべき
            for cmd in called_commands:
                assert "checkout" not in cmd
                assert "pull" not in cmd

            # 期待されるコマンドが実行されたことを検証
            assert mock_run.call_count == 4
            assert called_commands[0] == [
                "git",
                "ls-remote",
                "origin",
                "refs/heads/parent/issue-129",
            ]
            assert called_commands[1] == ["git", "fetch", "origin", "main"]
            assert called_commands[2] == [
                "git",
                "push",
                "origin",
                "FETCH_HEAD:refs/heads/parent/issue-129",
            ]
            assert called_commands[3] == [
                "git",
                "fetch",
                "origin",
                "+refs/heads/parent/issue-129:refs/remotes/origin/parent/issue-129",
            ]

    def test_ensure_parent_branch_does_nothing_if_remote_exists(self):
        with patch("orchestune.git_cli.subprocess.run") as mock_run:
            # ls-remote が結果（ハッシュ値など）を返す（存在する）
            # fetch -> ""
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="abcdef123456... refs/heads/parent/issue-129",
                    stderr="",
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ]

            ensure_parent_branch(129)

            called_commands = [call.args[0] for call in mock_run.call_args_list]
            assert len(called_commands) == 2
            assert called_commands[0] == [
                "git",
                "ls-remote",
                "origin",
                "refs/heads/parent/issue-129",
            ]
            assert called_commands[1] == [
                "git",
                "fetch",
                "origin",
                "+refs/heads/parent/issue-129:refs/remotes/origin/parent/issue-129",
            ]

    def test_ensure_parent_branch_handles_ls_remote_error(self):
        # ls-remote が例外を投げた場合、存在しないものとみなして作成処理へ進む
        with patch("orchestune.git_cli.subprocess.run") as mock_run:
            # 1. ls-remote -> Exception
            # 2. fetch main -> ""
            # 3. push -> ""
            # 4. fetch parent -> ""
            mock_run.side_effect = [
                Exception("network error"),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ]

            ensure_parent_branch(129)

            called_commands = [call.args[0] for call in mock_run.call_args_list]
            assert mock_run.call_count == 4
            assert called_commands[0] == [
                "git",
                "ls-remote",
                "origin",
                "refs/heads/parent/issue-129",
            ]
            assert called_commands[1] == ["git", "fetch", "origin", "main"]
            assert called_commands[2] == [
                "git",
                "push",
                "origin",
                "FETCH_HEAD:refs/heads/parent/issue-129",
            ]
            assert called_commands[3] == [
                "git",
                "fetch",
                "origin",
                "+refs/heads/parent/issue-129:refs/remotes/origin/parent/issue-129",
            ]

    def test_ensure_parent_branch_handles_push_error_without_crashing(self):
        # fetch または push が失敗しても、警告を出力してクラッシュしない
        with patch("orchestune.git_cli.subprocess.run") as mock_run:
            # 1. ls-remote -> ""
            # 2. fetch -> Exception
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
                Exception("fetch failed"),
            ]

            # 例外がスローされないことを確認
            ensure_parent_branch(129)

            called_commands = [call.args[0] for call in mock_run.call_args_list]
            assert len(called_commands) == 2

    def test_ensure_parent_branch_real_git_fetch_head(self, tmp_path):
        import os

        # 1. リモートとローカルリポジトリを準備
        remote_dir = tmp_path / "remote.git"
        remote_dir.mkdir()
        subprocess.run(["git", "init", "--bare"], cwd=str(remote_dir), check=True)
        # リモートのデフォルトブランチを main に設定
        subprocess.run(
            ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
            cwd=str(remote_dir),
            check=True,
        )

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(local_dir), check=True)
        # ローカルのデフォルトブランチを main に切り替える
        subprocess.run(
            ["git", "checkout", "-b", "main"], cwd=str(local_dir), check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(local_dir),
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(local_dir),
            check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote_dir)],
            cwd=str(local_dir),
            check=True,
        )

        # 最初のコミットを作成して push (これが古い main になる)
        dummy_file = local_dir / "dummy.txt"
        dummy_file.write_text("commit 1")
        subprocess.run(["git", "add", "dummy.txt"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "commit 1"], cwd=str(local_dir), check=True
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=str(local_dir),
            check=True,
        )

        # ローカルの refs/remotes/origin/main が commit 1 を指している状態
        old_sha = subprocess.check_output(
            ["git", "rev-parse", "refs/remotes/origin/main"],
            cwd=str(local_dir),
            text=True,
        ).strip()

        # 2. リモートの main を進める（別のクローンで新しいコミットを push）
        clone_dir = tmp_path / "clone"
        subprocess.run(["git", "clone", str(remote_dir), str(clone_dir)], check=True)
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(clone_dir),
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(clone_dir),
            check=True,
        )
        # 変更をコミットして push
        (clone_dir / "dummy.txt").write_text("commit 2")
        subprocess.run(["git", "add", "dummy.txt"], cwd=str(clone_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "commit 2"], cwd=str(clone_dir), check=True
        )
        subprocess.run(
            ["git", "push", "origin", "main"], cwd=str(clone_dir), check=True
        )

        new_sha = subprocess.check_output(
            ["git", "rev-parse", "main"], cwd=str(clone_dir), text=True
        ).strip()
        assert old_sha != new_sha

        # 3. local_dir 側で ensure_parent_branch を実行
        original_cwd = os.getcwd()
        os.chdir(str(local_dir))
        try:
            ensure_parent_branch(999)
        finally:
            os.chdir(original_cwd)

        # 4. 作成されたリモートの parent/issue-999 ブランチの SHA が、古い SHA ではなく、リモートの最新 main (new_sha) であることを確認する
        parent_sha = (
            subprocess.check_output(
                ["git", "ls-remote", str(remote_dir), "refs/heads/parent/issue-999"],
                text=True,
            )
            .split()[0]
            .strip()
        )

        assert parent_sha == new_sha

    def test_ensure_parent_branch_fetch_existing_branch(self, tmp_path):
        import os

        # 1. リモートとローカルリポジトリを準備
        remote_dir = tmp_path / "remote.git"
        remote_dir.mkdir()
        subprocess.run(["git", "init", "--bare"], cwd=str(remote_dir), check=True)
        subprocess.run(
            ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
            cwd=str(remote_dir),
            check=True,
        )

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "checkout", "-b", "main"], cwd=str(local_dir), check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(local_dir),
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(local_dir),
            check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote_dir)],
            cwd=str(local_dir),
            check=True,
        )

        # 最初のコミットを作成して push (これが古い SHA になる)
        dummy_file = local_dir / "dummy.txt"
        dummy_file.write_text("commit 1")
        subprocess.run(["git", "add", "dummy.txt"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "commit 1"], cwd=str(local_dir), check=True
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=str(local_dir),
            check=True,
        )
        sha1 = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(local_dir), text=True
        ).strip()

        # 2. 別のクローンから、リモート上に親ブランチ parent/issue-888 を作成する
        clone_dir = tmp_path / "clone"
        subprocess.run(["git", "clone", str(remote_dir), str(clone_dir)], check=True)
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(clone_dir),
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(clone_dir),
            check=True,
        )

        # 新しいコミットを作成して親ブランチとして push する
        (clone_dir / "dummy.txt").write_text("commit 2")
        subprocess.run(["git", "add", "dummy.txt"], cwd=str(clone_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "commit 2"], cwd=str(clone_dir), check=True
        )
        subprocess.run(
            ["git", "push", "origin", "HEAD:refs/heads/parent/issue-888"],
            cwd=str(clone_dir),
            check=True,
        )
        sha2 = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(clone_dir), text=True
        ).strip()

        assert sha1 != sha2

        # 3. 対象 clone (local_dir) にて、古いローカルブランチ parent/issue-888 を sha1 で作成する
        # この時点では、local_dir 側には refs/remotes/origin/parent/issue-888 はまだ存在しない (fetchしていないため)
        subprocess.run(
            ["git", "branch", "parent/issue-888", sha1],
            cwd=str(local_dir),
            check=True,
        )

        # 4. ensure_parent_branch(888) を実行する
        original_cwd = os.getcwd()
        os.chdir(str(local_dir))
        try:
            ensure_parent_branch(888)
        finally:
            os.chdir(original_cwd)

        # 5. リモート追跡ブランチ refs/remotes/origin/parent/issue-888 が自動で取得され、sha2 を指していることを確認する
        tracking_sha = subprocess.check_output(
            ["git", "rev-parse", "refs/remotes/origin/parent/issue-888"],
            cwd=str(local_dir),
            text=True,
        ).strip()
        assert tracking_sha == sha2

        # 6. prefer_remote=True で解決した際、古いローカルブランチ (sha1) ではなく、最新のリモート側 (origin/parent/issue-888) が返ることを確認する
        assert (
            resolve_local_or_remote_branch(
                local_dir, "parent/issue-888", prefer_remote=True
            )
            == "origin/parent/issue-888"
        )


class TestResolveLocalOrRemoteBranch:
    def test_resolve_local_or_remote_branch(self, tmp_path):
        # 1. ローカル・リモートリポジトリの初期化
        remote_dir = tmp_path / "remote.git"
        remote_dir.mkdir()
        subprocess.run(["git", "init", "--bare"], cwd=str(remote_dir), check=True)

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(local_dir), check=True)
        # 初期ユーザーとメールのアドレスを設定しないとコミットに失敗することがあるため設定
        subprocess.run(
            ["git", "config", "user.name", "Test User"], cwd=str(local_dir), check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(local_dir),
            check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote_dir)],
            cwd=str(local_dir),
            check=True,
        )

        # 最初のダミーコミットを作成してプッシュ
        dummy_file = local_dir / "dummy.txt"
        dummy_file.write_text("hello")
        subprocess.run(["git", "add", "dummy.txt"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial commit"], cwd=str(local_dir), check=True
        )
        subprocess.run(
            ["git", "push", "origin", "HEAD:refs/heads/main"],
            cwd=str(local_dir),
            check=True,
        )

        # 2. ローカルブランチが存在する場合の解決
        # ローカルブランチ "parent/issue-129" を作成する
        subprocess.run(
            ["git", "checkout", "-b", "parent/issue-129"],
            cwd=str(local_dir),
            check=True,
        )
        assert (
            resolve_local_or_remote_branch(local_dir, "parent/issue-129")
            == "parent/issue-129"
        )

        # 3. ローカルには存在せず、リモート追跡ブランチのみが存在する場合の解決
        # リモートに push する
        subprocess.run(
            ["git", "push", "origin", "parent/issue-129"],
            cwd=str(local_dir),
            check=True,
        )
        # mainに戻る
        subprocess.run(["git", "checkout", "main"], cwd=str(local_dir), check=True)
        # ローカルの parent/issue-129 を削除
        subprocess.run(
            ["git", "branch", "-D", "parent/issue-129"], cwd=str(local_dir), check=True
        )

        # origin/parent/issue-129 というリモート追跡ブランチが存在することを確認
        assert (
            resolve_local_or_remote_branch(local_dir, "parent/issue-129")
            == "origin/parent/issue-129"
        )

        # 4. どちらも存在しない場合
        # 存在しないブランチ名を解決しようとすると、そのままのブランチ名が返る
        assert (
            resolve_local_or_remote_branch(local_dir, "nonexistent-branch")
            == "nonexistent-branch"
        )

    def test_resolve_local_or_remote_branch_prefer_remote(self, tmp_path):
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(local_dir),
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(local_dir),
            check=True,
        )

        # ダミーコミット 1
        dummy_file = local_dir / "dummy.txt"
        dummy_file.write_text("hello 1")
        subprocess.run(["git", "add", "dummy.txt"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "commit 1"], cwd=str(local_dir), check=True
        )
        sha1 = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(local_dir), text=True
        ).strip()

        # ローカルブランチ "parent/issue-129" を作成 (sha1 を指す)
        subprocess.run(
            ["git", "branch", "parent/issue-129"], cwd=str(local_dir), check=True
        )

        # ダミーコミット 2 (SHAを進める)
        dummy_file.write_text("hello 2")
        subprocess.run(["git", "add", "dummy.txt"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "commit 2"], cwd=str(local_dir), check=True
        )
        sha2 = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(local_dir), text=True
        ).strip()

        # リモート追跡参照を作成 (sha2 を指す)
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/parent/issue-129", sha2],
            cwd=str(local_dir),
            check=True,
        )

        assert sha1 != sha2

        # prefer_remote=False の場合は、ローカルブランチが優先されて "parent/issue-129" が返る
        assert (
            resolve_local_or_remote_branch(
                local_dir, "parent/issue-129", prefer_remote=False
            )
            == "parent/issue-129"
        )

        # prefer_remote=True の場合は、リモート追跡ブランチが優先されて "origin/parent/issue-129" が返る
        assert (
            resolve_local_or_remote_branch(
                local_dir, "parent/issue-129", prefer_remote=True
            )
            == "origin/parent/issue-129"
        )
