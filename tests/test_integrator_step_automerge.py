"""子統合の確定: `LabelIncludedStep` / `AutoMergeChildIntegrationStep` /
`RetryChildIssueCloseStep`。

#170: `parent_issue_number`指定時のみ、統合PRを自動マージし対象の子Issueを
自動クローズする。最終マージ（親ブランチ→main）は引き続き人間が行うため、
`parent_issue_number`未指定（base_branch=main）時は一切自動マージ・クローズを
行わない。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import ANY, patch

import pytest

from orchestune.integrator import (
    AutoMergeChildIntegrationStep,
    IntegrationContext,
    IntegrationStatus,
    Integrator,
    IntegratorConfig,
)
from orchestune.integrator_steps import _is_parent_branch_cas_rejection
from orchestune.models import Task
from tests.conftest import IntegratorEnv, make_done_issue

_CLOSE_CHILD_ISSUES = (
    "orchestune.integrator.AutoMergeChildIntegrationStep._close_merged_child_issues"
)


class _SimulatedProcessCrash(Exception):
    """#209: プロセス強制終了を模すための、他の例外型と衝突しない専用の例外。"""


def _child_config() -> IntegratorConfig:
    return IntegratorConfig(
        apply=True, parent_issue_number=100, integration_run_id="test-run"
    )


class TestIsParentBranchCasRejection:
    """#437レビュー対応: non-fast-forward拒否と、認証・ネットワーク等の
    他のpush失敗要因とを区別する判定ロジック。"""

    def test_true_for_non_fast_forward_stderr(self):
        error = subprocess.CalledProcessError(
            1, ["git", "push"], stderr="! [rejected] (non-fast-forward)"
        )
        assert _is_parent_branch_cas_rejection(error) is True

    def test_true_for_fetch_first_stderr(self):
        error = subprocess.CalledProcessError(
            1, ["git", "push"], stderr="! [rejected] (fetch first)"
        )
        assert _is_parent_branch_cas_rejection(error) is True

    def test_false_for_authentication_failure(self):
        error = subprocess.CalledProcessError(
            1, ["git", "push"], stderr="fatal: Authentication failed"
        )
        assert _is_parent_branch_cas_rejection(error) is False

    def test_false_for_os_error(self):
        assert _is_parent_branch_cas_rejection(OSError("network unreachable")) is False

    def test_false_for_missing_stderr(self):
        error = subprocess.CalledProcessError(1, ["git", "push"], stderr=None)
        assert _is_parent_branch_cas_rejection(error) is False

    def test_true_for_bytes_stderr(self):
        error = subprocess.CalledProcessError(
            1, ["git", "push"], stderr=b"! [rejected] (non-fast-forward)"
        )
        assert _is_parent_branch_cas_rejection(error) is True


class TestLabelIncludedStep:
    def test_marks_merged_issues_as_integration_included(
        self, integrator_env: IntegratorEnv
    ):
        # #139: 統合ブランチへのpush・統合PR確保が成功した時点で、対象Issueに
        # `integration:included`を記帳する。`status:done`自体は変更しない
        # （依存解決・外部ロック等の他サブシステムが引き続き参照するため）。
        issue_a = make_done_issue(1, subtask_id="task-1")
        issue_b = make_done_issue(2, subtask_id="task-2", depends_on=("task-1",))
        integrator_env.set_done_issues(issue_a, issue_b, done=[issue_b, issue_a])

        res = Integrator(_child_config()).run()

        assert res["status"] == "success"
        assert res["newly_included"] == ["task-1", "task-2"]
        integrator_env.add_label.assert_any_call(1, "integration:included")
        integrator_env.add_label.assert_any_call(2, "integration:included")
        assert integrator_env.add_label.call_count == 2

    def test_label_is_added_before_close_is_attempted(
        self, integrator_env: IntegratorEnv
    ):
        # #209: `integration:included`の付与がクローズ試行より前に完了して
        # いることを、実際の呼び出し順序で直接検証する。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))

        call_order: list[str] = []
        integrator_env.add_label.side_effect = lambda *a, **k: call_order.append(
            "add_label"
        )
        integrator_env.close_issue.side_effect = lambda *a, **k: call_order.append(
            "close_issue"
        )

        res = Integrator(_child_config()).run()

        assert res["status"] == "success"
        assert call_order == ["add_label", "close_issue"]

    def test_label_granted_even_if_close_step_crashes(
        self, integrator_env: IntegratorEnv
    ):
        # #209: マージ成功直後にクローズ処理全体が未捕捉例外で中断しても
        # （プロセス強制終了を模す）、integration:includedのラベル付与だけは
        # 既に完了していなければならない。そうでなければ、子ブランチは既に
        # base_branchへ取り込み済みなのに信頼できる回復シグナルが無いまま
        # 次サイクルの統合PR作成が差分無しで失敗し、当該子Issueが永久に
        # クローズされないライブロックに陥る。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))

        # `except RuntimeError`（統合ブランチのロック競合用）と衝突しない
        # 独自の例外型で、それ以外のあらゆる箇所で捕捉されないことを保証する。
        with patch(_CLOSE_CHILD_ISSUES, side_effect=_SimulatedProcessCrash("crash")):
            with pytest.raises(_SimulatedProcessCrash, match="crash"):
                Integrator(_child_config()).run()

        integrator_env.add_label.assert_called_once_with(1, "integration:included")


