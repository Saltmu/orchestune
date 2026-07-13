from __future__ import annotations

from unittest.mock import patch

from orchestune.github import PrRecord
from orchestune.integrator_pr import ensure_parent_final_pr


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
            PrRecord(number=321, head_ref="parent/issue-100", changed_files=())
        ]

        pr_number = ensure_parent_final_pr(100)

        assert pr_number == 321
        mock_create_pr.assert_not_called()

    @patch("orchestune.integrator_pr.github.list_open_prs")
    @patch("orchestune.integrator_pr.github.create_pull_request")
    def test_pr_creation_failure_is_non_fatal(self, mock_create_pr, mock_open_prs):
        mock_open_prs.return_value = []
        mock_create_pr.side_effect = RuntimeError("no commits between main and branch")

        pr_number = ensure_parent_final_pr(100)

        assert pr_number is None
