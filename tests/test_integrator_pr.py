from __future__ import annotations

import pytest

from orchestune.integrator_pr import (
    ensure_integration_pr,
    ensure_parent_final_pr,
    handle_merge_failure,
)
from orchestune.models import PrRecord, Task


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


class _FakeForgeTest:
    @pytest.fixture(autouse=True)
    def _inject_forge(self, fake_forge):
        self.forge = fake_forge


class TestHandleMergeFailure(_FakeForgeTest):
    """#254: primary status(status:done/status:queued)遷移が途中失敗しても、
    Issueがどちらのstatusにも属さなくなる（全statusキューから脱落する）ことを
    防ぐ。add(status:queued)をremove(status:done)より先に行うことで、
    途中失敗時も必ずどちらか一方が残る。"""

    def test_adds_queued_before_removing_done(self):
        call_order: list[str] = []
        self.forge.add_label.side_effect = lambda *a, **k: call_order.append("add")
        self.forge.remove_label.side_effect = lambda *a, **k: call_order.append(
            "remove"
        )

        handle_merge_failure(_task(), "CI failed", apply=True, forge=self.forge)

        self.forge.add_label.assert_called_once_with(1, "status:queued")
        self.forge.remove_label.assert_called_once_with(1, "status:done")
        assert call_order == ["add", "remove"]

    def test_add_label_failure_leaves_status_done_untouched(self):
        """Reproducer(境界1): add(status:queued)が一時障害で失敗した場合、
        status:doneのremoveには到達せず、Issueはstatus:doneのまま残る
        （次cycleのIntegratorが同じdoneタスクとして再検出できる）。"""
        self.forge.add_label.side_effect = RuntimeError("transient API failure")

        try:
            handle_merge_failure(_task(), "CI failed", apply=True, forge=self.forge)
        except RuntimeError:
            pass

        self.forge.remove_label.assert_not_called()
        self.forge.add_comment.assert_not_called()

    def test_remove_label_failure_after_add_succeeds_does_not_lose_status(self):
        """Reproducer(境界2): addが成功した直後にremove(status:done)が
        一時障害で失敗しても、Issueは既にstatus:queuedを持っているため
        （status:doneも一時的に残るが）全statusキューから脱落しない。"""
        self.forge.remove_label.side_effect = RuntimeError("transient API failure")

        try:
            handle_merge_failure(_task(), "CI failed", apply=True, forge=self.forge)
        except RuntimeError:
            pass

        self.forge.add_label.assert_called_once_with(1, "status:queued")
        self.forge.add_comment.assert_not_called()

    def test_success_posts_comment_with_reason(self):
        """通常のCI失敗requeue・コメント追加に回帰がないことを確認する。"""
        handle_merge_failure(
            _task(), "Merge conflict: boom", apply=True, forge=self.forge
        )

        self.forge.add_comment.assert_called_once()
        assert self.forge.add_comment.call_args[0][0] == 1
        assert "Merge conflict: boom" in self.forge.add_comment.call_args[0][1]

    def test_dry_run_does_not_touch_labels_or_comments(self):
        handle_merge_failure(_task(), "CI failed", apply=False, forge=self.forge)

        self.forge.add_label.assert_not_called()
        self.forge.remove_label.assert_not_called()
        self.forge.add_comment.assert_not_called()