class TestAutoMergeChildIntegration:
    def test_no_auto_merge_when_parent_issue_number_is_none(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "success"
        create_pr_body = integrator_env.create_pull_request.call_args.kwargs["body"]
        assert "人間が行ってください" in create_pr_body
        integrator_env.merge_pull_request.assert_not_called()
        integrator_env.close_issue.assert_not_called()
        assert res.get("auto_merged") is None
        assert res.get("closed_issues") is None

    def test_no_auto_merge_when_pr_creation_failed(self, integrator_env: IntegratorEnv):
        # is_current_branch_tip_merged_into はデフォルトでFalse（integrator_env
        # フィクスチャ側の既定値）なので、実際にはまだ統合済みでないケース。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.create_pull_request.side_effect = RuntimeError("no commits")

        res = Integrator(_child_config()).run()

        assert res["status"] == "success"
        assert res["integration_pr_number"] is None
        integrator_env.merge_pull_request.assert_not_called()
        integrator_env.close_issue.assert_not_called()
        integrator_env.add_label.assert_not_called()
        assert res.get("auto_merged") is None

    def test_recovers_label_and_close_when_pr_creation_failed_but_already_merged(
        self, integrator_env: IntegratorEnv
    ):
        # #373: 前サイクルで`merge_pull_request`自体は成功していたが、その直後
        # （ラベル付与前）にプロセスがクラッシュしたケースを模す。今サイクルの
        # 一時ブランチは既にbase_branchへ統合済みのため差分無しでPR作成に失敗
        # する（`integration_pr_number is None`）が、対象ブランチの先端が
        # 実際にはbase_branchへ既に含まれていることを`is_current_branch_tip_
        # merged_into`で確認できる場合は、`merge_pull_request`を再度呼ばずに
        # ラベル付与・クローズだけを再試行できなければならない。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.create_pull_request.side_effect = RuntimeError("no commits")
        integrator_env.is_current_branch_tip_merged_into.return_value = True

        res = Integrator(_child_config()).run()

        assert res["status"] == "success"
        assert res["integration_pr_number"] is None
        integrator_env.merge_pull_request.assert_not_called()
        integrator_env.is_current_branch_tip_merged_into.assert_called_once_with(
            "claude/issue-1-task-1", "parent/issue-100"
        )
        integrator_env.add_label.assert_called_once_with(1, "integration:included")
        integrator_env.close_issue.assert_called_once_with(1, "completed", comment=ANY)
        assert res["auto_merged"] is False
        assert res["closed_issues"] == [1]
        assert res["newly_included"] == ["task-1"]

    def test_verification_exception_fails_closed_when_pr_creation_failed(
        self, integrator_env: IntegratorEnv
    ):
        # #373: 統合済みかどうかの確認自体がAPI障害等で失敗した場合は、
        # 誤ってラベル付与・クローズしてしまわないようfail closedで何もしない。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.create_pull_request.side_effect = RuntimeError("no commits")
        integrator_env.is_current_branch_tip_merged_into.side_effect = RuntimeError(
            "API unavailable"
        )

        res = Integrator(_child_config()).run()

        assert res["status"] == "success"
        integrator_env.merge_pull_request.assert_not_called()
        integrator_env.add_label.assert_not_called()
        integrator_env.close_issue.assert_not_called()
        assert res.get("auto_merged") is None

    def test_parent_branch_push_rejection_skips_closing(
        self, integrator_env: IntegratorEnv
    ):
        # #435: 親ブランチが別ランにより先へ進んでいれば、通常pushは
        # non-fast-forwardで拒否される。親を上書きせず、完了ラベルやIssueの
        # クローズも行わない。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.fail_git(
            lambda args: (
                args[:2] == ["git", "push"]
                and "HEAD:refs/heads/parent/issue-100" in args
            ),
            stderr="non-fast-forward",
        )

        res = Integrator(_child_config()).run()

        assert res["status"] == IntegrationStatus.PARENT_BRANCH_ADVANCED
        assert res["integration_pr_number"] == 999
        integrator_env.merge_pull_request.assert_not_called()
        integrator_env.close_issue.assert_not_called()
        # #437: 1回目の陳腐化では、まだ子Issueへの完了ラベルは付与しない。
        # 親Issueへは（2回連続の判定に使う）マーカーラベルのみ付与する。
        integrator_env.add_label.assert_called_once_with(
            100, "integration:parent-branch-stale"
        )
        integrator_env.add_comment.assert_any_call(1, ANY)

    def test_staleness_is_also_recorded_on_the_parent_issue(
        self, integrator_env: IntegratorEnv
    ):
        # #437: 子Issueへのコメントだけでなく、親Issue側にも陳腐化イベントを
        # 記録することで、親Issueだけを見ても衝突が起きた事実を説明できる。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.fail_git(
            lambda args: (
                args[:2] == ["git", "push"]
                and "HEAD:refs/heads/parent/issue-100" in args
            ),
            stderr="non-fast-forward",
        )

        Integrator(_child_config()).run()

        integrator_env.add_comment.assert_any_call(100, ANY)

    def test_parent_update_uses_non_force_git_push(self, integrator_env: IntegratorEnv):
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))

        res = Integrator(_child_config()).run()

        assert res["status"] == "success"
        parent_pushes = [
            call
            for call in integrator_env.calls_with("push")
            if "HEAD:refs/heads/parent/issue-100" in call.args[0]
        ]
        assert len(parent_pushes) == 1
        assert "--force" not in parent_pushes[0].args[0]
        integrator_env.merge_pull_request.assert_not_called()

    def test_close_failure_for_one_task_does_not_block_others(
        self, integrator_env: IntegratorEnv
    ):
        issue_a = make_done_issue(1, subtask_id="task-1")
        issue_b = make_done_issue(2, subtask_id="task-2", depends_on=("task-1",))
        integrator_env.set_done_issues(issue_a, issue_b, done=[issue_b, issue_a])
        integrator_env.close_issue.side_effect = [
            subprocess.CalledProcessError(1, ["gh", "issue", "close"]),
            None,
        ]

        res = Integrator(_child_config()).run()

        assert res["status"] == "success"
        assert integrator_env.close_issue.call_count == 2
        assert res["closed_issues"] == [2]

    def test_deletes_remote_branches_after_successful_merge(
        self, integrator_env: IntegratorEnv
    ):
        issue = make_done_issue(1, subtask_id="task-1")
        integrator_env.set_done_issues(issue)

        res = Integrator(_child_config()).run()

        assert res["status"] == "success"
        # 子ブランチと一時ブランチの削除が呼び出されていることを検証
        integrator_env.delete_branch.assert_any_call("claude/issue-1-task-1")
        integrator_env.delete_branch.assert_any_call(
            "integration/temp-parent-issue-100-test-run"
        )
        assert integrator_env.delete_branch.call_count == 2

    def test_continues_even_if_branch_deletion_raises_exception(
        self, integrator_env: IntegratorEnv
    ):
        issue = make_done_issue(1, subtask_id="task-1")
        integrator_env.set_done_issues(issue)
        # 削除処理で例外が発生するように設定
        integrator_env.delete_branch.side_effect = RuntimeError("API error")

        res = Integrator(_child_config()).run()

        # 削除失敗時も全体のステータスは成功すること
        assert res["status"] == "success"
        assert res["closed_issues"] == [1]
        integrator_env.delete_branch.assert_any_call("claude/issue-1-task-1")


