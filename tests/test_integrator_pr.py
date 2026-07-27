from __future__ import annotations

from unittest.mock import patch

from orchestune.dispatcher import Task
from orchestune.github import PrRecord
from orchestune.integrator_pr import (
    ensure_integration_pr,
    ensure_parent_final_pr,
    handle_merge_failure,
)


def _task(issue_number=1, subtask_id="task-1"):
    return Task(
        issue_number=issue_number,
        subtask_id=subtask_id,
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:done",),
        created_at="2026-01-01T00:00:00+00:00",
    )


class TestHandleMergeFailure:
    """#254: primary status(status:done/status:queued)遷移が途中失敗しても、
    Issueがどちらのstatusにも属さなくなる（全statusキューから脱落する）ことを
    防ぐ。add(status:queued)をremove(status:done)より先に行うことで、
    途中失敗時も必ずどちらか一方が残る。"""

    @patch("orchestune.integrator_pr.github.add_comment")
    @patch("orchestune.integrator_pr.github.remove_label")
    @patch("orchestune.integrator_pr.github.add_label")
    def test_adds_queued_before_removing_done(
        self, mock_add_label, mock_remove_label, mock_add_comment
    ):
        call_order: list[str] = []
        mock_add_label.side_effect = lambda *a, **k: call_order.append("add")
        mock_remove_label.side_effect = lambda *a, **k: call_order.append("remove")

        handle_merge_failure(_task(), "CI failed", apply=True)

        mock_add_label.assert_called_once_with(1, "status:queued")
        mock_remove_label.assert_called_once_with(1, "status:done")
        assert call_order == ["add", "remove"]

    @patch("orchestune.integrator_pr.github.add_comment")
    @patch("orchestune.integrator_pr.github.remove_label")
    @patch("orchestune.integrator_pr.github.add_label")
    def test_add_label_failure_leaves_status_done_untouched(
        self, mock_add_label, mock_remove_label, mock_add_comment
    ):
        """Reproducer(境界1): add(status:queued)が一時障害で失敗した場合、
        status:doneのremoveには到達せず、Issueはstatus:doneのまま残る
        （次cycleのIntegratorが同じdoneタスクとして再検出できる）。"""
        mock_add_label.side_effect = RuntimeError("transient API failure")

        try:
            handle_merge_failure(_task(), "CI failed", apply=True)
        except RuntimeError:
            pass

        mock_remove_label.assert_not_called()
        mock_add_comment.assert_not_called()

    @patch("orchestune.integrator_pr.github.add_comment")
    @patch("orchestune.integrator_pr.github.remove_label")
    @patch("orchestune.integrator_pr.github.add_label")
    def test_remove_label_failure_after_add_succeeds_does_not_lose_status(
        self, mock_add_label, mock_remove_label, mock_add_comment
    ):
        """Reproducer(境界2): addが成功した直後にremove(status:done)が
        一時障害で失敗しても、Issueは既にstatus:queuedを持っているため
        （status:doneも一時的に残るが）全statusキューから脱落しない。"""
        mock_remove_label.side_effect = RuntimeError("transient API failure")

        try:
            handle_merge_failure(_task(), "CI failed", apply=True)
        except RuntimeError:
            pass

        mock_add_label.assert_called_once_with(1, "status:queued")
        mock_add_comment.assert_not_called()

    @patch("orchestune.integrator_pr.github.add_comment")
    @patch("orchestune.integrator_pr.github.remove_label")
    @patch("orchestune.integrator_pr.github.add_label")
    def test_success_posts_comment_with_reason(
        self, mock_add_label, mock_remove_label, mock_add_comment
    ):
        """通常のCI失敗requeue・コメント追加に回帰がないことを確認する。"""
        handle_merge_failure(_task(), "Merge conflict: boom", apply=True)

        mock_add_comment.assert_called_once()
        assert mock_add_comment.call_args[0][0] == 1
        assert "Merge conflict: boom" in mock_add_comment.call_args[0][1]

    @patch("orchestune.integrator_pr.github.add_comment")
    @patch("orchestune.integrator_pr.github.remove_label")
    @patch("orchestune.integrator_pr.github.add_label")
    def test_dry_run_does_not_touch_labels_or_comments(
        self, mock_add_label, mock_remove_label, mock_add_comment
    ):
        handle_merge_failure(_task(), "CI failed", apply=False)

        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()
        mock_add_comment.assert_not_called()


