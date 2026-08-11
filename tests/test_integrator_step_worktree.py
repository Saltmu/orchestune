"""`SetupWorktreeStep`: 一時ワークツリーの作成・所有権確認・後始末。

並行実行が互いのワークツリーを破壊しないこと（#51）と、`repository_root`が
相対パスでもcwdとずれないこと（#48）を含む。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestune.dispatch_worktree import file_lock
from orchestune.integrator import Integrator, IntegratorConfig
from tests.conftest import IntegratorEnv, make_done_issue

_CUSTOM_ROOT = Path("/custom/repo/root")


def _worktree_remove_calls(env: IntegratorEnv) -> list:
    return env.calls_with("worktree", "remove")


class TestWorktreeIsolation:
    """#254: repository_rootの一時差し替え・ワークツリー分離・クリーンアップの検証。"""

    def test_cleanup_uses_original_root_not_cwd(self, integrator_env: IntegratorEnv):
        # repository_rootをカレントディレクトリ以外に明示指定した場合でも、
        # 一時ワークツリーの削除はoriginal_root（呼び出し時のrepository_root）
        # を基準に行われるべきで、決め打ちのPath(".")であってはならない。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.create_pull_request.return_value = 1

        config = IntegratorConfig(apply=True, repository_root=_CUSTOM_ROOT)
        res = Integrator(config).run()

        assert res["status"] == "success"
        remove_calls = _worktree_remove_calls(integrator_env)
        assert len(remove_calls) == 1
        assert remove_calls[0].kwargs["cwd"] == _CUSTOM_ROOT.resolve()

    def test_cleanup_runs_even_when_merge_fails(self, integrator_env: IntegratorEnv):
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.fail_git(
            lambda args: "merge" in args and "--abort" not in args, stderr=b""
        )

        config = IntegratorConfig(apply=True, repository_root=_CUSTOM_ROOT)
        res = Integrator(config).run()

        assert res["status"] == "failure"
        remove_calls = _worktree_remove_calls(integrator_env)
        assert len(remove_calls) == 1
        assert remove_calls[0].kwargs["cwd"] == _CUSTOM_ROOT.resolve()

    def test_creation_failure_reports_status_and_skips_removal(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.fail_git(
            lambda args: "worktree" in args and "add" in args, stderr=b""
        )

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "failed_to_create_temp_worktree"
        assert _worktree_remove_calls(integrator_env) == []


class TestWorktreeSafety:
    """#51: 固定worktreeパスの共有によって並行実行が相互に破壊しあう不具合の回帰テスト。"""

    def test_different_parent_issues_use_distinct_worktree_and_lock_paths(self):
        # 親Issueごとに一意なworktree/lockパスが割り当てられ、異なる親Issueの
        # Integrator同士が互いのworktreeを踏みつけないことを確認する。
        integrator_a = Integrator(IntegratorConfig(apply=True, parent_issue_number=100))
        integrator_b = Integrator(IntegratorConfig(apply=True, parent_issue_number=200))

        assert integrator_a._temp_worktree_path() != integrator_b._temp_worktree_path()
        assert integrator_a._worktree_lock_path() != integrator_b._worktree_lock_path()

    @pytest.mark.uses_real_file_lock
    def test_concurrent_run_on_same_branch_is_serialized_not_destructive(
        self, fake_forge: MagicMock
    ):
        # 同じ統合ブランチに対する実行が既にロックを保持している間は、
        # 後続の実行はworktreeを奪い取ったり削除したりせず、ロック済みとして
        # 直ちに直列化（自身は何もせず終了）されるべき。
        with tempfile.TemporaryDirectory() as tmp:
            issue = make_done_issue(
                1, subtask_id="task-1", parent={"number": 42, "state": "OPEN"}
            )
            fake_forge.list_issues_by_label.side_effect = lambda label, *a, **k: [issue]
            config = IntegratorConfig(
                apply=True,
                parent_issue_number=42,
                repository_root=Path(tmp),
                forge=fake_forge,
            )
            integrator = Integrator(config)

            with file_lock(integrator._worktree_lock_path()):
                res = integrator.run()

        assert res["status"] == "integration_branch_locked"

    def test_reclaim_refuses_to_delete_unrecognized_directory(self):
        # git worktreeとして認識できない（`.git`ポインタファイルを持たない）
        # 既存ディレクトリは、所有権を確認できないため削除してはならない。
        with tempfile.TemporaryDirectory() as tmp:
            integrator = Integrator(
                IntegratorConfig(apply=True, repository_root=Path(tmp))
            )
            foreign_dir = integrator._temp_worktree_path()
            foreign_dir.mkdir(parents=True)
            important_file = foreign_dir / "important_work.txt"
            important_file.write_text("do not delete")

            with pytest.raises(RuntimeError):
                integrator._reclaim_worktree_path(foreign_dir)

            assert important_file.exists()

    def test_reclaim_removes_recognized_leftover_worktree(self):
        # `.git`ポインタファイルを持つ、以前の実行が残した正規のリンクワークツリー
        # であれば`git worktree remove`経由で安全に除去できる。
        with tempfile.TemporaryDirectory() as tmp:
            integrator = Integrator(
                IntegratorConfig(apply=True, repository_root=Path(tmp))
            )
            leftover = integrator._temp_worktree_path()
            leftover.mkdir(parents=True)
            (leftover / ".git").write_text(
                "gitdir: /somewhere/.git/worktrees/integration-temp\n"
            )

            with patch("orchestune.integrator.subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0
                )
                integrator._reclaim_worktree_path(leftover)
                assert mock_run.call_count == 1
                assert "remove" in mock_run.call_args.args[0]

            assert not leftover.exists()


class TestRelativeRepositoryRoot:
    """#48: repository_rootが`Path(".")`以外の相対パスの場合、worktreeの作成先と
    その後のcheckout/merge/CIが参照するcwdがずれて処理全体が失敗する不具合の回帰テスト。

    subprocessをモックせず、実際の一時Gitリポジトリに対してIntegratorを走らせる。
    """

    @staticmethod
    def _git(repo: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)

    @classmethod
    def _commit_file(
        cls, repo: Path, rel_path: str, content: str, msg: str, executable: bool = False
    ) -> None:
        path = repo / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if executable:
            path.chmod(0o755)
        cls._git(repo, "add", rel_path)
        cls._git(repo, "commit", "-m", msg)

    def _build_repo(self, workspace: Path) -> Path:
        origin_path = workspace / "origin.git"
        origin_path.mkdir()
        subprocess.run(
            ["git", "init", "--bare"],
            cwd=str(origin_path),
            check=True,
            capture_output=True,
        )

        # Issueの再現例に合わせ、`.`以外の相対パス名でcloneする。
        repo_path = workspace / "repo"
        subprocess.run(
            ["git", "clone", str(origin_path), str(repo_path)],
            check=True,
            capture_output=True,
        )
        self._git(repo_path, "config", "user.name", "test-bot")
        self._git(repo_path, "config", "user.email", "test-bot@example.com")
        subprocess.run(
            ["git", "checkout", "-b", "main"], cwd=str(repo_path), capture_output=True
        )

        self._commit_file(repo_path, "README.md", "dummy\n", "Initial commit")
        if sys.platform == "win32":
            self._commit_file(
                repo_path,
                "scripts/local-ci.ps1",
                "exit 0\n",
                "Add local-ci.ps1",
            )
        else:
            self._commit_file(
                repo_path,
                "scripts/local-ci.sh",
                "#!/bin/bash\nexit 0\n",
                "Add local-ci.sh",
                executable=True,
            )
        self._git(repo_path, "push", "-u", "origin", "main")

        self._git(repo_path, "checkout", "-b", "claude/issue-1-task-1")
        self._commit_file(repo_path, "feature.txt", "feature\n", "Add feature")
        self._git(repo_path, "push", "-u", "origin", "claude/issue-1-task-1")
        self._git(repo_path, "checkout", "main")
        return repo_path

    def test_relative_repository_root_succeeds_with_real_git_repo(
        self, fake_forge: MagicMock
    ):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._build_repo(workspace)

            issue = make_done_issue(1, subtask_id="task-1")
            fake_forge.list_issues_by_label.side_effect = lambda label, *a, **k: [issue]
            fake_forge.list_open_prs.return_value = []
            fake_forge.create_pull_request.return_value = 999

            original_cwd = os.getcwd()
            # `repository_root=Path("repo")`が実際にプロセスのcwd相対のパスになるよう、
            # cloneした"repo"の親ディレクトリへchdirする。
            os.chdir(str(workspace))
            try:
                config = IntegratorConfig(
                    repository_root=Path("repo"), apply=True, forge=fake_forge
                )
                res = Integrator(config).run()
            finally:
                os.chdir(original_cwd)

            assert res["status"] == "success"
            assert res["merged"] == ["task-1"]
            assert res["integration_pr_number"] == 999

            # worktreeの作成先と参照先がずれた場合に発生していた「二重化されたパス」が
            # できていないことも確認する。
            assert not (workspace / "repo" / "repo").exists()