class TestParentBranchStaleRetryEscalation:
    """#437: 親branch更新の連続陳腐化を2サイクル連続で検知した場合、対象の
    子Issueをstatus:blocked-human-reviewへエスカレーションする。#437レビュー
    対応: 連続回数はローカルのstate fileではなく親Issueのマーカーラベル
    （`integration:parent-branch-stale`）で判定する。GitHub Actionsの
    スケジュール実行のようにサイクルごとに別のランナーが使われる構成でも
    判定が失われないようにするため。"""

    def _fail_parent_push(self, integrator_env: IntegratorEnv) -> None:
        integrator_env.fail_git(
            lambda args: (
                args[:2] == ["git", "push"]
                and "HEAD:refs/heads/parent/issue-100" in args
            ),
            stderr="non-fast-forward",
        )

    def test_first_occurrence_marks_parent_issue_without_escalating(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        self._fail_parent_push(integrator_env)

        Integrator(_child_config()).run()

        integrator_env.add_label.assert_called_once_with(
            100, "integration:parent-branch-stale"
        )

    def test_escalates_on_second_consecutive_occurrence(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        self._fail_parent_push(integrator_env)
        # 1回目の呼び出しで親Issueにマーカーラベルが付いた状態を模す。
        integrator_env.get_issue_labels.return_value = (
            "integration:parent-branch-stale",
        )

        Integrator(_child_config()).run()

        integrator_env.add_label.assert_any_call(1, "status:blocked-human-review")
        assert any(
            call.args[0] == 1 and "blocked-human-review" in call.args[1]
            for call in integrator_env.add_comment.call_args_list
        )
        # 人間の確認が必要になったため、マーカーラベル自体は除去する。
        integrator_env.remove_label.assert_any_call(
            100, "integration:parent-branch-stale"
        )

    def test_successful_push_clears_the_marker_label(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))

        Integrator(_child_config()).run()

        integrator_env.remove_label.assert_any_call(
            100, "integration:parent-branch-stale"
        )

    def test_non_cas_push_failure_is_not_counted_as_staleness(
        self, integrator_env: IntegratorEnv
    ):
        # #437レビュー対応: 認証エラー・ネットワーク障害・ブランチ保護拒否等の
        # non-fast-forward以外のpush失敗は、一時的な障害である可能性があり、
        # 陳腐化としてカウント・エスカレーションしてはならない（さもないと
        # 障害復旧後も手動でのラベル除去なしに自己修復できなくなる）。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.fail_git(
            lambda args: (
                args[:2] == ["git", "push"]
                and "HEAD:refs/heads/parent/issue-100" in args
            ),
            stderr="fatal: Authentication failed for 'https://github.com/...'",
        )

        res = Integrator(_child_config()).run()

        assert res["status"] == IntegrationStatus.PARENT_BRANCH_ADVANCED
        integrator_env.add_label.assert_not_called()
        integrator_env.get_issue_labels.assert_not_called()
        assert not any(
            call.args[0] == 100 and "陳腐化" in call.args[1]
            for call in integrator_env.add_comment.call_args_list
        )
        # 既存の子Issueへの失敗通知は引き続き行われる。
        integrator_env.add_comment.assert_any_call(1, ANY)


