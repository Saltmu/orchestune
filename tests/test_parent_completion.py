from __future__ import annotations

import subprocess
from unittest.mock import ANY, MagicMock, patch

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

    @patch("orchestune.forge.GitHubForge.is_current_branch_tip_merged_into")
    @patch("orchestune.forge.GitHubForge.get_issue_last_reopened_at")
    @patch("orchestune.forge.GitHubForge.get_merged_pr_timestamp")
    @patch("orchestune.forge.GitHubForge.list_sub_issues")
    @patch("orchestune.forge.GitHubForge.get_issue_state")
    @patch("orchestune.forge.GitHubForge.close_issue")
    def test_closes_parent_issue_when_branch_still_exists_and_tip_verified(
        self,
        mock_close,
        mock_get_state,
        mock_list_sub_issues,
        mock_merged_at,
        mock_reopened_at,
        mock_tip,
    ):
        """branchが削除されずに残っており、現在のtipがmainへ含まれることを
        直接検証できる場合にcloseする（通常のクリーンな最終マージ後）。"""
        mock_list_sub_issues.return_value = [_issue(101, "CLOSED")]
        mock_merged_at.return_value = "2026-07-26T10:00:00Z"
        mock_reopened_at.return_value = None
        mock_tip.return_value = True
        mock_get_state.return_value = "OPEN"

        res = process_parent_completion(100, apply=True)

        mock_merged_at.assert_called_once_with("parent/issue-100", "main")
        mock_tip.assert_called_once_with("parent/issue-100", "main")
        mock_close.assert_called_once_with(100, "completed", comment=ANY)
        assert res == {"status": "parent_closed", "parent_issue_number": 100}

    @patch("orchestune.forge.GitHubForge.branch_exists")
    @patch("orchestune.forge.GitHubForge.is_current_branch_tip_merged_into")
    @patch("orchestune.forge.GitHubForge.get_issue_last_reopened_at")
    @patch("orchestune.forge.GitHubForge.get_merged_pr_timestamp")
    @patch("orchestune.forge.GitHubForge.list_sub_issues")
    @patch("orchestune.forge.GitHubForge.get_issue_state")
    @patch("orchestune.forge.GitHubForge.close_issue")
    def test_closes_parent_issue_when_branch_deleted_after_merge(
        self,
        mock_close,
        mock_get_state,
        mock_list_sub_issues,
        mock_merged_at,
        mock_reopened_at,
        mock_tip,
        mock_branch_exists,
    ):
        """branchが最終マージ後にクリーンアップ（削除）された正規のケースでは、
        現在tip検証が404で失敗し、branch_existsで真に不在だと確認できた
        場合にhistorical merged PR記録を信頼してcloseする。"""
        mock_list_sub_issues.return_value = [_issue(101, "CLOSED")]
        mock_merged_at.return_value = "2026-07-26T10:00:00Z"
        mock_reopened_at.return_value = None
        mock_tip.side_effect = subprocess.CalledProcessError(
            1, ["gh", "api"], stderr="gh: Branch not found (HTTP 404)"
        )
        mock_branch_exists.return_value = False
        mock_get_state.return_value = "OPEN"

        res = process_parent_completion(100, apply=True)

        mock_branch_exists.assert_called_once_with("parent/issue-100")
        mock_close.assert_called_once_with(100, "completed", comment=ANY)
        assert res == {"status": "parent_closed", "parent_issue_number": 100}

    @patch("orchestune.forge.GitHubForge.is_current_branch_tip_merged_into")
    @patch("orchestune.forge.GitHubForge.get_issue_last_reopened_at")
    @patch("orchestune.forge.GitHubForge.get_merged_pr_timestamp")
    @patch("orchestune.forge.GitHubForge.list_sub_issues")
    @patch("orchestune.forge.GitHubForge.get_issue_state")
    @patch("orchestune.forge.GitHubForge.close_issue")
    def test_does_not_double_close_already_closed_parent(
        self,
        mock_close,
        mock_get_state,
        mock_list_sub_issues,
        mock_merged_at,
        mock_reopened_at,
        mock_tip,
    ):
        mock_list_sub_issues.return_value = [_issue(101, "CLOSED")]
        mock_merged_at.return_value = "2026-07-26T10:00:00Z"
        mock_reopened_at.return_value = None
        mock_tip.return_value = True
        mock_get_state.return_value = "CLOSED"

        res = process_parent_completion(100, apply=True)

        mock_close.assert_not_called()
        assert res == {"status": "already_closed"}

    @patch("orchestune.forge.GitHubForge.get_merged_pr_timestamp")
    @patch("orchestune.forge.GitHubForge.list_sub_issues")
    @patch("orchestune.parent_completion.ensure_parent_final_pr")
    def test_creates_final_pr_once_all_children_are_closed(
        self, mock_ensure_pr, mock_list_sub_issues, mock_merged_at
    ):
        mock_merged_at.return_value = None
        mock_list_sub_issues.return_value = [
            _issue(101, "CLOSED"),
            _issue(102, "CLOSED"),
        ]
        mock_ensure_pr.return_value = 777

        res = process_parent_completion(100, apply=True)

        mock_ensure_pr.assert_called_once_with(100, forge=ANY)
        assert res == {"status": "final_pr_ready", "pr_number": 777}

    @patch("orchestune.forge.GitHubForge.get_merged_pr_timestamp")
    @patch("orchestune.forge.GitHubForge.list_sub_issues")
    @patch("orchestune.parent_completion.ensure_parent_final_pr")
    def test_waits_when_some_children_still_open(
        self, mock_ensure_pr, mock_list_sub_issues, mock_merged_at
    ):
        mock_merged_at.return_value = None
        mock_list_sub_issues.return_value = [
            _issue(101, "CLOSED"),
            _issue(102, "OPEN"),
        ]

        res = process_parent_completion(100, apply=True)

        mock_ensure_pr.assert_not_called()
        assert res == {"status": "waiting_on_children", "open_children": [102]}

    @patch("orchestune.forge.GitHubForge.get_merged_pr_timestamp")
    @patch("orchestune.forge.GitHubForge.list_sub_issues")
    @patch("orchestune.parent_completion.ensure_parent_final_pr")
    def test_waits_when_parent_has_no_children_yet(
        self, mock_ensure_pr, mock_list_sub_issues, mock_merged_at
    ):
        mock_merged_at.return_value = None
        mock_list_sub_issues.return_value = []

        res = process_parent_completion(100, apply=True)

        mock_ensure_pr.assert_not_called()
        assert res == {"status": "waiting_on_children", "open_children": []}

    @patch("orchestune.forge.GitHubForge.is_current_branch_tip_merged_into")
    @patch("orchestune.forge.GitHubForge.get_merged_pr_timestamp")
    @patch("orchestune.forge.GitHubForge.list_sub_issues")
    @patch("orchestune.forge.GitHubForge.close_issue")
    def test_does_not_close_when_open_child_exists_despite_historical_merged_pr(
        self, mock_close, mock_list_sub_issues, mock_merged_at, mock_tip
    ):
        """Reproducer: 親Issueを再openし新しい子Issueを追加した場合、過去に
        同head/baseのmerged PRが存在していても即再closeしてはならない。"""
        mock_list_sub_issues.return_value = [
            _issue(101, "CLOSED"),
            _issue(102, "OPEN"),
        ]
        mock_merged_at.return_value = "2026-07-26T10:00:00Z"

        res = process_parent_completion(100, apply=True)

        mock_tip.assert_not_called()
        mock_close.assert_not_called()
        assert res == {"status": "waiting_on_children", "open_children": [102]}

    @patch("orchestune.forge.GitHubForge.is_current_branch_tip_merged_into")
    @patch("orchestune.forge.GitHubForge.get_issue_last_reopened_at")
    @patch("orchestune.forge.GitHubForge.get_merged_pr_timestamp")
    @patch("orchestune.forge.GitHubForge.list_sub_issues")
    @patch("orchestune.parent_completion.ensure_parent_final_pr")
    @patch("orchestune.forge.GitHubForge.close_issue")
    def test_does_not_close_when_branch_has_new_unmerged_commit(
        self,
        mock_close,
        mock_ensure_pr,
        mock_list_sub_issues,
        mock_merged_at,
        mock_reopened_at,
        mock_tip,
    ):
        """Reproducer: 親Issueを再open後、子Issueを追加せずparent branchへ
        直接新commitを積んだ場合でも、過去のmerged PR記録だけでcloseせず、
        現在のtip SHA検証で未マージと判定する。"""
        mock_list_sub_issues.return_value = [_issue(101, "CLOSED")]
        mock_merged_at.return_value = "2026-07-26T10:00:00Z"
        mock_reopened_at.return_value = None
        mock_tip.return_value = False

        res = process_parent_completion(100, apply=True)

        mock_close.assert_not_called()
        mock_ensure_pr.assert_called_once_with(100, forge=ANY)
        assert res == {
            "status": "final_pr_ready",
            "pr_number": mock_ensure_pr.return_value,
        }

    @patch("orchestune.forge.GitHubForge.branch_exists")
    @patch("orchestune.forge.GitHubForge.is_current_branch_tip_merged_into")
    @patch("orchestune.forge.GitHubForge.get_issue_last_reopened_at")
    @patch("orchestune.forge.GitHubForge.get_merged_pr_timestamp")
    @patch("orchestune.forge.GitHubForge.list_sub_issues")
    @patch("orchestune.forge.GitHubForge.close_issue")
    def test_does_not_close_when_tip_verification_fails_for_unknown_reason(
        self,
        mock_close,
        mock_list_sub_issues,
        mock_merged_at,
        mock_reopened_at,
        mock_tip,
        mock_branch_exists,
    ):
        """404以外の理由でtip検証自体が失敗した場合はfail closedとし、
        マージ未確認のまま次cycleへ持ち越す。"""
        mock_list_sub_issues.return_value = [_issue(101, "CLOSED")]
        mock_merged_at.return_value = "2026-07-26T10:00:00Z"
        mock_reopened_at.return_value = None
        mock_tip.side_effect = subprocess.CalledProcessError(
            1, ["gh", "api"], stderr="gh: rate limit exceeded"
        )

        process_parent_completion(100, apply=True)

        mock_branch_exists.assert_not_called()
        mock_close.assert_not_called()

    @patch("orchestune.forge.GitHubForge.get_issue_last_reopened_at")
    @patch("orchestune.forge.GitHubForge.get_merged_pr_timestamp")
    @patch("orchestune.forge.GitHubForge.list_sub_issues")
    @patch("orchestune.parent_completion.ensure_parent_final_pr")
    @patch("orchestune.forge.GitHubForge.close_issue")
    def test_does_not_close_when_reopened_after_last_merge(
        self,
        mock_close,
        mock_ensure_pr,
        mock_list_sub_issues,
        mock_merged_at,
        mock_reopened_at,
    ):
        """#276 P1 Reproducer: 親Issueが再openされた直後、まだ新しい子Issue
        や新commitが何も追加されていない状態でも、reopen後にマージされた
        ものが何もなければ即再closeしてはならない。"""
        mock_list_sub_issues.return_value = [_issue(101, "CLOSED")]
        mock_merged_at.return_value = "2026-07-26T10:00:00Z"
        mock_reopened_at.return_value = "2026-07-27T00:00:00Z"

        res = process_parent_completion(100, apply=True)

        mock_close.assert_not_called()
        mock_ensure_pr.assert_called_once_with(100, forge=ANY)
        assert res == {
            "status": "final_pr_ready",
            "pr_number": mock_ensure_pr.return_value,
        }

    @patch("orchestune.forge.GitHubForge.is_current_branch_tip_merged_into")
    @patch("orchestune.forge.GitHubForge.get_issue_last_reopened_at")
    @patch("orchestune.forge.GitHubForge.get_merged_pr_timestamp")
    @patch("orchestune.forge.GitHubForge.list_sub_issues")
    @patch("orchestune.forge.GitHubForge.get_issue_state")
    @patch("orchestune.forge.GitHubForge.close_issue")
    def test_closes_when_merge_happened_after_reopen(
        self,
        mock_close,
        mock_get_state,
        mock_list_sub_issues,
        mock_merged_at,
        mock_reopened_at,
        mock_tip,
    ):
        """reopen後に実際にマージされた場合は通常通りcloseする（回帰確認）。"""
        mock_list_sub_issues.return_value = [_issue(101, "CLOSED")]
        mock_reopened_at.return_value = "2026-07-26T00:00:00Z"
        mock_merged_at.return_value = "2026-07-27T00:00:00Z"
        mock_tip.return_value = True
        mock_get_state.return_value = "OPEN"

        res = process_parent_completion(100, apply=True)

        mock_close.assert_called_once_with(100, "completed", comment=ANY)
        assert res == {"status": "parent_closed", "parent_issue_number": 100}

    @patch("orchestune.forge.GitHubForge.is_current_branch_tip_merged_into")
    @patch("orchestune.forge.GitHubForge.get_issue_last_reopened_at")
    @patch("orchestune.forge.GitHubForge.get_merged_pr_timestamp")
    @patch("orchestune.forge.GitHubForge.list_sub_issues")
    @patch("orchestune.parent_completion.ensure_parent_final_pr")
    @patch("orchestune.forge.GitHubForge.close_issue")
    def test_does_not_close_when_merged_at_equals_reopened_at(
        self,
        mock_close,
        mock_ensure_pr,
        mock_list_sub_issues,
        mock_merged_at,
        mock_reopened_at,
        mock_tip,
    ):
        """#276 P1 Reproducer(境界): GitHubのタイムスタンプは秒精度のため、
        最終マージによるcloseとその直後のreopenが同一秒に記録されうる。
        同値は「reopen後にマージされた」証拠にならないため、fail closed
        でcloseを見送らなければならない。"""
        mock_list_sub_issues.return_value = [_issue(101, "CLOSED")]
        mock_merged_at.return_value = "2026-07-27T00:00:00Z"
        mock_reopened_at.return_value = "2026-07-27T00:00:00Z"

        res = process_parent_completion(100, apply=True)

        mock_tip.assert_not_called()
        mock_close.assert_not_called()
        mock_ensure_pr.assert_called_once_with(100, forge=ANY)
        assert res == {
            "status": "final_pr_ready",
            "pr_number": mock_ensure_pr.return_value,
        }

    @patch("orchestune.forge.GitHubForge.branch_exists")
    @patch("orchestune.forge.GitHubForge.is_current_branch_tip_merged_into")
    @patch("orchestune.forge.GitHubForge.get_issue_last_reopened_at")
    @patch("orchestune.forge.GitHubForge.get_merged_pr_timestamp")
    @patch("orchestune.forge.GitHubForge.list_sub_issues")
    @patch("orchestune.parent_completion.ensure_parent_final_pr")
    @patch("orchestune.forge.GitHubForge.close_issue")
    def test_does_not_close_when_branch_still_exists_despite_tip_check_404(
        self,
        mock_close,
        mock_ensure_pr,
        mock_list_sub_issues,
        mock_merged_at,
        mock_reopened_at,
        mock_tip,
        mock_branch_exists,
    ):
        """#276 P2 Reproducer: is_current_branch_tip_merged_intoが404で失敗
        しても、branch_existsで真に存在すると確認できた場合（compare呼び出し
        側の404など）はhistorical記録を信頼せず、closeしない。"""
        mock_list_sub_issues.return_value = [_issue(101, "CLOSED")]
        mock_merged_at.return_value = "2026-07-26T10:00:00Z"
        mock_reopened_at.return_value = None
        mock_tip.side_effect = subprocess.CalledProcessError(
            1, ["gh", "api"], stderr="gh: Not Found (HTTP 404)"
        )
        mock_branch_exists.return_value = True

        res = process_parent_completion(100, apply=True)

        mock_branch_exists.assert_called_once_with("parent/issue-100")
        mock_close.assert_not_called()
        mock_ensure_pr.assert_called_once_with(100, forge=ANY)
        assert res == {
            "status": "final_pr_ready",
            "pr_number": mock_ensure_pr.return_value,
        }

    @patch("orchestune.forge.GitHubForge.branch_exists")
    @patch("orchestune.forge.GitHubForge.is_current_branch_tip_merged_into")
    @patch("orchestune.forge.GitHubForge.get_issue_last_reopened_at")
    @patch("orchestune.forge.GitHubForge.get_merged_pr_timestamp")
    @patch("orchestune.forge.GitHubForge.list_sub_issues")
    @patch("orchestune.parent_completion.ensure_parent_final_pr")
    @patch("orchestune.forge.GitHubForge.close_issue")
    def test_does_not_close_when_branch_existence_check_itself_fails(
        self,
        mock_close,
        mock_ensure_pr,
        mock_list_sub_issues,
        mock_merged_at,
        mock_reopened_at,
        mock_tip,
        mock_branch_exists,
    ):
        """branch存在確認自体が例外で失敗した場合もfail closedとする。"""
        mock_list_sub_issues.return_value = [_issue(101, "CLOSED")]
        mock_merged_at.return_value = "2026-07-26T10:00:00Z"
        mock_reopened_at.return_value = None
        mock_tip.side_effect = subprocess.CalledProcessError(
            1, ["gh", "api"], stderr="gh: Not Found (HTTP 404)"
        )
        mock_branch_exists.side_effect = RuntimeError("boom")

        res = process_parent_completion(100, apply=True)

        mock_close.assert_not_called()
        mock_ensure_pr.assert_called_once_with(100, forge=ANY)
        assert res == {
            "status": "final_pr_ready",
            "pr_number": mock_ensure_pr.return_value,
        }


class TestProcessParentCompletionWithFakeForge:
    """#291: `mock.patch`によるグローバルなクラスメソッド差し替えではなく、
    `forge`引数への注入だけでテストが書けることを示す。"""

    def test_waits_on_open_children_using_injected_fake_forge(self):
        fake_forge = MagicMock()
        fake_forge.list_sub_issues.return_value = [_issue(101, "OPEN")]

        res = process_parent_completion(100, apply=True, forge=fake_forge)

        assert res == {"status": "waiting_on_children", "open_children": [101]}
        fake_forge.list_sub_issues.assert_called_once_with(100)
        fake_forge.get_merged_pr_timestamp.assert_not_called()
        fake_forge.close_issue.assert_not_called()
