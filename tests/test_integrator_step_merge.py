"""`MergeAndTestStep`: 対象ブランチのfetch・マージ・CI検証・ロールバック。

CI実行そのものを担う`IntegrationMerger`のPoetry環境検出も併せて検証する。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from orchestune.integrator import Integrator, IntegratorConfig
from orchestune.integrator_git_ops import IntegrationMerger
from orchestune.models import PrRecord
from orchestune.process_utils import default_ci_command
from tests.conftest import IntegratorEnv, make_done_issue

_TASK_1_BRANCH = "claude/issue-1-task-1"
_TASK_1_REFSPEC = f"+refs/heads/{_TASK_1_BRANCH}:refs/remotes/origin/{_TASK_1_BRANCH}"


def _is_ci(args: list[str]) -> bool:
    return any("local-ci." in arg for arg in args)


def _ok(args: list[str], stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout)


def _merge_index(env: IntegratorEnv) -> int:
    return env.call_index(env.calls_with("merge", "--no-ff")[0])


class TestMergeFailure:
    def test_merge_conflict_requeues_the_task(self, integrator_env: IntegratorEnv):
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.fail_git(
            lambda args: "merge" in args,
            stderr=b"CONFLICT (content): Merge conflict",
        )

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "failure"
        assert "task-1" in res["failed"]
        integrator_env.remove_label.assert_called_with(1, "status:done")
        integrator_env.add_label.assert_called_with(1, "status:queued")
        integrator_env.add_comment.assert_called_once()
        assert "Merge conflict" in integrator_env.add_comment.call_args[0][1]

    def test_conflict_aborts_before_next_task(self, integrator_env: IntegratorEnv):
        # task-1 のマージがコンフリクトで失敗しても、task-2 は巻き添えを受けず
        # クリーンな状態から正常にマージ・統合されるべき。
        integrator_env.set_done_issues(
            make_done_issue(1, subtask_id="task-1"),
            make_done_issue(2, subtask_id="task-2"),
        )
        integrator_env.fail_git(
            lambda args: "merge" in args and any(_TASK_1_BRANCH in a for a in args),
            stderr=b"CONFLICT (content): Merge conflict",
        )

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "partial_success"
        assert res["merged"] == ["task-2"]
        assert res["failed"] == ["task-1"]

        abort_calls = integrator_env.calls_with("merge", "--abort")
        assert len(abort_calls) == 1

        merge_indices = [
            integrator_env.call_index(call)
            for call in integrator_env.calls_with("merge", "--no-ff")
        ]
        abort_index = integrator_env.call_index(abort_calls[0])
        # abort は task-1 のマージ失敗の直後、task-2 のマージ試行より前に呼ばれる
        assert merge_indices[0] < abort_index < merge_indices[1]


class TestCiFailure:
    def test_rolls_back_to_pre_merge_sha_and_requeues(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        pre_merge_sha = "abc123deadbeef"

        def handler(args):
            if _is_ci(args):
                raise subprocess.CalledProcessError(
                    returncode=1,
                    cmd=args,
                    output=b"5 passed, 1 failed",
                    stderr=b"CI fail",
                )
            return _ok(args, f"{pre_merge_sha}\n") if "rev-parse" in args else None

        integrator_env.stub_git(handler)

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "failure"
        assert "task-1" in res["failed"]

        # #53: 固定の"HEAD~1"ではなく、merge試行前に保存したHEAD SHAへ戻す
        reset_calls = integrator_env.calls_with("reset")
        assert len(reset_calls) == 1
        assert pre_merge_sha in reset_calls[0].args[0]
        assert "HEAD~1" not in reset_calls[0].args[0]

        integrator_env.remove_label.assert_called_with(1, "status:done")
        integrator_env.add_label.assert_called_with(1, "status:queued")
        comment_body = integrator_env.add_comment.call_args[0][1]
        assert "CI verification failed" in comment_body
        # #295: CI出力が破棄されず、コメントに含まれることを検証する
        assert "CI fail" in comment_body
        assert "5 passed, 1 failed" in comment_body

    def test_output_is_logged_to_job_log(self, integrator_env: IntegratorEnv, capsys):
        # #295: GitHub Actionsのジョブログからも追跡できるよう、
        # コメントへの切り詰め有無に関わらず出力全文をstderrへprintする。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.fail_git(_is_ci, stderr=b"UNIQUE_JOB_LOG_MARKER")

        Integrator(IntegratorConfig(apply=True)).run()

        assert "UNIQUE_JOB_LOG_MARKER" in capsys.readouterr().err

    def test_comment_truncates_long_output(self, integrator_env: IntegratorEnv):
        # コメント本文の肥大化を避けるため、末尾のみを埋め込む。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.fail_git(_is_ci, output=("x" * 10000 + "TAIL_MARKER").encode())

        Integrator(IntegratorConfig(apply=True)).run()

        comment_body = integrator_env.add_comment.call_args[0][1]
        assert "TAIL_MARKER" in comment_body
        assert len(comment_body) < 6000

    def test_ci_runs_only_once_without_whole_script_retry(
        self, integrator_env: IntegratorEnv
    ):
        # #208: quarantine対象外のflakyテストによる失敗は、丸ごとリトライで
        # 隠さず、1回の実行結果どおりに正しくCI失敗として扱う。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.fail_git(_is_ci)

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "failure"
        assert res["failed"] == ["task-1"]
        ci_calls = [c for c in integrator_env.run.call_args_list if _is_ci(c.args[0])]
        assert len(ci_calls) == 1


class TestRollbackSha:
    """#53: CI失敗時のrollbackが固定の`HEAD~1`を前提としており、mergeが新規コミットを
    作らなかった場合（対象ブランチの先端が既にHEADへ含まれている等）に無関係な
    直前のコミットを削除してしまっていた不具合の回帰テスト。"""

    def test_uses_pre_merge_sha_when_merge_created_no_new_commit(
        self, integrator_env: IntegratorEnv
    ):
        # 対象タスクのブランチが既にHEADに含まれており、`git merge --no-ff`が
        # "Already up to date"となって新規コミットを作らなかったケースを模す。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        pre_merge_sha = "deadbeefcafef00d"

        def handler(args):
            if "rev-parse" in args:
                return _ok(args, f"{pre_merge_sha}\n")
            if "merge" in args:
                return _ok(args)
            if _is_ci(args):
                raise subprocess.CalledProcessError(
                    returncode=1, cmd=args, stderr=b"CI fail"
                )
            return None

        integrator_env.stub_git(handler)

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "failure"
        reset_calls = integrator_env.calls_with("reset")
        assert len(reset_calls) == 1
        assert reset_calls[0].args[0] == ["git", "reset", "--hard", pre_merge_sha]

    def test_rollback_failure_is_reported_explicitly(
        self, integrator_env: IntegratorEnv
    ):
        # rollback自体(git reset --hard)が失敗した場合、その旨を明示的に報告する。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))

        def handler(args):
            if "rev-parse" in args:
                return _ok(args, "abc123\n")
            if "reset" in args:
                raise subprocess.CalledProcessError(
                    returncode=1, cmd=args, stderr=b"reset failed"
                )
            if _is_ci(args):
                raise subprocess.CalledProcessError(
                    returncode=1, cmd=args, stderr=b"CI fail"
                )
            return None

        integrator_env.stub_git(handler)

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "failure"
        comment_body = integrator_env.add_comment.call_args[0][1]
        assert "rollback" in comment_body.lower() or "戻" in comment_body


class TestFetchBeforeMerge:
    def test_fetches_branch_with_explicit_refspec(self, integrator_env: IntegratorEnv):
        # actions/checkout@v6 のデフォルト（単一ブランチの浅いclone）では
        # `git fetch origin <branch>`（refspec省略）だけでは
        # `origin/<branch>` のremote-trackingブランチが作成されないため、
        # 明示的な refspec 付きでfetchしてからマージする必要がある。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "success"
        task_fetch_calls = [
            call
            for call in integrator_env.calls_with("fetch")
            if _TASK_1_REFSPEC in call.args[0]
        ]
        assert len(task_fetch_calls) == 1
        assert task_fetch_calls[0].args[0] == [
            "git",
            "fetch",
            "origin",
            _TASK_1_REFSPEC,
        ]
        assert integrator_env.call_index(task_fetch_calls[0]) < _merge_index(
            integrator_env
        )

    def test_configures_git_identity_before_merging(
        self, integrator_env: IntegratorEnv
    ):
        # CI環境（actions/checkout等）ではgit committer identityが未設定のことがあり、
        # `git merge --no-ff`でマージコミットを作成する際に
        # "Committer identity unknown" で必ず失敗するため、事前に設定する必要がある。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "success"
        name_calls = integrator_env.calls_with("config", "user.name")
        email_calls = integrator_env.calls_with("config", "user.email")
        assert len(name_calls) == 1
        assert len(email_calls) == 1
        assert integrator_env.call_index(name_calls[0]) < _merge_index(integrator_env)

    def test_unshallows_repository_before_merging_when_shallow(
        self, integrator_env: IntegratorEnv
    ):
        # actions/checkout@v6 のデフォルト（浅いclone）のままタスクブランチを
        # fetchすると、そのコミットも親を持たない浅い状態になり、mainとの共通の
        # 祖先が見つからず『refusing to merge unrelated histories』でmergeが
        # 必ず失敗するため、浅いリポジトリの場合は事前に履歴を深くする必要がある。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.stub_git(
            lambda args: (
                _ok(args, "true\n") if args[:2] == ["git", "rev-parse"] else None
            )
        )

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "success"
        unshallow_calls = integrator_env.calls_with("--unshallow")
        assert len(unshallow_calls) == 1
        assert unshallow_calls[0].args[0] == [
            "git",
            "fetch",
            "--unshallow",
            "origin",
            "main",
        ]

        branch_fetch = integrator_env.calls_with(_TASK_1_REFSPEC)[0]
        assert integrator_env.call_index(
            unshallow_calls[0]
        ) < integrator_env.call_index(branch_fetch)

    def test_skips_unshallow_when_repository_is_not_shallow(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.stub_git(
            lambda args: (
                _ok(args, "false\n") if args[:2] == ["git", "rev-parse"] else None
            )
        )

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "success"
        assert integrator_env.calls_with("--unshallow") == []


class TestFetchFailure:
    """fetch失敗はマージ失敗と同様に扱う。ただしブランチが既にbaseへ取り込み済み
    （PRがマージ済み）と確認できた場合のみスキップしてよい。"""

    def _fail_fetch(self, env: IntegratorEnv, stderr: bytes) -> None:
        env.fail_git(lambda args: "fetch" in args, stderr=stderr)

    def test_is_handled_like_merge_failure(self, integrator_env: IntegratorEnv):
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.list_open_prs.return_value = [
            PrRecord(
                number=10,
                head_ref=_TASK_1_BRANCH,
                changed_files=(),
                review_decision="APPROVED",
                is_ci_passing=True,
            )
        ]
        self._fail_fetch(
            integrator_env,
            b"fatal: couldn't find remote ref claude/issue-1-task-1",
        )

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "failure"
        assert "task-1" in res["failed"]
        assert integrator_env.calls_with("merge", "--no-ff") == []
        integrator_env.remove_label.assert_called_with(1, "status:done")
        integrator_env.add_label.assert_called_with(1, "status:queued")
        integrator_env.is_current_branch_tip_merged_into.assert_called_once_with(
            _TASK_1_BRANCH, "main"
        )

    def test_reused_branch_with_old_merged_pr_fails_closed(
        self, integrator_env: IntegratorEnv
    ):
        # The branch name was used by an older merged PR, but its recreated
        # current tip is not contained in base. The SHA-aware GitHub helper
        # therefore returns False and the historical PR must not cause a skip.
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        self._fail_fetch(integrator_env, b"fatal: couldn't find remote ref")

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "failure"
        assert res["merged"] == []
        assert res["failed"] == ["task-1"]
        integrator_env.remove_label.assert_called_once_with(1, "status:done")
        integrator_env.add_label.assert_called_once_with(1, "status:queued")
        integrator_env.add_comment.assert_called_once()
        integrator_env.is_current_branch_tip_merged_into.assert_called_once_with(
            _TASK_1_BRANCH, "main"
        )

    def test_is_skipped_when_branch_tip_is_already_merged(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.is_current_branch_tip_merged_into.return_value = True
        self._fail_fetch(integrator_env, b"fatal: couldn't find remote ref")

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-1"]
        integrator_env.is_current_branch_tip_merged_into.assert_called_once_with(
            _TASK_1_BRANCH, "main"
        )
        # 差し戻し（status:done剥がし・status:queued付与・失敗コメント）は行われず、
        # 統合済みを示す`integration:included`だけが付く。
        integrator_env.remove_label.assert_not_called()
        integrator_env.add_comment.assert_not_called()
        integrator_env.add_label.assert_called_once_with(1, "integration:included")

    def test_fails_closed_when_merged_lookup_itself_fails(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.is_current_branch_tip_merged_into.side_effect = RuntimeError(
            "GitHub API unavailable"
        )
        self._fail_fetch(integrator_env, b"temporary network failure")

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "failure"
        assert res["failed"] == ["task-1"]
        integrator_env.is_current_branch_tip_merged_into.assert_called_once_with(
            _TASK_1_BRANCH, "main"
        )


class _UnexpectedGitFailure(RuntimeError):
    """#374: subprocess.CalledProcessError/OSError以外の想定外例外を模す
    ための、他の例外型と衝突しない専用の例外。"""


class TestUnexpectedException:
    """#374: merge_and_test_tasks内で想定外の例外（subprocess.CalledProcessError
    以外）が発生しても、それ以前/以降に処理したタスクの結果や、GitHub側の副作用
    （ラベル・コメント）とのズレを生まないことを検証する。"""

    def test_is_contained_and_recorded_as_failure_without_blocking_other_tasks(
        self, integrator_env: IntegratorEnv
    ):
        issue_a = make_done_issue(1, subtask_id="task-1")
        issue_b = make_done_issue(2, subtask_id="task-2")
        integrator_env.set_done_issues(issue_a, issue_b, done=[issue_a, issue_b])

        def handler(args):
            if (
                "merge" in args
                and "--no-ff" in args
                and any(_TASK_1_BRANCH in a for a in args)
            ):
                raise _UnexpectedGitFailure("disk I/O error")
            return None

        integrator_env.stub_git(handler)

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "partial_success"
        assert res["merged"] == ["task-2"]
        assert res["failed"] == ["task-1"]

        # task-1の失敗によるGitHub側の副作用（差し戻し）が、レポート内容と
        # 一貫して実行されている。
        integrator_env.remove_label.assert_called_with(1, "status:done")
        integrator_env.add_label.assert_called_with(1, "status:queued")
        comment_body = integrator_env.add_comment.call_args[0][1]
        assert "Unexpected error during merge/test" in comment_body

        # task-1のクラッシュ相当の失敗が、後続task-2の正常な統合を巻き添えにしない。
        merge_calls = integrator_env.calls_with("merge", "--no-ff")
        assert any(_TASK_1_BRANCH in a for c in merge_calls for a in c.args[0])
        assert any("claude/issue-2-task-2" in a for a in merge_calls[-1].args[0])

    def test_rolls_back_to_pre_merge_sha_when_ci_step_raises_unexpectedly(
        self, integrator_env: IntegratorEnv
    ):
        # `git merge`自体は成功しCI検証の段階で想定外の例外が発生した場合でも、
        # merge前のHEADへロールバックし、未検証のコミットを後続タスクへ
        # 持ち越さないことを確認する。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        pre_merge_sha = "cafefeed1234"

        def handler(args):
            if "rev-parse" in args:
                return _ok(args, f"{pre_merge_sha}\n")
            if _is_ci(args):
                raise _UnexpectedGitFailure("out of memory")
            return None

        integrator_env.stub_git(handler)

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "failure"
        assert res["failed"] == ["task-1"]
        reset_calls = integrator_env.calls_with("reset")
        assert len(reset_calls) == 1
        assert pre_merge_sha in reset_calls[0].args[0]

    def test_fails_closed_when_recording_the_failure_itself_also_raises(
        self, integrator_env: IntegratorEnv
    ):
        # handle_failure（GitHub側の差し戻し）自体が例外を送出しても、
        # その後続タスクの処理・全体のレポート作成は継続されなければならない。
        issue_a = make_done_issue(1, subtask_id="task-1")
        issue_b = make_done_issue(2, subtask_id="task-2")
        integrator_env.set_done_issues(issue_a, issue_b, done=[issue_a, issue_b])
        integrator_env.add_label.side_effect = _UnexpectedGitFailure("GitHub API down")

        def handler(args):
            if (
                "merge" in args
                and "--no-ff" in args
                and any(_TASK_1_BRANCH in a for a in args)
            ):
                raise _UnexpectedGitFailure("disk I/O error")
            return None

        integrator_env.stub_git(handler)

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "partial_success"
        assert res["merged"] == ["task-2"]
        assert res["failed"] == ["task-1"]
        assert "task-1" in res["failed_reasons"]
        assert "Unexpected error during merge/test" in res["failed_reasons"]["task-1"]


class TestCiEnvironment:
    def test_ci_command_receives_the_project_virtualenv(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))

        # 実際の一時ディレクトリに .venv/bin を用意し、Path.exists()を
        # グローバルにモックせず本物のファイルシステム状態で検証する
        # （globalモックはworktreeパスの所有権チェックなど無関係な箇所にも
        # 影響してしまうため）。
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / ".venv" / "bin").mkdir(parents=True)
            integrator = Integrator(IntegratorConfig(apply=True, repository_root=root))
            integrator.run()

        ci_calls = integrator_env.calls_with(*default_ci_command())
        assert len(ci_calls) == 1
        env = ci_calls[0].kwargs["env"]

        expected_venv = integrator.original_root / ".venv"
        assert env["VIRTUAL_ENV"] == str(expected_venv.resolve())
        assert env["PATH"].startswith(str(expected_venv / "bin"))

    def test_run_ci_installs_poetry_deps_and_detects_venv(self, tmp_path):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        orig_root = tmp_path / "orig"
        orig_root.mkdir()
        (repo_root / "pyproject.toml").write_text("[tool.poetry]\n")

        merger = IntegrationMerger(
            repository_root=repo_root,
            original_root=orig_root,
            ci_command=["./scripts/local-ci.sh"],
        )

        dummy_venv_path = tmp_path / "dummy_venv"
        (dummy_venv_path / "bin").mkdir(parents=True)

        def mock_run_impl(args, **kwargs):
            if "poetry" in args and "info" in args and "--path" in args:
                return _ok(args, f"{dummy_venv_path}\n")
            return _ok(args)

        with patch("subprocess.run", side_effect=mock_run_impl) as mock_run:
            passed, _ = merger.run_ci_with_flaky_check()

        assert passed
        calls = mock_run.call_args_list
        assert len(calls) >= 3

        install_calls = [
            c for c in calls if "poetry" in c.args[0] and "install" in c.args[0]
        ]
        assert len(install_calls) == 1
        assert install_calls[0].kwargs.get("cwd") == str(repo_root)

        ci_calls = [c for c in calls if "./scripts/local-ci.sh" in c.args[0]]
        assert len(ci_calls) == 1
        ci_env = ci_calls[0].kwargs.get("env", {})
        assert ci_env.get("VIRTUAL_ENV") == str(dummy_venv_path.resolve())
        assert str(dummy_venv_path / "bin") in ci_env.get("PATH", "")

    def test_run_ci_reports_missing_poetry(self, tmp_path):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        orig_root = tmp_path / "orig"
        orig_root.mkdir()
        (repo_root / "pyproject.toml").write_text("[tool.poetry]\n")

        merger = IntegrationMerger(
            repository_root=repo_root,
            original_root=orig_root,
            ci_command=["./scripts/local-ci.sh"],
        )

        with patch("subprocess.run", side_effect=FileNotFoundError("poetry")):
            ok, output = merger.run_ci_with_flaky_check()

        assert ok is False
        assert "poetry" in output
