"""integrator_git_ops.py のGit操作例外・コンフリクト境界値テスト (#339)。

`IntegrationMerger`は既存の`tests/test_integrator_step_merge.py`で正常系・
主要な失敗系（マージコンフリクト・CI失敗・fetch失敗）は検証済みだが、
以下は未検証だったため本ファイルで単体テストとして完結させる。

- dry-run（`apply=False`）時に一切のgit操作を行わずタスクをmerged扱いすること
- `create_temp_branch`のcheckout失敗時のfail-closed
- `ensure_full_history`のrev-parse/fetch失敗時のfail-open（無視して継続）
- merge試行前のHEAD SHA取得（`current_head_sha`）失敗時のタスク失敗扱い
- `run_ci_in_worktree`のvirtualenv自動検出における各フォールバック分岐
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from orchestune.integrator import Integrator, IntegratorConfig
from orchestune.integrator.git_ops import IntegrationMerger
from orchestune.models import PrRecord, Task
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
    """`run_ci_in_worktree`のvirtualenv自動検出フォールバック分岐。"""

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
            passed, _ = merger.run_ci_in_worktree()

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
            passed, _ = merger.run_ci_in_worktree()

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
            passed, _ = merger.run_ci_in_worktree()

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
            passed, _ = merger.run_ci_in_worktree()

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
            passed, _ = merger.run_ci_in_worktree()

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
            passed, _ = merger.run_ci_in_worktree()

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
            passed, _ = merger.run_ci_in_worktree()

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
            passed, _ = merger.run_ci_in_worktree()

        assert passed
        ci_calls = [
            c for c in run.call_args_list if "./scripts/local-ci.sh" in c.args[0]
        ]
        env = ci_calls[0].kwargs["env"]
        # `poetry env info --path`が存在しないパスを返した場合は無視され、
        # original_root/.venvへフォールバックする。
        assert env["VIRTUAL_ENV"] == str((orig_root / ".venv").resolve())


def _task(
    issue_number: int = 1,
    subtask_id: str = "t1",
    depends_on: tuple[str, ...] = (),
    status_labels: tuple[str, ...] = (),
) -> Task:
    return Task(
        issue_number=issue_number,
        subtask_id=subtask_id,
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=status_labels,
        created_at="2026-01-01T00:00:00Z",
        depends_on=depends_on,
    )


class TestCheckTaskBlocking:
    def _merger(self, tmp_path: Path) -> IntegrationMerger:
        return IntegrationMerger(
            repository_root=tmp_path,
            original_root=tmp_path,
            ci_command=["echo", "1"],
        )

    def test_blocks_on_escalation_label(self, tmp_path: Path):
        merger = self._merger(tmp_path)
        task = _task(
            issue_number=1,
            subtask_id="t1",
            status_labels=("status:blocked-human-review",),
        )
        reason = merger._check_task_blocking(task, unavailable_ids=set())
        assert reason is not None
        assert "status:blocked-human-reviewへエスカレーション済み" in reason

    def test_blocks_on_unavailable_dependency(self, tmp_path: Path):
        merger = self._merger(tmp_path)
        task = _task(
            issue_number=2,
            subtask_id="t2",
            depends_on=("t1",),
        )
        reason = merger._check_task_blocking(task, unavailable_ids={"t1"})
        assert reason is not None
        assert "依存タスク t1 が失敗または依存失敗のため" in reason

    def test_allows_non_blocked_task(self, tmp_path: Path):
        merger = self._merger(tmp_path)
        task = _task(
            issue_number=3,
            subtask_id="t3",
            depends_on=("t0",),
            status_labels=("status:done",),
        )
        reason = merger._check_task_blocking(task, unavailable_ids={"other"})
        assert reason is None


class TestFetchTaskBranch:
    def test_fetch_success(self, tmp_path: Path):
        merger = IntegrationMerger(tmp_path, tmp_path, ["echo", "1"])
        with patch(
            "orchestune.integrator.git_ops.fetch_remote_branch",
            return_value="origin/feature",
        ):
            success, already_merged, err = merger._fetch_task_branch("feature", "main")
        assert success is True
        assert already_merged is False
        assert err == ""

    def test_fetch_failure_already_merged(self, tmp_path: Path):
        merger = IntegrationMerger(tmp_path, tmp_path, ["echo", "1"])
        with (
            patch.object(
                merger.forge, "is_current_branch_tip_merged_into", return_value=True
            ),
            patch(
                "orchestune.integrator.git_ops.fetch_remote_branch",
                side_effect=subprocess.CalledProcessError(
                    1, ["fetch"], stderr=b"fetch error"
                ),
            ),
        ):
            success, already_merged, err = merger._fetch_task_branch("feature", "main")
        assert success is True
        assert already_merged is True
        assert err == ""

    def test_fetch_failure_not_merged(self, tmp_path: Path):
        merger = IntegrationMerger(tmp_path, tmp_path, ["echo", "1"])
        with (
            patch.object(
                merger.forge, "is_current_branch_tip_merged_into", return_value=False
            ),
            patch(
                "orchestune.integrator.git_ops.fetch_remote_branch",
                side_effect=subprocess.CalledProcessError(
                    1, ["fetch"], stderr=b"fetch error"
                ),
            ),
        ):
            success, already_merged, err = merger._fetch_task_branch("feature", "main")
        assert success is False
        assert already_merged is False
        assert "fetch error" in err


class TestResolveMergeBranch:
    """#777: マージ対象ブランチの段階的解決（①正規名→[canonical不在の確認]→
    ②厳密単一PR一致→fail-closed）。②由来の名前はdeleteに使われない（別の
    テストクラスで検証）ため、ここではfetch対象の選択のみを検証する。

    `merger.forge`は既定で実`GitHubForge`（`gh api`を実サブプロセス実行する）
    なので、`branch_exists`/`is_current_branch_tip_merged_into`/`list_prs`は
    ①のfetchを失敗させるすべてのテストで明示的にモックする。モックし忘れると
    実際の`gh` CLI呼び出しにフォールバックし、テストの成否が環境（`gh`の
    認証状態やリポジトリの実際のブランチ構成）に依存してしまう。
    """

    def test_canonical_branch_fetch_succeeds_skips_pr_lookup(self, tmp_path: Path):
        merger = IntegrationMerger(tmp_path, tmp_path, ["echo", "1"])
        task = _task(issue_number=1, subtask_id="t1")
        with (
            patch(
                "orchestune.integrator.git_ops.fetch_remote_branch",
                return_value="origin/claude/issue-1-t1",
            ) as fetch_mock,
            patch.object(merger.forge, "branch_exists") as branch_exists_mock,
            patch.object(merger.forge, "list_prs") as list_prs_mock,
        ):
            branch, fetched, already_merged, reason = merger._resolve_merge_branch(
                task, "main"
            )
        assert (branch, fetched, already_merged, reason) == (
            "claude/issue-1-t1",
            True,
            False,
            "",
        )
        fetch_mock.assert_called_once_with(tmp_path, "claude/issue-1-t1")
        branch_exists_mock.assert_not_called()
        list_prs_mock.assert_not_called()

    def test_falls_back_to_unique_pr_branch_when_canonical_confirmed_absent(
        self, tmp_path: Path
    ):
        merger = IntegrationMerger(tmp_path, tmp_path, ["echo", "1"])
        task = _task(issue_number=2, subtask_id="t2")
        pr = PrRecord(
            number=9,
            head_ref="codex/issue-2-t2",
            changed_files=(),
            is_cross_repository=False,
        )

        def fetch_side_effect(_root, branch_name):
            if branch_name == "claude/issue-2-t2":
                raise subprocess.CalledProcessError(1, ["fetch"], stderr=b"not found")
            return f"origin/{branch_name}"

        with (
            patch(
                "orchestune.integrator.git_ops.fetch_remote_branch",
                side_effect=fetch_side_effect,
            ),
            patch.object(
                merger.forge, "is_current_branch_tip_merged_into", return_value=False
            ),
            patch.object(merger.forge, "branch_exists", return_value=False),
            patch.object(merger.forge, "list_prs", return_value=[pr]) as list_prs_mock,
        ):
            branch, fetched, already_merged, reason = merger._resolve_merge_branch(
                task, "main"
            )
        assert (branch, fetched, already_merged, reason) == (
            "codex/issue-2-t2",
            True,
            False,
            "",
        )
        list_prs_mock.assert_called_once_with(state="open")

    def test_does_not_fall_back_when_canonical_branch_still_exists(
        self, tmp_path: Path
    ):
        """PR#780 Codexレビュー(Round3): fetch失敗の理由が一時的なもの
        （ネットワーク・認証・local gitエラー等）であり、正規ブランチが
        実際には存在する場合、②へフォールバックしてはならない。①の失敗を
        そのまま返す。"""
        merger = IntegrationMerger(tmp_path, tmp_path, ["echo", "1"])
        task = _task(issue_number=20, subtask_id="t20")
        pr = PrRecord(
            number=90,
            head_ref="codex/issue-20-t20",
            changed_files=(),
            is_cross_repository=False,
        )

        with (
            patch(
                "orchestune.integrator.git_ops.fetch_remote_branch",
                side_effect=subprocess.CalledProcessError(
                    1, ["fetch"], stderr=b"transient network error"
                ),
            ),
            patch.object(
                merger.forge, "is_current_branch_tip_merged_into", return_value=False
            ),
            patch.object(merger.forge, "branch_exists", return_value=True),
            patch.object(merger.forge, "list_prs", return_value=[pr]) as list_prs_mock,
        ):
            branch, fetched, already_merged, reason = merger._resolve_merge_branch(
                task, "main"
            )
        assert branch == "claude/issue-20-t20"
        assert fetched is False
        assert "Failed to fetch branch" in reason
        list_prs_mock.assert_not_called()

    def test_does_not_fall_back_when_existence_cannot_be_confirmed(
        self, tmp_path: Path
    ):
        """`branch_exists`自体がAPI障害で失敗した場合も、不在を確認できて
        いないためfail-closedにする（②を試みない）。"""
        merger = IntegrationMerger(tmp_path, tmp_path, ["echo", "1"])
        task = _task(issue_number=21, subtask_id="t21")

        with (
            patch(
                "orchestune.integrator.git_ops.fetch_remote_branch",
                side_effect=subprocess.CalledProcessError(
                    1, ["fetch"], stderr=b"not found"
                ),
            ),
            patch.object(
                merger.forge, "is_current_branch_tip_merged_into", return_value=False
            ),
            patch.object(
                merger.forge, "branch_exists", side_effect=RuntimeError("API down")
            ),
            patch.object(merger.forge, "list_prs") as list_prs_mock,
        ):
            branch, fetched, already_merged, reason = merger._resolve_merge_branch(
                task, "main"
            )
        assert branch == "claude/issue-21-t21"
        assert fetched is False
        list_prs_mock.assert_not_called()

    def test_ambiguous_pr_matches_stay_fail_closed_on_canonical_failure(
        self, tmp_path: Path
    ):
        """②で複数の異なるブランチが厳密一致する場合、tie-breakで1件へ
        絞らず①の失敗結果（fail-closed）をそのまま返す。"""
        merger = IntegrationMerger(tmp_path, tmp_path, ["echo", "1"])
        task = _task(issue_number=3, subtask_id="t3")
        prs = [
            PrRecord(
                number=1,
                head_ref="codex/issue-3-t3",
                changed_files=(),
                is_cross_repository=False,
            ),
            PrRecord(
                number=2,
                head_ref="feat/issue-3-t3",
                changed_files=(),
                is_cross_repository=False,
            ),
        ]

        with (
            patch(
                "orchestune.integrator.git_ops.fetch_remote_branch",
                side_effect=subprocess.CalledProcessError(
                    1, ["fetch"], stderr=b"not found"
                ),
            ),
            patch.object(
                merger.forge, "is_current_branch_tip_merged_into", return_value=False
            ),
            patch.object(merger.forge, "branch_exists", return_value=False),
            patch.object(merger.forge, "list_prs", return_value=prs),
        ):
            branch, fetched, already_merged, reason = merger._resolve_merge_branch(
                task, "main"
            )
        assert branch == "claude/issue-3-t3"
        assert fetched is False
        assert already_merged is False
        assert "Failed to fetch branch" in reason

    def test_no_pr_match_stays_fail_closed(self, tmp_path: Path):
        merger = IntegrationMerger(tmp_path, tmp_path, ["echo", "1"])
        task = _task(issue_number=4, subtask_id="t4")

        with (
            patch(
                "orchestune.integrator.git_ops.fetch_remote_branch",
                side_effect=subprocess.CalledProcessError(
                    1, ["fetch"], stderr=b"not found"
                ),
            ),
            patch.object(
                merger.forge, "is_current_branch_tip_merged_into", return_value=False
            ),
            patch.object(merger.forge, "branch_exists", return_value=False),
            patch.object(merger.forge, "list_prs", return_value=[]),
        ):
            branch, fetched, already_merged, reason = merger._resolve_merge_branch(
                task, "main"
            )
        assert branch == "claude/issue-4-t4"
        assert fetched is False

    def test_pr_list_failure_is_treated_as_no_candidates(self, tmp_path: Path):
        """PR一覧取得自体がAPI障害で失敗しても、②を諦めて①の失敗結果へ
        fail-closedする（例外を伝播させない）。"""
        merger = IntegrationMerger(tmp_path, tmp_path, ["echo", "1"])
        task = _task(issue_number=5, subtask_id="t5")

        with (
            patch(
                "orchestune.integrator.git_ops.fetch_remote_branch",
                side_effect=subprocess.CalledProcessError(
                    1, ["fetch"], stderr=b"not found"
                ),
            ),
            patch.object(
                merger.forge, "is_current_branch_tip_merged_into", return_value=False
            ),
            patch.object(merger.forge, "branch_exists", return_value=False),
            patch.object(
                merger.forge, "list_prs", side_effect=RuntimeError("API down")
            ),
        ):
            branch, fetched, already_merged, reason = merger._resolve_merge_branch(
                task, "main"
            )
        assert branch == "claude/issue-5-t5"
        assert fetched is False

    def test_excludes_fork_pr_from_fallback(self, tmp_path: Path):
        """PR#780 Codexレビュー: forkのhead_refをそのまま`origin`からfetchする
        と、無関係なupstreamブランチを誤ってfetch/mergeする、または正当な
        forkの貢献を誤って却下する経路になるため、②の候補から除外する。"""
        merger = IntegrationMerger(tmp_path, tmp_path, ["echo", "1"])
        task = _task(issue_number=6, subtask_id="t6")
        fork_pr = PrRecord(
            number=13,
            head_ref="codex/issue-6-t6",
            changed_files=(),
            is_cross_repository=True,
        )

        with (
            patch(
                "orchestune.integrator.git_ops.fetch_remote_branch",
                side_effect=subprocess.CalledProcessError(
                    1, ["fetch"], stderr=b"not found"
                ),
            ),
            patch.object(
                merger.forge, "is_current_branch_tip_merged_into", return_value=False
            ),
            patch.object(merger.forge, "branch_exists", return_value=False),
            patch.object(merger.forge, "list_prs", return_value=[fork_pr]),
        ):
            branch, fetched, already_merged, reason = merger._resolve_merge_branch(
                task, "main"
            )
        assert branch == "claude/issue-6-t6"
        assert fetched is False


class TestMergeTaskBranch:
    def test_merge_success(self, tmp_path: Path):
        merger = IntegrationMerger(tmp_path, tmp_path, ["echo", "1"])
        with patch("orchestune.integrator.git_ops.run_git") as mock_git:
            mock_git.side_effect = [
                _ok(["rev-parse", "HEAD"], stdout="sha123\n"),
                _ok(["merge"]),
            ]
            success, pre_merge_sha, err = merger._merge_task_branch("feature")
        assert success is True
        assert pre_merge_sha == "sha123"
        assert err == ""

    def test_merge_conflict_aborts(self, tmp_path: Path):
        merger = IntegrationMerger(tmp_path, tmp_path, ["echo", "1"])
        with patch("orchestune.integrator.git_ops.run_git") as mock_git:
            mock_git.side_effect = [
                _ok(["rev-parse", "HEAD"], stdout="sha123\n"),
                subprocess.CalledProcessError(1, ["merge"], stderr=b"CONFLICT"),
                _ok(["merge", "--abort"]),
            ]
            success, pre_merge_sha, err = merger._merge_task_branch("feature")
        assert success is False
        assert pre_merge_sha == "sha123"
        assert "Merge conflict" in err

    def test_head_capture_failure_returns_none_sha(self, tmp_path: Path):
        merger = IntegrationMerger(tmp_path, tmp_path, ["echo", "1"])
        with patch(
            "orchestune.integrator.git_ops.run_git",
            side_effect=subprocess.CalledProcessError(
                1, ["rev-parse", "HEAD"], stderr=b"HEAD error"
            ),
        ):
            success, pre_merge_sha, err = merger._merge_task_branch("feature")
        assert success is False
        assert pre_merge_sha is None
        assert "Failed to capture pre-merge HEAD" in err

    def test_merge_oserror_aborts_and_fails(self, tmp_path: Path):
        merger = IntegrationMerger(tmp_path, tmp_path, ["echo", "1"])
        with patch("orchestune.integrator.git_ops.run_git") as mock_git:
            mock_git.side_effect = [
                _ok(["rev-parse", "HEAD"], stdout="sha123\n"),
                OSError("git process failed to start"),
                _ok(["merge", "--abort"]),
            ]
            success, pre_merge_sha, err = merger._merge_task_branch("feature")
        assert success is False
        assert pre_merge_sha == "sha123"
        assert "Merge conflict" in err


class TestVerifyCiAndRollback:
    def test_ci_success(self, tmp_path: Path):
        merger = IntegrationMerger(tmp_path, tmp_path, ["echo", "1"])
        with patch.object(merger, "run_ci_in_worktree", return_value=(True, "")):
            success, reason, out = merger._verify_ci_and_rollback("sha123")
        assert success is True
        assert reason == ""
        assert out is None

    def test_ci_failure_triggers_rollback(self, tmp_path: Path):
        merger = IntegrationMerger(tmp_path, tmp_path, ["echo", "1"])
        with (
            patch.object(
                merger, "run_ci_in_worktree", return_value=(False, "ci failed")
            ),
            patch.object(merger, "rollback_to", return_value=True) as mock_rollback,
        ):
            success, reason, out = merger._verify_ci_and_rollback("sha123")
        assert success is False
        assert "CI verification failed" in reason
        assert out == "ci failed"
        mock_rollback.assert_called_once_with("sha123")


class TestExecuteCiCommand:
    def test_success(self, tmp_path: Path):
        merger = IntegrationMerger(tmp_path, tmp_path, ["echo", "1"])
        with patch("subprocess.run", return_value=_ok([])):
            passed, out = merger._execute_ci_command({})
        assert passed is True
        assert out == ""

    def test_failure_formats_output(self, tmp_path: Path):
        merger = IntegrationMerger(tmp_path, tmp_path, ["echo", "1"])
        with patch(
            "subprocess.run",
            side_effect=subprocess.CalledProcessError(
                1, ["echo"], output=b"out-text", stderr=b"err-text"
            ),
        ):
            passed, out = merger._execute_ci_command({})
        assert passed is False
        assert "--- stdout ---\nout-text" in out
        assert "--- stderr ---\nerr-text" in out