class TestEnsureIntegrationPrIdentity(_FakeForgeTest):
    """#243: head名だけの照合で外部fork・別baseのPRを再利用しないことを検証する。"""

    def test_reuses_pr_with_verified_head_base_and_same_repo(self):
        self.forge.list_open_prs.return_value = [
            PrRecord(
                number=77,
                head_ref="integration/temp-parent-issue-100",
                changed_files=(),
                base_ref="parent/issue-100",
                is_cross_repository=False,
            )
        ]

        pr_number = ensure_integration_pr(
            "integration/temp-parent-issue-100",
            "origin/parent/issue-100",
            ["task-a"],
            forge=self.forge,
        )

        assert pr_number == 77
        self.forge.create_pull_request.assert_not_called()

    def test_reused_pr_is_updated_with_current_merged_tasks(self):
        # #375: 複数サイクルにまたがって同じPRへタスクが追加統合される場合、
        # 再利用時にタイトル・本文を最新のmerged_tasksへ同期する。
        self.forge.list_open_prs.return_value = [
            PrRecord(
                number=77,
                head_ref="integration/temp-parent-issue-100",
                changed_files=(),
                base_ref="parent/issue-100",
                is_cross_repository=False,
            )
        ]

        pr_number = ensure_integration_pr(
            "integration/temp-parent-issue-100",
            "origin/parent/issue-100",
            ["task-a", "task-b"],
            forge=self.forge,
        )

        assert pr_number == 77
        self.forge.create_pull_request.assert_not_called()
        self.forge.update_pull_request.assert_called_once()
        assert self.forge.update_pull_request.call_args.args[0] == 77
        assert (
            "task-a, task-b" in self.forge.update_pull_request.call_args.kwargs["title"]
        )
        assert (
            "task-a, task-b" in self.forge.update_pull_request.call_args.kwargs["body"]
        )

    def test_reuse_survives_update_failure(self):
        # タイトル・本文の更新自体が一時障害で失敗しても、有効なPRの再利用は
        # 諦めない（番号は返し、警告のみ出す）。
        self.forge.list_open_prs.return_value = [
            PrRecord(
                number=77,
                head_ref="integration/temp-parent-issue-100",
                changed_files=(),
                base_ref="parent/issue-100",
                is_cross_repository=False,
            )
        ]
        self.forge.update_pull_request.side_effect = RuntimeError(
            "transient API failure"
        )

        pr_number = ensure_integration_pr(
            "integration/temp-parent-issue-100",
            "origin/parent/issue-100",
            ["task-a"],
            forge=self.forge,
        )

        assert pr_number == 77
        self.forge.create_pull_request.assert_not_called()

    def test_does_not_reuse_cross_repository_pr_with_same_head_name(self):
        """Reproducer: 外部forkの同名headRefName PRを正規統合PRと誤認しない。"""
        self.forge.list_open_prs.return_value = [
            PrRecord(
                number=666,
                head_ref="integration/temp-parent-issue-100",
                changed_files=(),
                base_ref="parent/issue-100",
                is_cross_repository=True,
            )
        ]
        self.forge.create_pull_request.return_value = 78

        pr_number = ensure_integration_pr(
            "integration/temp-parent-issue-100",
            "origin/parent/issue-100",
            ["task-a"],
            forge=self.forge,
        )

        assert pr_number == 78
        self.forge.create_pull_request.assert_called_once()

    def test_does_not_reuse_pr_with_different_base(self):
        self.forge.list_open_prs.return_value = [
            PrRecord(
                number=666,
                head_ref="integration/temp-parent-issue-100",
                changed_files=(),
                base_ref="some/other-base",
                is_cross_repository=False,
            )
        ]
        self.forge.create_pull_request.return_value = 79

        pr_number = ensure_integration_pr(
            "integration/temp-parent-issue-100",
            "origin/parent/issue-100",
            ["task-a"],
            forge=self.forge,
        )

        assert pr_number == 79
        self.forge.create_pull_request.assert_called_once()

    def test_does_not_reuse_pr_with_unknown_identity(self):
        """identity不明（base_ref空・is_cross_repository不明）はfail closedで再利用しない。"""
        self.forge.list_open_prs.return_value = [
            PrRecord(
                number=666,
                head_ref="integration/temp-parent-issue-100",
                changed_files=(),
            )
        ]
        self.forge.create_pull_request.return_value = 80

        pr_number = ensure_integration_pr(
            "integration/temp-parent-issue-100",
            "origin/parent/issue-100",
            ["task-a"],
            forge=self.forge,
        )

        assert pr_number == 80
        self.forge.create_pull_request.assert_called_once()


class TestEnsureParentFinalPr(_FakeForgeTest):
    def test_creates_pr_from_parent_branch_to_main(self):
        self.forge.list_open_prs.return_value = []
        self.forge.create_pull_request.return_value = 555

        pr_number = ensure_parent_final_pr(100, forge=self.forge)

        assert pr_number == 555
        assert (
            self.forge.create_pull_request.call_args.kwargs["head"]
            == "parent/issue-100"
        )
        assert self.forge.create_pull_request.call_args.kwargs["base"] == "main"
        assert "100" in self.forge.create_pull_request.call_args.kwargs["body"]

    def test_reuses_existing_open_pr(self):
        self.forge.list_open_prs.return_value = [
            PrRecord(
                number=321,
                head_ref="parent/issue-100",
                changed_files=(),
                base_ref="main",
                is_cross_repository=False,
            )
        ]

        pr_number = ensure_parent_final_pr(100, forge=self.forge)

        assert pr_number == 321
        self.forge.create_pull_request.assert_not_called()

    def test_does_not_reuse_cross_repository_parent_pr(self):
        """#243: 外部forkの同名parentブランチPRを最終統合PRとして再利用しない。"""
        self.forge.list_open_prs.return_value = [
            PrRecord(
                number=666,
                head_ref="parent/issue-100",
                changed_files=(),
                base_ref="main",
                is_cross_repository=True,
            )
        ]
        self.forge.create_pull_request.return_value = 322

        pr_number = ensure_parent_final_pr(100, forge=self.forge)

        assert pr_number == 322
        self.forge.create_pull_request.assert_called_once()

    def test_pr_creation_failure_is_non_fatal(self):
        self.forge.list_open_prs.return_value = []
        self.forge.create_pull_request.side_effect = RuntimeError(
            "no commits between main and branch"
        )

        pr_number = ensure_parent_final_pr(100, forge=self.forge)

        assert pr_number is None