class TestEnsureIntegrationPrIdentity:
    """#243: head名だけの照合で外部fork・別baseのPRを再利用しないことを検証する。"""

    @patch("orchestune.integrator_pr.github.list_open_prs")
    @patch("orchestune.integrator_pr.github.create_pull_request")
    def test_reuses_pr_with_verified_head_base_and_same_repo(
        self, mock_create_pr, mock_open_prs
    ):
        mock_open_prs.return_value = [
            PrRecord(
                number=77,
                head_ref="integration/temp-parent-issue-100",
                changed_files=(),
                base_ref="parent/issue-100",
                is_cross_repository=False,
            )
        ]

        pr_number = ensure_integration_pr(
            "integration/temp-parent-issue-100", "origin/parent/issue-100", ["task-a"]
        )

        assert pr_number == 77
        mock_create_pr.assert_not_called()

    @patch("orchestune.integrator_pr.github.list_open_prs")
    @patch("orchestune.integrator_pr.github.create_pull_request")
    def test_does_not_reuse_cross_repository_pr_with_same_head_name(
        self, mock_create_pr, mock_open_prs
    ):
        """Reproducer: 外部forkの同名headRefName PRを正規統合PRと誤認しない。"""
        mock_open_prs.return_value = [
            PrRecord(
                number=666,
                head_ref="integration/temp-parent-issue-100",
                changed_files=(),
                base_ref="parent/issue-100",
                is_cross_repository=True,
            )
        ]
        mock_create_pr.return_value = 78

        pr_number = ensure_integration_pr(
            "integration/temp-parent-issue-100", "origin/parent/issue-100", ["task-a"]
        )

        assert pr_number == 78
        mock_create_pr.assert_called_once()

    @patch("orchestune.integrator_pr.github.list_open_prs")
    @patch("orchestune.integrator_pr.github.create_pull_request")
    def test_does_not_reuse_pr_with_different_base(self, mock_create_pr, mock_open_prs):
        mock_open_prs.return_value = [
            PrRecord(
                number=666,
                head_ref="integration/temp-parent-issue-100",
                changed_files=(),
                base_ref="some/other-base",
                is_cross_repository=False,
            )
        ]
        mock_create_pr.return_value = 79

        pr_number = ensure_integration_pr(
            "integration/temp-parent-issue-100", "origin/parent/issue-100", ["task-a"]
        )

        assert pr_number == 79
        mock_create_pr.assert_called_once()

    @patch("orchestune.integrator_pr.github.list_open_prs")
    @patch("orchestune.integrator_pr.github.create_pull_request")
    def test_does_not_reuse_pr_with_unknown_identity(
        self, mock_create_pr, mock_open_prs
    ):
        """identity不明（base_ref空・is_cross_repository不明）はfail closedで再利用しない。"""
        mock_open_prs.return_value = [
            PrRecord(
                number=666,
                head_ref="integration/temp-parent-issue-100",
                changed_files=(),
            )
        ]
        mock_create_pr.return_value = 80

        pr_number = ensure_integration_pr(
            "integration/temp-parent-issue-100", "origin/parent/issue-100", ["task-a"]
        )

        assert pr_number == 80
        mock_create_pr.assert_called_once()


class TestEnsureParentFinalPr:
    @patch("orchestune.integrator_pr.github.list_open_prs")
    @patch("orchestune.integrator_pr.github.create_pull_request")
    def test_creates_pr_from_parent_branch_to_main(self, mock_create_pr, mock_open_prs):
        mock_open_prs.return_value = []
        mock_create_pr.return_value = 555

        pr_number = ensure_parent_final_pr(100)

        assert pr_number == 555
        assert mock_create_pr.call_args.kwargs["head"] == "parent/issue-100"
        assert mock_create_pr.call_args.kwargs["base"] == "main"
        assert "100" in mock_create_pr.call_args.kwargs["body"]

    @patch("orchestune.integrator_pr.github.list_open_prs")
    @patch("orchestune.integrator_pr.github.create_pull_request")
    def test_reuses_existing_open_pr(self, mock_create_pr, mock_open_prs):
        mock_open_prs.return_value = [
            PrRecord(
                number=321,
                head_ref="parent/issue-100",
                changed_files=(),
                base_ref="main",
                is_cross_repository=False,
            )
        ]

        pr_number = ensure_parent_final_pr(100)

        assert pr_number == 321
        mock_create_pr.assert_not_called()

    @patch("orchestune.integrator_pr.github.list_open_prs")
    @patch("orchestune.integrator_pr.github.create_pull_request")
    def test_does_not_reuse_cross_repository_parent_pr(
        self, mock_create_pr, mock_open_prs
    ):
        """#243: 外部forkの同名parentブランチPRを最終統合PRとして再利用しない。"""
        mock_open_prs.return_value = [
            PrRecord(
                number=666,
                head_ref="parent/issue-100",
                changed_files=(),
                base_ref="main",
                is_cross_repository=True,
            )
        ]
        mock_create_pr.return_value = 322

        pr_number = ensure_parent_final_pr(100)

        assert pr_number == 322
        mock_create_pr.assert_called_once()

    @patch("orchestune.integrator_pr.github.list_open_prs")
    @patch("orchestune.integrator_pr.github.create_pull_request")
    def test_pr_creation_failure_is_non_fatal(self, mock_create_pr, mock_open_prs):
        mock_open_prs.return_value = []
        mock_create_pr.side_effect = RuntimeError("no commits between main and branch")

        pr_number = ensure_parent_final_pr(100)

        assert pr_number is None
