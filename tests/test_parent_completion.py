from __future__ import annotations

from unittest.mock import ANY, patch

from orchestune.github import IssueRecord
from orchestune.parent_completion import process_parent_completion


def _issue(number: int, state: str) -> IssueRecord:
    return IssueRecord(
        number=number,
        title=f"Issue {number}",
        body="",
        labels=(),
        created_at="2026-07-13T00:00:00Z",
        state=state,
    )


class TestProcessParentCompletion:
    def test_skips_when_parent_issue_number_is_none(self):
        res = process_parent_completion(None, apply=True)
        assert res == {"status": "skipped"}

    def test_skips_when_not_apply(self):
        res = process_parent_completion(100, apply=False)
        assert res == {"status": "skipped"}

    @patch("orchestune.parent_completion.github.is_branch_merged_into")
    @patch("orchestune.parent_completion.github.get_issue_state")
    @patch("orchestune.parent_completion.github.close_issue")
    def test_closes_parent_issue_when_final_pr_merged_and_still_open(
        self, mock_close, mock_get_state, mock_merged
    ):
        mock_merged.return_value = True
        mock_get_state.return_value = "OPEN"

        res = process_parent_completion(100, apply=True)

        mock_merged.assert_called_once_with("parent/issue-100", "main")
        mock_close.assert_called_once_with(100, "completed", comment=ANY)
        assert res == {"status": "parent_closed", "parent_issue_number": 100}

    @patch("orchestune.parent_completion.github.is_branch_merged_into")
    @patch("orchestune.parent_completion.github.get_issue_state")
    @patch("orchestune.parent_completion.github.close_issue")
    def test_does_not_double_close_already_closed_parent(
        self, mock_close, mock_get_state, mock_merged
    ):
        mock_merged.return_value = True
        mock_get_state.return_value = "CLOSED"

        res = process_parent_completion(100, apply=True)

        mock_close.assert_not_called()
        assert res == {"status": "already_closed"}

    @patch("orchestune.parent_completion.github.is_branch_merged_into")
    @patch("orchestune.parent_completion.github.list_sub_issues")
    @patch("orchestune.parent_completion.ensure_parent_final_pr")
    def test_creates_final_pr_once_all_children_are_closed(
        self, mock_ensure_pr, mock_list_sub_issues, mock_merged
    ):
        mock_merged.return_value = False
        mock_list_sub_issues.return_value = [
            _issue(101, "CLOSED"),
            _issue(102, "CLOSED"),
        ]
        mock_ensure_pr.return_value = 777

        res = process_parent_completion(100, apply=True)

        mock_ensure_pr.assert_called_once_with(100)
        assert res == {"status": "final_pr_ready", "pr_number": 777}

    @patch("orchestune.parent_completion.github.is_branch_merged_into")
    @patch("orchestune.parent_completion.github.list_sub_issues")
    @patch("orchestune.parent_completion.ensure_parent_final_pr")
    def test_waits_when_some_children_still_open(
        self, mock_ensure_pr, mock_list_sub_issues, mock_merged
    ):
        mock_merged.return_value = False
        mock_list_sub_issues.return_value = [
            _issue(101, "CLOSED"),
            _issue(102, "OPEN"),
        ]

        res = process_parent_completion(100, apply=True)

        mock_ensure_pr.assert_not_called()
        assert res == {"status": "waiting_on_children", "open_children": [102]}

    @patch("orchestune.parent_completion.github.is_branch_merged_into")
    @patch("orchestune.parent_completion.github.list_sub_issues")
    @patch("orchestune.parent_completion.ensure_parent_final_pr")
    def test_waits_when_parent_has_no_children_yet(
        self, mock_ensure_pr, mock_list_sub_issues, mock_merged
    ):
        mock_merged.return_value = False
        mock_list_sub_issues.return_value = []

        res = process_parent_completion(100, apply=True)

        mock_ensure_pr.assert_not_called()
        assert res == {"status": "waiting_on_children", "open_children": []}
