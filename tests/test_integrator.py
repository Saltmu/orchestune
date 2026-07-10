from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from orchestune.dispatch_targets import DispatchHandle
from orchestune.github import IssueRecord, PrRecord
from orchestune.integrator import Integrator, IntegratorConfig


def _issue(
    number: int,
    labels: tuple[str, ...] = (),
    subtask_id: str = "",
    depends_on: tuple[str, ...] = (),
    parent_number: int | None = 100,
    state: str = "OPEN",
    parent_state: str = "OPEN",
) -> IssueRecord:
    body = "```yaml\n"
    if subtask_id:
        body += f"subtask_id: {subtask_id}\n"
    if depends_on:
        body += "depends_on:\n"
        for dep in depends_on:
            body += f"  - {dep}\n"
    body += "```\n"
    parent = (
        {"number": parent_number, "state": parent_state}
        if parent_number is not None
        else None
    )
    return IssueRecord(
        number=number,
        title=f"Test Issue {number}",
        body=body,
        labels=labels,
        created_at="2026-07-07T00:00:00Z",
        state=state,
        parent=parent,
    )


class TestIntegrator:
    @patch("orchestune.integrator.github.list_issues_by_label")
    def test_no_done_tasks(self, mock_list):
        mock_list.return_value = []
        config = IntegratorConfig(apply=True)
        integrator = Integrator(config)
        res = integrator.run()
        assert res["status"] == "no_done_tasks"

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    @patch("orchestune.integrator.github.list_open_prs")
    @patch("orchestune.integrator.github.create_pull_request")
    def test_success_integration(
        self, mock_create_pr, mock_open_prs, mock_run, mock_list
    ):
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        issue_b = _issue(
            2, labels=("status:done",), subtask_id="task-2", depends_on=("task-1",)
        )

        def list_side_effect(label, *args, **kwargs):
            if label == "status:done":
                return [issue_b, issue_a]
            return [issue_a, issue_b]

        mock_list.side_effect = list_side_effect

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )
        mock_open_prs.return_value = []
        mock_create_pr.return_value = 999

        config = IntegratorConfig(apply=True, parent_issue_number=100)
        integrator = Integrator(config)

        res = integrator.run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-1", "task-2"]
        assert res["integration_pr_number"] == 999
        assert res["semantic_review_dispatched"] is False

        merge_calls = [
            call for call in mock_run.call_args_list if "merge" in call.args[0]
        ]
        assert len(merge_calls) == 2
        assert any("claude/issue-1-task-1" in arg for arg in merge_calls[0].args[0])
        assert any("claude/issue-2-task-2" in arg for arg in merge_calls[1].args[0])

        # 統合ブランチからmainへのPRが作成され、成功時の統合作業はここで完結する
        # （最終マージは常に人間が行う）。
        mock_create_pr.assert_called_once()
        assert (
            mock_create_pr.call_args.kwargs["head"]
            == "integration/temp-parent-issue-100"
        )
        assert mock_create_pr.call_args.kwargs["base"] == "parent/issue-100"
        assert "task-1" in mock_create_pr.call_args.kwargs["body"]
        assert "人間が行ってください" in mock_create_pr.call_args.kwargs["body"]

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    @patch("orchestune.integrator.github.create_pull_request")
    def test_success_integration_reuses_existing_open_pr(
        self, mock_create_pr, mock_run, mock_list
    ):
        # 既にintegration/temp-main→mainのopenなPRがある場合は重複作成しない。
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )

        with patch("orchestune.integrator.github.list_open_prs") as mock_open_prs:
            mock_open_prs.return_value = [
                PrRecord(number=777, head_ref="integration/temp-main", changed_files=())
            ]
            config = IntegratorConfig(apply=True)
            integrator = Integrator(config)
            res = integrator.run()

        assert res["status"] == "success"
        assert res["integration_pr_number"] == 777
        mock_create_pr.assert_not_called()

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    @patch("orchestune.integrator.github.list_open_prs")
    @patch("orchestune.integrator.github.create_pull_request")
    def test_success_integration_pr_creation_failure_is_non_fatal(
        self, mock_create_pr, mock_open_prs, mock_run, mock_list
    ):
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )
        mock_open_prs.return_value = []
        mock_create_pr.side_effect = RuntimeError("no commits between main and branch")

        config = IntegratorConfig(apply=True)
        integrator = Integrator(config)
        res = integrator.run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-1"]
        assert res["integration_pr_number"] is None
        assert res["semantic_review_dispatched"] is False

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    @patch("orchestune.integrator.github.remove_label")
    @patch("orchestune.integrator.github.add_label")
    @patch("orchestune.integrator.github.add_comment")
    def test_merge_conflict_failure(
        self, mock_comment, mock_add, mock_remove, mock_run, mock_list
    ):
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")

        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]

        def run_side_effect(args, **kwargs):
            if "checkout" in args:
                return subprocess.CompletedProcess(args=args, returncode=0)
            if "merge" in args:
                raise subprocess.CalledProcessError(
                    returncode=1, cmd=args, stderr=b"CONFLICT (content): Merge conflict"
                )
            return subprocess.CompletedProcess(args=args, returncode=0)

        mock_run.side_effect = run_side_effect

        config = IntegratorConfig(apply=True)
        integrator = Integrator(config)

        res = integrator.run()

        assert res["status"] == "failure"
        assert "task-1" in res["failed"]

        mock_remove.assert_called_with(1, "status:done")
        mock_add.assert_called_with(1, "status:queued")
        mock_comment.assert_called_once()
        assert "Merge conflict" in mock_comment.call_args[0][1]

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    @patch("orchestune.integrator.github.remove_label")
    @patch("orchestune.integrator.github.add_label")
    @patch("orchestune.integrator.github.add_comment")
    def test_ci_failure_recovery(
        self, mock_comment, mock_add, mock_remove, mock_run, mock_list
    ):
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]

        def run_side_effect(args, **kwargs):
            if "local-ci.sh" in args[0] or "local-ci.sh" in args:
                raise subprocess.CalledProcessError(
                    returncode=1,
                    cmd=args,
                    output=b"5 passed, 1 failed",
                    stderr=b"CI fail",
                )
            return subprocess.CompletedProcess(args=args, returncode=0)

        mock_run.side_effect = run_side_effect

        config = IntegratorConfig(apply=True)
        integrator = Integrator(config)
        res = integrator.run()

        assert res["status"] == "failure"
        assert "task-1" in res["failed"]

        reset_calls = [
            call for call in mock_run.call_args_list if "reset" in call.args[0]
        ]
        assert len(reset_calls) == 1
        assert "HEAD~1" in reset_calls[0].args[0]

        mock_remove.assert_called_with(1, "status:done")
        mock_add.assert_called_with(1, "status:queued")
        comment_body = mock_comment.call_args[0][1]
        assert "CI verification failed" in comment_body
        # #295: CI出力が破棄されず、コメントに含まれることを検証する
        assert "CI fail" in comment_body
        assert "5 passed, 1 failed" in comment_body

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    @patch("orchestune.integrator.github.remove_label")
    @patch("orchestune.integrator.github.add_label")
    @patch("orchestune.integrator.github.add_comment")
    def test_ci_failure_output_is_logged_to_job_log(
        self, mock_comment, mock_add, mock_remove, mock_run, mock_list, capsys
    ):
        # #295: GitHub Actionsのジョブログからも追跡できるよう、
        # コメントへの切り詰め有無に関わらず出力全文をstderrへprintする。
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]

        def run_side_effect(args, **kwargs):
            if "local-ci.sh" in args[0] or "local-ci.sh" in args:
                raise subprocess.CalledProcessError(
                    returncode=1, cmd=args, stderr=b"UNIQUE_JOB_LOG_MARKER"
                )
            return subprocess.CompletedProcess(args=args, returncode=0)

        mock_run.side_effect = run_side_effect

        config = IntegratorConfig(apply=True)
        integrator = Integrator(config)
        integrator.run()

        captured = capsys.readouterr()
        assert "UNIQUE_JOB_LOG_MARKER" in captured.err

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    @patch("orchestune.integrator.github.remove_label")
    @patch("orchestune.integrator.github.add_label")
    @patch("orchestune.integrator.github.add_comment")
    def test_ci_failure_comment_truncates_long_output(
        self, mock_comment, mock_add, mock_remove, mock_run, mock_list
    ):
        # コメント本文の肥大化を避けるため、末尾のみを埋め込む。
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]

        long_output = ("x" * 10000 + "TAIL_MARKER").encode()

        def run_side_effect(args, **kwargs):
            if "local-ci.sh" in args[0] or "local-ci.sh" in args:
                raise subprocess.CalledProcessError(
                    returncode=1, cmd=args, output=long_output
                )
            return subprocess.CompletedProcess(args=args, returncode=0)

        mock_run.side_effect = run_side_effect

        config = IntegratorConfig(apply=True)
        integrator = Integrator(config)
        integrator.run()

        comment_body = mock_comment.call_args[0][1]
        assert "TAIL_MARKER" in comment_body
        assert len(comment_body) < 6000

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    def test_fetches_branch_with_explicit_refspec_before_merge(
        self, mock_run, mock_list
    ):
        # actions/checkout@v6 のデフォルト（単一ブランチの浅いclone）では
        # `git fetch origin <branch>`（refspec省略）だけでは
        # `origin/<branch>` のremote-trackingブランチが作成されないため、
        # 明示的な refspec 付きでfetchしてからマージする必要がある。
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )

        config = IntegratorConfig(apply=True)
        integrator = Integrator(config)
        res = integrator.run()

        assert res["status"] == "success"

        fetch_calls = [
            call for call in mock_run.call_args_list if "fetch" in call.args[0]
        ]
        assert len(fetch_calls) == 1
        assert fetch_calls[0].args[0] == [
            "git",
            "fetch",
            "origin",
            "claude/issue-1-task-1:refs/remotes/origin/claude/issue-1-task-1",
        ]

        fetch_index = mock_run.call_args_list.index(fetch_calls[0])
        merge_index = next(
            i
            for i, call in enumerate(mock_run.call_args_list)
            if "merge" in call.args[0] and "--no-ff" in call.args[0]
        )
        assert fetch_index < merge_index

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    def test_configures_git_identity_before_merging(self, mock_run, mock_list):
        # CI環境（actions/checkout等）ではgit committer identityが未設定のことがあり、
        # `git merge --no-ff`でマージコミットを作成する際に
        # "Committer identity unknown" で必ず失敗するため、事前に設定する必要がある。
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )

        config = IntegratorConfig(apply=True)
        integrator = Integrator(config)
        res = integrator.run()

        assert res["status"] == "success"

        name_calls = [
            call
            for call in mock_run.call_args_list
            if call.args[0][:3] == ["git", "config", "user.name"]
        ]
        email_calls = [
            call
            for call in mock_run.call_args_list
            if call.args[0][:3] == ["git", "config", "user.email"]
        ]
        assert len(name_calls) == 1
        assert len(email_calls) == 1

        identity_index = mock_run.call_args_list.index(name_calls[0])
        merge_index = next(
            i
            for i, call in enumerate(mock_run.call_args_list)
            if "merge" in call.args[0] and "--no-ff" in call.args[0]
        )
        assert identity_index < merge_index

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    def test_unshallows_repository_before_merging_when_shallow(
        self, mock_run, mock_list
    ):
        # actions/checkout@v6 のデフォルト（浅いclone）のままタスクブランチを
        # fetchすると、そのコミットも親を持たない浅い状態になり、mainとの共通の
        # 祖先が見つからず『refusing to merge unrelated histories』でmergeが
        # 必ず失敗するため、浅いリポジトリの場合は事前に履歴を深くする必要がある。
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]

        def run_side_effect(args, **kwargs):
            if args[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=b"true\n", stderr=b""
                )
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=b"", stderr=b""
            )

        mock_run.side_effect = run_side_effect

        config = IntegratorConfig(apply=True)
        integrator = Integrator(config)
        res = integrator.run()

        assert res["status"] == "success"

        unshallow_calls = [
            call for call in mock_run.call_args_list if "--unshallow" in call.args[0]
        ]
        assert len(unshallow_calls) == 1
        assert unshallow_calls[0].args[0] == [
            "git",
            "fetch",
            "--unshallow",
            "origin",
            "main",
        ]

        unshallow_index = mock_run.call_args_list.index(unshallow_calls[0])
        branch_fetch_index = next(
            i
            for i, call in enumerate(mock_run.call_args_list)
            if call.args[0]
            == [
                "git",
                "fetch",
                "origin",
                "claude/issue-1-task-1:refs/remotes/origin/claude/issue-1-task-1",
            ]
        )
        assert unshallow_index < branch_fetch_index

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    def test_skips_unshallow_when_repository_is_not_shallow(self, mock_run, mock_list):
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]

        def run_side_effect(args, **kwargs):
            if args[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=b"false\n", stderr=b""
                )
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=b"", stderr=b""
            )

        mock_run.side_effect = run_side_effect

        config = IntegratorConfig(apply=True)
        integrator = Integrator(config)
        res = integrator.run()

        assert res["status"] == "success"

        unshallow_calls = [
            call for call in mock_run.call_args_list if "--unshallow" in call.args[0]
        ]
        assert len(unshallow_calls) == 0

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    @patch("orchestune.integrator.github.remove_label")
    @patch("orchestune.integrator.github.add_label")
    @patch("orchestune.integrator.github.add_comment")
    @patch("orchestune.integrator.github.list_open_prs")
    @patch(
        "orchestune.integrator.github.is_branch_merged_into",
        return_value=False,
    )
    def test_fetch_failure_is_handled_like_merge_failure(
        self,
        mock_is_merged,
        mock_list_prs,
        mock_comment,
        mock_add,
        mock_remove,
        mock_run,
        mock_list,
    ):
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]

        mock_list_prs.return_value = [
            PrRecord(
                number=10,
                head_ref="claude/issue-1-task-1",
                changed_files=(),
                review_decision="APPROVED",
                is_ci_passing=True,
            )
        ]

        def run_side_effect(args, **kwargs):
            if "fetch" in args:
                raise subprocess.CalledProcessError(
                    returncode=1,
                    cmd=args,
                    stderr=b"fatal: couldn't find remote ref claude/issue-1-task-1",
                )
            return subprocess.CompletedProcess(args=args, returncode=0)

        mock_run.side_effect = run_side_effect

        config = IntegratorConfig(apply=True)
        integrator = Integrator(config)
        res = integrator.run()

        assert res["status"] == "failure"
        assert "task-1" in res["failed"]

        merge_calls = [
            call
            for call in mock_run.call_args_list
            if "merge" in call.args[0] and "--no-ff" in call.args[0]
        ]
        assert len(merge_calls) == 0

        mock_remove.assert_called_with(1, "status:done")
        mock_add.assert_called_with(1, "status:queued")
        mock_is_merged.assert_called_once_with("claude/issue-1-task-1", "main")

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    @patch("orchestune.integrator.github.remove_label")
    @patch("orchestune.integrator.github.add_label")
    @patch("orchestune.integrator.github.add_comment")
    def test_merge_conflict_aborts_before_next_task(
        self, mock_comment, mock_add, mock_remove, mock_run, mock_list
    ):
        # task-1 のマージがコンフリクトで失敗しても、task-2 は巻き添えを受けず
        # クリーンな状態から正常にマージ・統合されるべき。
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        issue_b = _issue(2, labels=("status:done",), subtask_id="task-2")

        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a, issue_b]

        def run_side_effect(args, **kwargs):
            if "merge" in args and any("claude/issue-1-task-1" in a for a in args):
                raise subprocess.CalledProcessError(
                    returncode=1,
                    cmd=args,
                    stderr=b"CONFLICT (content): Merge conflict",
                )
            return subprocess.CompletedProcess(args=args, returncode=0)

        mock_run.side_effect = run_side_effect

        config = IntegratorConfig(apply=True)
        integrator = Integrator(config)

        res = integrator.run()

        assert res["status"] == "partial_success"
        assert res["merged"] == ["task-2"]
        assert res["failed"] == ["task-1"]

        abort_calls = [
            call
            for call in mock_run.call_args_list
            if "merge" in call.args[0] and "--abort" in call.args[0]
        ]
        assert len(abort_calls) == 1

        merge_call_indices = [
            i
            for i, call in enumerate(mock_run.call_args_list)
            if "merge" in call.args[0] and "--no-ff" in call.args[0]
        ]
        abort_call_index = mock_run.call_args_list.index(abort_calls[0])
        # abort は task-1 のマージ失敗の直後、task-2 のマージ試行より前に呼ばれる
        assert merge_call_indices[0] < abort_call_index < merge_call_indices[1]

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    @patch("orchestune.integrator.github.remove_label")
    @patch("orchestune.integrator.github.add_label")
    @patch("orchestune.integrator.github.add_comment")
    def test_ci_failure_runs_only_once_no_whole_script_retry(
        self, mock_comment, mock_add, mock_remove, mock_run, mock_list
    ):
        # #208: quarantine対象外のflakyテストによる失敗は、丸ごとリトライで
        # 隠さず、1回の実行結果どおりに正しくCI失敗として扱う。
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]

        ci_calls = 0

        def run_side_effect(args, **kwargs):
            nonlocal ci_calls
            if "local-ci.sh" in args[0] or "local-ci.sh" in args:
                ci_calls += 1
                raise subprocess.CalledProcessError(returncode=1, cmd=args)
            return subprocess.CompletedProcess(args=args, returncode=0)

        mock_run.side_effect = run_side_effect

        config = IntegratorConfig(apply=True)
        integrator = Integrator(config)
        res = integrator.run()

        assert res["status"] == "failure"
        assert res["failed"] == ["task-1"]
        assert ci_calls == 1

    @patch("orchestune.integrator.github.list_open_prs")
    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    def test_semantic_review_dispatched_fire_and_forget_after_pr_created(
        self, mock_run, mock_list, mock_open_prs
    ):
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )
        # 統合ブランチ(temp-main)向けのopenなPRはまだ無いので新規作成される。
        mock_open_prs.return_value = []

        calls = []

        class DispatchingCoordinator:
            def dispatch_review(self, **kwargs):
                calls.append(kwargs)
                return DispatchHandle(
                    external_id="s1", external_url="https://claude.ai/code/s/s1"
                )

        config = IntegratorConfig(
            apply=True,
            parent_issue_number=100,
            enable_semantic_review=True,
            coordinator=DispatchingCoordinator(),
        )
        integrator = Integrator(config)
        with patch(
            "orchestune.integrator.github.create_pull_request"
        ) as mock_create_pr:
            mock_create_pr.return_value = 315
            res = integrator.run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-1"]
        assert res["integration_pr_number"] == 315
        assert res["semantic_review_dispatched"] is True

        # レビューは統合PR番号付きでfire-and-forgetで起動される。結果を待ったり、
        # 状態を記録したりはしない（自動マージ等の後続処理が無くなったため）。
        assert len(calls) == 1
        assert calls[0]["merged_subtask_ids"] == ["task-1"]
        assert calls[0]["temp_branch"] == "integration/temp-parent-issue-100"
        assert calls[0]["pr_number"] == 315

        # ブランチのforce pushは行われる（起動セッションがレビューできるように）
        push_calls = [
            call for call in mock_run.call_args_list if "push" in call.args[0]
        ]
        assert len(push_calls) == 1

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    @patch("orchestune.integrator.github.list_open_prs")
    @patch("orchestune.integrator.github.create_pull_request")
    def test_semantic_review_explicitly_disabled_is_not_dispatched(
        self, mock_create_pr, mock_open_prs, mock_run, mock_list
    ):
        # enable_semantic_review=False を明示するとレビューは委譲されない。
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )
        mock_open_prs.return_value = []
        mock_create_pr.return_value = 315

        called = []

        class TrackingCoordinator:
            def dispatch_review(self, **kwargs):
                called.append(1)
                return DispatchHandle(external_id="s")

        config = IntegratorConfig(
            apply=True,
            parent_issue_number=100,
            enable_semantic_review=False,
            coordinator=TrackingCoordinator(),
        )
        integrator = Integrator(config)
        res = integrator.run()

        assert res["status"] == "success"
        assert called == []
        assert res["semantic_review_dispatched"] is False

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    def test_semantic_review_default_on_but_skips_without_coordinator(
        self, mock_run, mock_list
    ):
        # 既定ONだが coordinator 未注入なら安全にスキップ（既存の直接構築呼び出しを壊さない）。
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )

        config = IntegratorConfig(apply=True)  # coordinator=None, enable=既定True
        assert config.enable_semantic_review is True
        integrator = Integrator(config)
        with (
            patch("orchestune.integrator.github.list_open_prs", return_value=[]),
            patch("orchestune.integrator.github.create_pull_request", return_value=315),
        ):
            res = integrator.run()

        assert res["status"] == "success"
        assert res["semantic_review_dispatched"] is False

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    def test_integration_with_closed_done_task(self, mock_run, mock_list):
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        issue_b = _issue(
            2, labels=("status:done",), subtask_id="task-2", depends_on=("task-1",)
        )

        def list_side_effect(label, state="open"):
            if label == "status:done":
                if state == "all":
                    return [issue_a, issue_b]
                else:
                    return [issue_b]
            return []

        mock_list.side_effect = list_side_effect

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )

        config = IntegratorConfig(apply=True)
        integrator = Integrator(config)
        res = integrator.run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-1", "task-2"]

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    @patch("pathlib.Path.exists")
    def test_integrator_ci_env_injection(self, mock_exists, mock_run, mock_list):
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]

        # .venv と bin_path の exists() を True にする
        mock_exists.return_value = True

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )

        config = IntegratorConfig(apply=True)
        integrator = Integrator(config)
        integrator.run()

        # subprocess.run の引数をチェック
        ci_run_calls = [
            call
            for call in mock_run.call_args_list
            if len(call.args) > 0
            and isinstance(call.args[0], list)
            and "./scripts/local-ci.sh" in call.args[0]
        ]
        assert len(ci_run_calls) == 1
        call_kwargs = ci_run_calls[0].kwargs
        assert "env" in call_kwargs
        env = call_kwargs["env"]
        assert "VIRTUAL_ENV" in env

        expected_venv = integrator.original_root / ".venv"
        if "tools/orchestune" in str(expected_venv):
            expected_venv = expected_venv.parent.parent.parent / ".venv"

        assert env["VIRTUAL_ENV"] == str(expected_venv.resolve())
        assert "PATH" in env
        assert env["PATH"].startswith(str(expected_venv / "bin"))

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    @patch("orchestune.integrator.github.list_open_prs")
    @patch(
        "orchestune.integrator.github.is_branch_merged_into",
        return_value=False,
    )
    def test_fetch_failure_without_merged_pr_is_not_treated_as_success(
        self, mock_is_merged, mock_list_prs, mock_run, mock_list
    ):
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]

        mock_list_prs.return_value = []

        def run_side_effect(args, **kwargs):
            if "fetch" in args:
                raise subprocess.CalledProcessError(
                    returncode=1, cmd=args, stderr=b"fatal: couldn't find remote ref"
                )
            return subprocess.CompletedProcess(args=args, returncode=0)

        mock_run.side_effect = run_side_effect

        config = IntegratorConfig(apply=True)
        integrator = Integrator(config)

        with (
            patch("orchestune.integrator.github.remove_label") as mock_remove,
            patch("orchestune.integrator.github.add_label") as mock_add,
            patch("orchestune.integrator.github.add_comment") as mock_comment,
        ):
            res = integrator.run()

        assert res["status"] == "failure"
        assert res["merged"] == []
        assert res["failed"] == ["task-1"]
        mock_remove.assert_called_once_with(1, "status:done")
        mock_add.assert_called_once_with(1, "status:queued")
        mock_comment.assert_called_once()
        mock_is_merged.assert_called_once_with("claude/issue-1-task-1", "main")

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    @patch("orchestune.integrator.github.list_open_prs")
    @patch(
        "orchestune.integrator.github.is_branch_merged_into",
        return_value=True,
    )
    def test_fetch_failure_is_skipped_when_matching_pr_is_merged(
        self, mock_is_merged, mock_list_prs, mock_run, mock_list
    ):
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]
        mock_list_prs.return_value = []

        def run_side_effect(args, **kwargs):
            if "fetch" in args:
                raise subprocess.CalledProcessError(
                    returncode=1, cmd=args, stderr=b"fatal: couldn't find remote ref"
                )
            return subprocess.CompletedProcess(args=args, returncode=0)

        mock_run.side_effect = run_side_effect

        with (
            patch("orchestune.integrator.github.remove_label") as mock_remove,
            patch("orchestune.integrator.github.add_label") as mock_add,
            patch("orchestune.integrator.github.add_comment") as mock_comment,
        ):
            res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "success"
        assert res["merged"] == ["task-1"]
        mock_is_merged.assert_called_once_with("claude/issue-1-task-1", "main")
        mock_remove.assert_not_called()
        mock_add.assert_not_called()
        mock_comment.assert_not_called()

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    @patch("orchestune.integrator.github.list_open_prs")
    @patch(
        "orchestune.integrator.github.is_branch_merged_into",
        side_effect=RuntimeError("GitHub API unavailable"),
    )
    def test_fetch_failure_fails_closed_when_merged_lookup_fails(
        self, mock_is_merged, mock_list_prs, mock_run, mock_list
    ):
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]
        mock_list_prs.return_value = []

        def run_side_effect(args, **kwargs):
            if "fetch" in args:
                raise subprocess.CalledProcessError(
                    returncode=1, cmd=args, stderr=b"temporary network failure"
                )
            return subprocess.CompletedProcess(args=args, returncode=0)

        mock_run.side_effect = run_side_effect

        with (
            patch("orchestune.integrator.github.remove_label"),
            patch("orchestune.integrator.github.add_label"),
            patch("orchestune.integrator.github.add_comment"),
        ):
            res = Integrator(IntegratorConfig(apply=True)).run()

        assert res["status"] == "failure"
        assert res["failed"] == ["task-1"]
        mock_is_merged.assert_called_once_with("claude/issue-1-task-1", "main")

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    @patch("orchestune.integrator.github.list_open_prs")
    @patch("orchestune.integrator.github.create_pull_request")
    def test_exclude_closed_tasks(
        self, mock_create_pr, mock_open_prs, mock_run, mock_list
    ):
        # 1. 自身がCLOSEDなタスクは除外されること
        issue_closed = _issue(
            1, labels=("status:done",), subtask_id="task-1", state="CLOSED"
        )
        # 2. 親IssueがCLOSEDなタスクは除外されること
        issue_parent_closed = _issue(
            2,
            labels=("status:done",),
            subtask_id="task-2",
            parent_state="CLOSED",
        )
        # 3. どちらもOPENなタスクは検証されること
        issue_active = _issue(3, labels=("status:done",), subtask_id="task-3")

        def list_side_effect(label, *args, **kwargs):
            if label == "status:done":
                return [issue_closed, issue_parent_closed, issue_active]
            return [issue_closed, issue_parent_closed, issue_active]

        mock_list.side_effect = list_side_effect
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )
        mock_open_prs.return_value = []
        mock_create_pr.return_value = 888

        config = IntegratorConfig(apply=True)
        integrator = Integrator(config)
        res = integrator.run()

        # task-1 と task-2 は除外され、task-3 のみがマージ検証される
        assert res["status"] == "success"
        assert res["merged"] == ["task-3"]
        assert res["integration_pr_number"] == 888

        # 実際にマージが走ったのは task-3 のみであることを確認
        merge_calls = [
            call for call in mock_run.call_args_list if "merge" in call.args[0]
        ]
        assert len(merge_calls) == 1
        assert any("claude/issue-3-task-3" in arg for arg in merge_calls[0].args[0])


