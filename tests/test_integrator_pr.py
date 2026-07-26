from __future__ import annotations

from unittest.mock import patch

from orchestune.github import PrRecord
from orchestune.integrator_pr import ensure_integration_pr, ensure_parent_final_pr


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
