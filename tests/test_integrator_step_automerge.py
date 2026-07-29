"""子統合の確定: `LabelIncludedStep` / `AutoMergeChildIntegrationStep` /
`RetryChildIssueCloseStep`。

#170: `parent_issue_number`指定時のみ、統合PRを自動マージし対象の子Issueを
自動クローズする。最終マージ（親ブランチ→main）は引き続き人間が行うため、
`parent_issue_number`未指定（base_branch=main）時は一切自動マージ・クローズを
行わない。
"""

from __future__ import annotations

import subprocess
from unittest.mock import ANY, patch

import pytest

from orchestune.integrator import Integrator, IntegratorConfig
from tests.conftest import IntegratorEnv, make_done_issue

_CLOSE_CHILD_ISSUES = (
    "orchestune.integrator.AutoMergeChildIntegrationStep._close_merged_child_issues"
)


class _SimulatedProcessCrash(Exception):
    """#209: プロセス強制終了を模すための、他の例外型と衝突しない専用の例外。"""


def _child_config() -> IntegratorConfig:
    return IntegratorConfig(apply=True, parent_issue_number=100)


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
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.create_pull_request.side_effect = RuntimeError("no commits")

        res = Integrator(_child_config()).run()

        assert res["status"] == "success"
        assert res["integration_pr_number"] is None
        integrator_env.merge_pull_request.assert_not_called()
        integrator_env.close_issue.assert_not_called()

    def test_merge_failure_leaves_pr_open_and_skips_closing(
        self, integrator_env: IntegratorEnv
    ):
        # レビュー指摘(#170): マージ失敗を`status: success`で握り潰さず、
        # かつマージが確定していない以上`integration:included`も付与しない
        # （パイプライン順序をAutoMerge→Labelに変更し、非successでラベル
        # ステップ自体をスキップさせる）。
        integrator_env.set_done_issues(make_done_issue(1, subtask_id="task-1"))
        integrator_env.merge_pull_request.side_effect = subprocess.CalledProcessError(
            1, ["gh", "pr", "merge"], stderr=b"not mergeable"
        )

        res = Integrator(_child_config()).run()

        assert res["status"] == "auto_merge_failed"
        assert res["integration_pr_number"] == 999
        integrator_env.merge_pull_request.assert_called_once_with(999)
        integrator_env.close_issue.assert_not_called()
        integrator_env.add_label.assert_not_called()
        integrator_env.add_comment.assert_any_call(1, ANY)

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
        integrator_env.merge_pull_request.assert_called_once_with(999)

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