class TestIntegratorWorktreeIsolation:
    """#254: repository_rootの一時差し替え・ワークツリー分離・クリーンアップの検証。"""

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    @patch("orchestune.integrator.github.list_open_prs")
    @patch("orchestune.integrator.github.create_pull_request")
    def test_cleanup_uses_original_root_not_cwd(
        self, mock_create_pr, mock_open_prs, mock_run, mock_list
    ):
        # repository_rootをカレントディレクトリ以外に明示指定した場合でも、
        # 一時ワークツリーの削除はoriginal_root（呼び出し時のrepository_root）
        # を基準に行われるべきで、決め打ちのPath(".")であってはならない。
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]
        mock_open_prs.return_value = []
        mock_create_pr.return_value = 1
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )

        custom_root = Path("/custom/repo/root")
        config = IntegratorConfig(apply=True, repository_root=custom_root)
        integrator = Integrator(config)
        res = integrator.run()

        assert res["status"] == "success"
        remove_calls = [
            call
            for call in mock_run.call_args_list
            if "worktree" in call.args[0] and "remove" in call.args[0]
        ]
        assert len(remove_calls) == 1
        assert remove_calls[0].kwargs["cwd"] == str(custom_root.resolve())

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    @patch("orchestune.integrator.github.remove_label")
    @patch("orchestune.integrator.github.add_label")
    @patch("orchestune.integrator.github.add_comment")
    def test_cleanup_runs_even_when_merge_fails(
        self, mock_comment, mock_add, mock_remove, mock_run, mock_list
    ):
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]

        def run_side_effect(args, **kwargs):
            if "merge" in args and "--abort" not in args:
                raise subprocess.CalledProcessError(returncode=1, cmd=args, stderr=b"")
            return subprocess.CompletedProcess(args=args, returncode=0)

        mock_run.side_effect = run_side_effect

        custom_root = Path("/custom/repo/root")
        config = IntegratorConfig(apply=True, repository_root=custom_root)
        integrator = Integrator(config)
        res = integrator.run()

        assert res["status"] == "failure"
        remove_calls = [
            call
            for call in mock_run.call_args_list
            if "worktree" in call.args[0] and "remove" in call.args[0]
        ]
        assert len(remove_calls) == 1
        assert remove_calls[0].kwargs["cwd"] == str(custom_root.resolve())

    @patch("orchestune.integrator.github.list_issues_by_label")
    @patch("orchestune.integrator.subprocess.run")
    def test_worktree_creation_failure_reports_status(self, mock_run, mock_list):
        issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")
        mock_list.side_effect = lambda label, *args, **kwargs: [issue_a]

        def run_side_effect(args, **kwargs):
            if "worktree" in args and "add" in args:
                raise subprocess.CalledProcessError(returncode=1, cmd=args, stderr=b"")
            return subprocess.CompletedProcess(args=args, returncode=0)

        mock_run.side_effect = run_side_effect

        config = IntegratorConfig(apply=True)
        integrator = Integrator(config)
        res = integrator.run()

        assert res["status"] == "failed_to_create_temp_worktree"
        remove_calls = [
            call
            for call in mock_run.call_args_list
            if "worktree" in call.args[0] and "remove" in call.args[0]
        ]
        assert remove_calls == []


