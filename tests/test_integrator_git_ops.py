"""integrator_git_ops.py のGit操作例外・コンフリクト境界値テスト (#339)。

`IntegrationMerger`は既存の`tests/test_integrator_step_merge.py`で正常系・
主要な失敗系（マージコンフリクト・CI失敗・fetch失敗）は検証済みだが、
以下は未検証だったため本ファイルで単体テストとして完結させる。

- dry-run（`apply=False`）時に一切のgit操作を行わずタスクをmerged扱いすること
- `create_temp_branch`のcheckout失敗時のfail-closed
- `ensure_full_history`のrev-parse/fetch失敗時のfail-open（無視して継続）
- merge試行前のHEAD SHA取得（`current_head_sha`）失敗時のタスク失敗扱い
- `run_ci_with_flaky_check`のvirtualenv自動検出における各フォールバック分岐
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from orchestune.integrator import Integrator, IntegratorConfig
from orchestune.integrator_git_ops import IntegrationMerger
from tests.conftest import IntegratorEnv, make_done_issue

_TASK_1_BRANCH = "claude/issue-1-task-1"


def _ok(args: list[str], stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout)


class TestDryRun:
    def test_apply_false_skips_all_git_operations_and_counts_as_merged(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))

        res = Integrator(IntegratorConfig(apply=False)).run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-1"]
        assert integrator_env.run.call_count == 0


class TestCreateTempBranchFailure:
    def test_checkout_failure_reports_failed_to_create_temp_branch(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.fail_git(
            lambda args: "checkout" in args and "-B" in args,
            stderr=b"fatal: unable to create temp branch",
        )

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "failed_to_create_temp_branch"
        # マージ・CI検証には一切進んでいないことを確認する
        assert integrator_env.calls_with("merge", "--no-ff") == []


class TestEnsureFullHistoryFailure:
    def test_continues_when_shallow_check_fails(self, integrator_env: IntegratorEnv):
        # rev-parse --is-shallow-repository自体が失敗しても、
        # 浅いリポジトリでないものとして扱い、統合処理は継続する。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.fail_git(
            lambda args: "rev-parse" in args and "--is-shallow-repository" in args,
            stderr=b"fatal: not a git repository",
        )

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-1"]
        assert integrator_env.calls_with("--unshallow") == []

    def test_continues_when_unshallow_fetch_fails(self, integrator_env: IntegratorEnv):
        # 浅いリポジトリと判定された後、履歴を深くするfetch自体が失敗しても、
        # 例外を無視して後続のタスクブランチfetch・マージへ進む。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))

        def handler(args):
            if args[:2] == ["git", "rev-parse"]:
                return _ok(args, "true\n")
            if "--unshallow" in args:
                raise subprocess.CalledProcessError(
                    returncode=1, cmd=args, stderr=b"fatal: unshallow failed"
                )
            return None

        integrator_env.stub_git(handler)

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-1"]
        unshallow_calls = integrator_env.calls_with("--unshallow")
        assert len(unshallow_calls) == 1


class TestPreMergeShaCaptureFailure:
    def test_fails_task_when_head_capture_fails_after_successful_fetch(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.fail_git(
            lambda args: args[-2:] == ["rev-parse", "HEAD"],
            stderr=b"fatal: ambiguous argument 'HEAD'",
        )

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "failure"
        assert res["failed"] == ["task-1"]
        integrator_env.remove_label.assert_called_with(1, "status:done")
        integrator_env.add_label.assert_called_with(1, "status:queued")
        comment_body = integrator_env.add_comment.call_args[0][1]
        assert "Failed to capture pre-merge HEAD" in comment_body
        # HEAD SHA取得失敗のため、マージ自体は試行されていない
        assert integrator_env.calls_with("merge", "--no-ff") == []


class TestRunCiVenvDetection:
    """`run_ci_with_flaky_check`のvirtualenv自動検出フォールバック分岐。"""

    def _merger(self, repo_root: Path, orig_root: Path) -> IntegrationMerger:
        return IntegrationMerger(
            repository_root=repo_root,
            original_root=orig_root,
            ci_command=["./scripts/local-ci.sh"],
        )

    def test_prefers_repository_root_venv_when_present(self, tmp_path):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        orig_root = tmp_path / "orig"
        orig_root.mkdir()
        (repo_root / ".venv" / "bin").mkdir(parents=True)

        merger = self._merger(repo_root, orig_root)

        with patch("subprocess.run", side_effect=lambda args, **kw: _ok(args)) as run:
            passed, _ = merger.run_ci_with_flaky_check()

        assert passed
        ci_calls = [
            c for c in run.call_args_list if "./scripts/local-ci.sh" in c.args[0]
        ]
        assert len(ci_calls) == 1
        env = ci_calls[0].kwargs["env"]
        assert env["VIRTUAL_ENV"] == str((repo_root / ".venv").resolve())
        assert str((repo_root / ".venv" / "bin").resolve()) in env["PATH"]

    def test_climbs_to_workspace_venv_in_monorepo_layout(self, tmp_path):
        # original_rootが`tools/orchestune`のようにネストして配置されて
        # おり、リポジトリ直下にvenvが見つからない場合、`.venv`を持つ最初の
        # 祖先ディレクトリ（ここではワークスペースルート）へフォールバックする。
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        orig_root = tmp_path / "workspace" / "tools" / "orchestune"
        orig_root.mkdir(parents=True)
        workspace_venv = tmp_path / "workspace" / ".venv"
        (workspace_venv / "bin").mkdir(parents=True)

        merger = self._merger(repo_root, orig_root)

        with patch("subprocess.run", side_effect=lambda args, **kw: _ok(args)) as run:
            passed, _ = merger.run_ci_with_flaky_check()

        assert passed
        ci_calls = [
            c for c in run.call_args_list if "./scripts/local-ci.sh" in c.args[0]
        ]
        env = ci_calls[0].kwargs["env"]
        assert env["VIRTUAL_ENV"] == str(workspace_venv.resolve())

    def test_finds_ancestor_venv_regardless_of_path_name_or_depth(self, tmp_path):
        # #376 Reproducer: 旧実装は"tools/orchestune"という固定文字列一致と
        # ちょうど3階層上という決め打ちの深さに依存しており、それ以外の
        # パス名・ネスト深さのmonorepo配置ではvenvを発見できなかった。
        # 一般化された祖先探索は、パス名やネストの深さに関わらず`.venv`を
        # 持つ最も近い祖先を見つけられなければならない。
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        monorepo_root = tmp_path / "my-monorepo"
        monorepo_venv = monorepo_root / ".venv"
        (monorepo_venv / "bin").mkdir(parents=True)
        orig_root = monorepo_root / "packages" / "sub" / "deeply" / "nested"
        orig_root.mkdir(parents=True)

        merger = self._merger(repo_root, orig_root)

        with patch("subprocess.run", side_effect=lambda args, **kw: _ok(args)) as run:
            passed, _ = merger.run_ci_with_flaky_check()

        assert passed
        ci_calls = [
            c for c in run.call_args_list if "./scripts/local-ci.sh" in c.args[0]
        ]
        env = ci_calls[0].kwargs["env"]
        assert env["VIRTUAL_ENV"] == str(monorepo_venv.resolve())

    def test_prefers_nearest_ancestor_venv_over_a_farther_one(self, tmp_path):
        # 複数の祖先にvenvが存在する場合、最も近いものを優先する。
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        far_venv = tmp_path / ".venv"
        (far_venv / "bin").mkdir(parents=True)
        near_venv = tmp_path / "workspace" / ".venv"
        (near_venv / "bin").mkdir(parents=True)
        orig_root = tmp_path / "workspace" / "nested"
        orig_root.mkdir(parents=True)

        merger = self._merger(repo_root, orig_root)

        with patch("subprocess.run", side_effect=lambda args, **kw: _ok(args)) as run:
            passed, _ = merger.run_ci_with_flaky_check()

        assert passed
        ci_calls = [
            c for c in run.call_args_list if "./scripts/local-ci.sh" in c.args[0]
        ]
        env = ci_calls[0].kwargs["env"]
        assert env["VIRTUAL_ENV"] == str(near_venv.resolve())

    def test_sets_virtual_env_without_extending_path_when_bin_missing(self, tmp_path):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        orig_root = tmp_path / "orig"
        # original_root/.venv は存在するが bin/ サブディレクトリは無い
        (orig_root / ".venv").mkdir(parents=True)

        merger = self._merger(repo_root, orig_root)

        with patch("subprocess.run", side_effect=lambda args, **kw: _ok(args)) as run:
            passed, _ = merger.run_ci_with_flaky_check()

        assert passed
        ci_calls = [
            c for c in run.call_args_list if "./scripts/local-ci.sh" in c.args[0]
        ]
        env = ci_calls[0].kwargs["env"]
        assert env["VIRTUAL_ENV"] == str((orig_root / ".venv").resolve())
        assert str(orig_root / ".venv" / "bin") not in env["PATH"]

    def test_monorepo_layout_without_any_existing_venv_leaves_virtual_env_untouched(
        self, tmp_path
    ):
        # ネストして配置されているが、祖先のどこにもvenvが存在しない場合は
        # どのvenvも見つからずVIRTUAL_ENVは（呼び出し元の環境から引き継いだ
        # 値のまま）変更されない。
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        orig_root = tmp_path / "workspace" / "tools" / "orchestune"
        orig_root.mkdir(parents=True)
        original_virtual_env = os.environ.get("VIRTUAL_ENV")

        merger = self._merger(repo_root, orig_root)

        with patch("subprocess.run", side_effect=lambda args, **kw: _ok(args)) as run:
            passed, _ = merger.run_ci_with_flaky_check()

        assert passed
        ci_calls = [
            c for c in run.call_args_list if "./scripts/local-ci.sh" in c.args[0]
        ]
        env = ci_calls[0].kwargs["env"]
        assert env.get("VIRTUAL_ENV") == original_virtual_env

    def test_falls_back_when_poetry_env_info_raises(self, tmp_path):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / "pyproject.toml").write_text("[tool.poetry]\n")
        orig_root = tmp_path / "orig"
        (orig_root / ".venv" / "bin").mkdir(parents=True)

        merger = self._merger(repo_root, orig_root)

        def mock_run_impl(args, **kwargs):
            if "poetry" in args and "info" in args and "--path" in args:
                raise subprocess.CalledProcessError(returncode=1, cmd=args)
            return _ok(args)

        with patch("subprocess.run", side_effect=mock_run_impl) as run:
            passed, _ = merger.run_ci_with_flaky_check()

        assert passed
        ci_calls = [
            c for c in run.call_args_list if "./scripts/local-ci.sh" in c.args[0]
        ]
        env = ci_calls[0].kwargs["env"]
        # poetry env info自体が例外を送出しても、original_root/.venvへの
        # フォールバックで復旧できることを確認する。
        assert env["VIRTUAL_ENV"] == str((orig_root / ".venv").resolve())

    def test_falls_back_when_poetry_env_info_path_does_not_exist(self, tmp_path):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / "pyproject.toml").write_text("[tool.poetry]\n")
        orig_root = tmp_path / "orig"
        (orig_root / ".venv" / "bin").mkdir(parents=True)
        bogus_venv = tmp_path / "no-such-venv"

        merger = self._merger(repo_root, orig_root)

        def mock_run_impl(args, **kwargs):
            if "poetry" in args and "info" in args and "--path" in args:
                return _ok(args, f"{bogus_venv}\n")
            return _ok(args)

        with patch("subprocess.run", side_effect=mock_run_impl) as run:
            passed, _ = merger.run_ci_with_flaky_check()

        assert passed
        ci_calls = [
            c for c in run.call_args_list if "./scripts/local-ci.sh" in c.args[0]
        ]
        env = ci_calls[0].kwargs["env"]
        # `poetry env info --path`が存在しないパスを返した場合は無視され、
        # original_root/.venvへフォールバックする。
        assert env["VIRTUAL_ENV"] == str((orig_root / ".venv").resolve())