@pytest.mark.integration
def test_parent_push_rejects_stale_base_without_updating_remote(fake_forge):
    """#435: 実gitでも競合ランが進めた親branchを上書きしない。"""

    def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        origin = workspace / "origin.git"
        subprocess.run(["git", "init", "--bare", str(origin)], check=True)
        worker = workspace / "worker"
        subprocess.run(["git", "clone", str(origin), str(worker)], check=True)
        git(worker, "config", "user.name", "test-bot")
        git(worker, "config", "user.email", "test-bot@example.com")
        git(worker, "checkout", "-b", "main")
        (worker / "base.txt").write_text("base\n", encoding="utf-8")
        git(worker, "add", "base.txt")
        git(worker, "commit", "-m", "base")
        git(worker, "push", "-u", "origin", "main")
        git(worker, "checkout", "-b", "parent/issue-100")
        git(worker, "push", "-u", "origin", "parent/issue-100")

        temp_branch = "integration/temp-parent-issue-100-test-run"
        git(worker, "checkout", "-b", temp_branch, "origin/parent/issue-100")
        (worker / "integration.txt").write_text("integrated\n", encoding="utf-8")
        git(worker, "add", "integration.txt")
        git(worker, "commit", "-m", "integration result")

        racer = workspace / "racer"
        subprocess.run(["git", "clone", str(origin), str(racer)], check=True)
        git(racer, "config", "user.name", "racer")
        git(racer, "config", "user.email", "racer@example.com")
        git(racer, "checkout", "parent/issue-100")
        (racer / "racer.txt").write_text("advanced\n", encoding="utf-8")
        git(racer, "add", "racer.txt")
        git(racer, "commit", "-m", "advance parent")
        git(racer, "push", "origin", "parent/issue-100")
        advanced_sha = git(racer, "rev-parse", "HEAD").stdout.strip()

        config = IntegratorConfig(
            repository_root=worker,
            parent_issue_number=100,
            integration_run_id="test-run",
            apply=True,
            forge=fake_forge,
        )
        ctx = IntegrationContext(
            config=config,
            repository_root=worker,
            original_root=worker,
            base_branch="origin/parent/issue-100",
            temp_branch=temp_branch,
            merged_tasks=["task-1"],
            active_done_tasks=[
                Task(
                    issue_number=1,
                    subtask_id="task-1",
                    footprint=(),
                    symbols=(),
                    risk=False,
                    priority="medium",
                    progress_partial=False,
                    status_labels=("status:done",),
                    created_at="2026-01-01T00:00:00+00:00",
                )
            ],
            integration_pr_number=123,
        )

        result = AutoMergeChildIntegrationStep().execute(ctx)

        assert result["status"] == IntegrationStatus.PARENT_BRANCH_ADVANCED
        remote_parent = subprocess.run(
            ["git", "--git-dir", str(origin), "rev-parse", "parent/issue-100"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert remote_parent == advanced_sha
        # #437: 1回目の陳腐化では、まだ子Issueへの完了ラベルは付与しない。
        # 親Issueへは（2回連続の判定に使う）マーカーラベルのみ付与する。
        fake_forge.add_label.assert_called_once_with(
            100, "integration:parent-branch-stale"
        )
        fake_forge.close_issue.assert_not_called()


class TestRetryChildIssueCloseStep:
    """#170レビュー対応: マージ成功が確定した信頼できるシグナルである
    `integration:included`ラベルを再利用し、マージ済み・クローズ未了の
    子Issueを独立に再試行してクローズする。
    """

    def _included(self, number: int, subtask_id: str):
        return make_done_issue(
            number,
            subtask_id=subtask_id,
            labels=("status:done", "integration:included"),
        )

    def test_retries_closing_issue_already_marked_included(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(self._included(1, "task-1"))

        res = Integrator(_child_config()).run()

        assert res["status"] == "no_done_tasks"
        integrator_env.close_issue.assert_called_once_with(1, "completed", comment=ANY)
        assert res["retried_closed_issues"] == [1]

    def test_included_task_is_recovered_via_retry_not_remerged(
        self, integrator_env: IntegratorEnv
    ):
        # #170レビュー対応: integration:includedは、マージ成功が確定した場合
        # にのみ付与されるようになったため（`AutoMergeChildIntegrationStep`が
        # `LabelIncludedStep`より前段で実行される）、既にこのラベルを持つ
        # タスクは「base_branchへ既に安全に取り込まれ済み」であることの
        # 信頼できるシグナルになる。`RetryChildIssueCloseStep`がこれを検知して
        # クローズを独立に再試行し、統合ブランチへの再マージ対象からは除外する
        # （既にbase_branchに含まれているため、再マージする必要がない）。
        issue_a = self._included(1, "task-1")
        issue_b = make_done_issue(2, subtask_id="task-2", depends_on=("task-1",))
        integrator_env.set_done_issues(issue_a, issue_b, done=[issue_b, issue_a])

        res = Integrator(_child_config()).run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-2"]
        assert res["retried_closed_issues"] == [1]
        assert res["newly_included"] == ["task-2"]
        integrator_env.close_issue.assert_any_call(1, "completed", comment=ANY)
        integrator_env.close_issue.assert_any_call(2, "completed", comment=ANY)
        assert integrator_env.close_issue.call_count == 2
        integrator_env.add_label.assert_called_once_with(2, "integration:included")

        # task-1は既にbase_branchに含まれている前提のため再マージされない。
        # `git merge`のみを数える（#170: parent_issue_number指定時は、この後
        # `gh pr merge`による自動マージ呼び出しも同じsubprocess.runモックを
        # 経由するため、それらと混同しないようgitコマンドに限定する）。
        merge_calls = [
            call
            for call in integrator_env.calls_with("merge")
            if call.args[0][0] == "git"
        ]
        assert len(merge_calls) == 1
        assert any("claude/issue-2-task-2" in arg for arg in merge_calls[0].args[0])

    def test_does_not_block_normal_processing_of_other_tasks(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(
            self._included(1, "task-1"), make_done_issue(2, subtask_id="task-2")
        )

        res = Integrator(_child_config()).run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-2"]
        assert res["retried_closed_issues"] == [1]
        integrator_env.close_issue.assert_any_call(1, "completed", comment=ANY)
        integrator_env.close_issue.assert_any_call(2, "completed", comment=ANY)
        assert integrator_env.close_issue.call_count == 2
        integrator_env.merge_pull_request.assert_not_called()

    def test_retry_failure_falls_back_to_normal_reprocessing(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(self._included(1, "task-1"))
        integrator_env.close_issue.side_effect = [
            subprocess.CalledProcessError(1, ["gh", "issue", "close"]),
            None,
        ]

        res = Integrator(_child_config()).run()

        assert res["status"] == "success"
        assert res["retried_closed_issues"] == []
        assert res["merged"] == ["task-1"]
        assert res["closed_issues"] == [1]
        assert integrator_env.close_issue.call_count == 2

    def test_no_retry_when_parent_issue_number_is_none(
        self, integrator_env: IntegratorEnv
    ):
        integrator_env.set_done_issues(self._included(1, "task-1"))

        res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-1"]
        integrator_env.close_issue.assert_not_called()
        assert not res.get("retried_closed_issues")

    def test_next_cycle_retries_close_after_label_persisted_from_crash(
        self, integrator_env: IntegratorEnv
    ):
        # #209: 1サイクル目でマージ成功後にクローズ処理全体がプロセス強制終了
        # 相当の未捕捉例外で中断しても、ラベルは既に付与済み（実際にGitHub側へ
        # 反映済み）である。2サイクル目はこのラベルを信頼できるシグナルとして
        # `RetryChildIssueCloseStep`がクローズを独立に再試行できることを確認する。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        config = _child_config()

        with patch(_CLOSE_CHILD_ISSUES, side_effect=_SimulatedProcessCrash("crash")):
            with pytest.raises(_SimulatedProcessCrash):
                Integrator(config).run()
        integrator_env.add_label.assert_called_once_with(1, "integration:included")

        # サイクル1でラベルが実際に永続化されたことを、2サイクル目の
        # `active_done_tasks`取得結果に反映する。
        integrator_env.set_done_issues(self._included(1, "task-1"))

        res = Integrator(config).run()

        assert res["status"] == "no_done_tasks"
        assert res["retried_closed_issues"] == [1]
        integrator_env.close_issue.assert_called_once_with(1, "completed", comment=ANY)