class TestIntegratorRelativeRepositoryRoot:
    """#48: repository_rootが`Path(".")`以外の相対パスの場合、worktreeの作成先と
    その後のcheckout/merge/CIが参照するcwdがずれて処理全体が失敗する不具合の回帰テスト。

    subprocessをモックせず、実際の一時Gitリポジトリに対してIntegratorを走らせる。
    """

    def test_relative_repository_root_succeeds_with_real_git_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
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
            subprocess.run(
                ["git", "config", "user.name", "test-bot"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "test-bot@example.com"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "checkout", "-b", "main"],
                cwd=str(repo_path),
                capture_output=True,
            )

            def commit_file(
                rel_path: str, content: str, msg: str, executable: bool = False
            ) -> None:
                p = repo_path / rel_path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                if executable:
                    p.chmod(0o755)
                subprocess.run(
                    ["git", "add", rel_path],
                    cwd=str(repo_path),
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "commit", "-m", msg],
                    cwd=str(repo_path),
                    check=True,
                    capture_output=True,
                )

            commit_file("README.md", "dummy\n", "Initial commit")
            commit_file(
                "scripts/local-ci.sh",
                "#!/bin/bash\nexit 0\n",
                "Add local-ci.sh",
                executable=True,
            )
            subprocess.run(
                ["git", "push", "-u", "origin", "main"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
            )

            subprocess.run(
                ["git", "checkout", "-b", "claude/issue-1-task-1"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
            )
            commit_file("feature.txt", "feature\n", "Add feature")
            subprocess.run(
                ["git", "push", "-u", "origin", "claude/issue-1-task-1"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "checkout", "main"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
            )

            issue_a = _issue(1, labels=("status:done",), subtask_id="task-1")

            original_cwd = os.getcwd()
            # `repository_root=Path("repo")`が実際にプロセスのcwd相対のパスになるよう、
            # cloneした"repo"の親ディレクトリへchdirする。
            os.chdir(str(workspace))
            try:
                with (
                    patch(
                        "orchestune.integrator.github.list_issues_by_label",
                        lambda label, *a, **k: [issue_a],
                    ),
                    patch(
                        "orchestune.integrator.github.list_open_prs", return_value=[]
                    ),
                    patch(
                        "orchestune.integrator.github.create_pull_request",
                        return_value=999,
                    ),
                    patch("orchestune.integrator.github.add_label"),
                    patch("orchestune.integrator.github.remove_label"),
                    patch("orchestune.integrator.github.add_comment"),
                ):
                    config = IntegratorConfig(repository_root=Path("repo"), apply=True)
                    integrator = Integrator(config)
                    res = integrator.run()
            finally:
                os.chdir(original_cwd)

            assert res["status"] == "success"
            assert res["merged"] == ["task-1"]
            assert res["integration_pr_number"] == 999

            # worktreeの作成先と参照先がずれた場合に発生していた「二重化されたパス」が
            # できていないことも確認する。
            assert not (workspace / "repo" / "repo").exists()
