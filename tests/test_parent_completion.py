from __future__ import annotations

import subprocess
from unittest.mock import ANY, MagicMock, patch

from orchestune.integrator.parent_completion import process_parent_completion
from orchestune.integrator.pr import ParentFinalPrMigrationError
from orchestune.models import IssueRecord


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

    def test_closes_parent_issue_when_branch_still_exists_and_tip_verified(
        self, fake_forge: MagicMock
    ):
        """branchが削除されずに残っており、現在のtipがmainへ含まれることを
        直接検証できる場合にcloseする（通常のクリーンな最終マージ後）。"""
        fake_forge.list_sub_issues.return_value = [_issue(101, "CLOSED")]
        fake_forge.get_merged_pr_timestamp.return_value = "2026-07-26T10:00:00Z"
        fake_forge.get_issue_last_reopened_at.return_value = None
        fake_forge.is_current_branch_tip_merged_into.return_value = True
        fake_forge.get_issue_state.return_value = "OPEN"

        res = process_parent_completion(100, apply=True, forge=fake_forge)

        fake_forge.get_merged_pr_timestamp.assert_called_once_with(
            "parent/issue-100", "main"
        )
        fake_forge.is_current_branch_tip_merged_into.assert_called_once_with(
            "parent/issue-100", "main"
        )
        fake_forge.close_issue.assert_called_once_with(100, "completed", comment=ANY)
        assert res == {"status": "parent_closed", "parent_issue_number": 100}

    def test_closes_parent_issue_when_branch_deleted_after_merge(
        self, fake_forge: MagicMock
    ):
        """branchが最終マージ後にクリーンアップ（削除）された正規のケースでは、
        現在tip検証が404で失敗し、branch_existsで真に不在だと確認できた
        場合にhistorical merged PR記録を信頼してcloseする。"""
        fake_forge.list_sub_issues.return_value = [_issue(101, "CLOSED")]
        fake_forge.get_merged_pr_timestamp.return_value = "2026-07-26T10:00:00Z"
        fake_forge.get_issue_last_reopened_at.return_value = None
        fake_forge.is_current_branch_tip_merged_into.side_effect = (
            subprocess.CalledProcessError(
                1, ["gh", "api"], stderr="gh: Branch not found (HTTP 404)"
            )
        )
        fake_forge.branch_exists.return_value = False
        fake_forge.get_issue_state.return_value = "OPEN"

        res = process_parent_completion(100, apply=True, forge=fake_forge)

        fake_forge.branch_exists.assert_called_once_with("parent/issue-100")
        fake_forge.close_issue.assert_called_once_with(100, "completed", comment=ANY)
        assert res == {"status": "parent_closed", "parent_issue_number": 100}

    def test_does_not_double_close_already_closed_parent(self, fake_forge: MagicMock):
        fake_forge.list_sub_issues.return_value = [_issue(101, "CLOSED")]
        fake_forge.get_merged_pr_timestamp.return_value = "2026-07-26T10:00:00Z"
        fake_forge.get_issue_last_reopened_at.return_value = None
        fake_forge.is_current_branch_tip_merged_into.return_value = True
        fake_forge.get_issue_state.return_value = "CLOSED"

        res = process_parent_completion(100, apply=True, forge=fake_forge)

        fake_forge.close_issue.assert_not_called()
        assert res == {"status": "already_closed"}

    @patch("orchestune.integrator.parent_completion.ensure_parent_final_pr")
    def test_creates_final_pr_once_all_children_are_closed(
        self, mock_ensure_pr, fake_forge: MagicMock
    ):
        fake_forge.get_merged_pr_timestamp.return_value = None
        fake_forge.list_sub_issues.return_value = [
            _issue(101, "CLOSED"),
            _issue(102, "CLOSED"),
        ]
        mock_ensure_pr.return_value = 777

        res = process_parent_completion(100, apply=True, forge=fake_forge)

        mock_ensure_pr.assert_called_once_with(100, forge=fake_forge, children=ANY)
        assert res == {"status": "final_pr_ready", "pr_number": 777}

    @patch("orchestune.integrator.parent_completion.ensure_parent_final_pr")
    def test_hands_the_discovered_children_to_the_final_pr(
        self, mock_ensure_pr, fake_forge: MagicMock
    ):
        """#681: 子Issue一覧テーブルの生成のために、既に取得済みの子Issueを
        そのまま引き渡す（`find_children_by_parent`の二重呼び出しを避ける）。"""
        children = [_issue(101, "CLOSED"), _issue(102, "CLOSED")]
        fake_forge.get_merged_pr_timestamp.return_value = None
        fake_forge.list_sub_issues.return_value = children
        mock_ensure_pr.return_value = 777

        process_parent_completion(100, apply=True, forge=fake_forge)

        assert mock_ensure_pr.call_args.kwargs["children"] == children

    @patch("orchestune.integrator.parent_completion.ensure_parent_final_pr")
    def test_waits_when_some_children_still_open(
        self, mock_ensure_pr, fake_forge: MagicMock
    ):
        fake_forge.get_merged_pr_timestamp.return_value = None
        fake_forge.list_sub_issues.return_value = [
            _issue(101, "CLOSED"),
            _issue(102, "OPEN"),
        ]

        res = process_parent_completion(100, apply=True, forge=fake_forge)

        mock_ensure_pr.assert_not_called()
        assert res == {"status": "waiting_on_children", "open_children": [102]}

    @patch("orchestune.integrator.parent_completion.ensure_parent_final_pr")
    @patch("orchestune.integrator.parent_completion.migrate_open_parent_final_pr")
    def test_migrates_legacy_final_pr_before_waiting_on_new_open_child(
        self, mock_migrate_legacy_pr, mock_ensure_pr, fake_forge: MagicMock
    ):
        """最終PR作成後に子Issueが追加された場合も、待機へ戻る前にlegacyの
        closing referenceを除去する。これにより次cycle前のマージ競合を残さない。"""
        fake_forge.list_sub_issues.return_value = [
            _issue(101, "CLOSED"),
            _issue(102, "OPEN"),
        ]
        mock_migrate_legacy_pr.return_value = 321

        res = process_parent_completion(100, apply=True, forge=fake_forge)

        mock_migrate_legacy_pr.assert_called_once_with(100, forge=fake_forge)
        mock_ensure_pr.assert_not_called()
        assert res == {"status": "waiting_on_children", "open_children": [102]}

    @patch("orchestune.integrator.parent_completion.migrate_open_parent_final_pr")
    def test_reports_unsafe_legacy_pr_before_waiting_on_open_child(
        self, mock_migrate_legacy_pr, fake_forge: MagicMock
    ):
        fake_forge.list_sub_issues.return_value = [_issue(101, "OPEN")]
        mock_migrate_legacy_pr.side_effect = ParentFinalPrMigrationError(
            321, RuntimeError("transient API failure")
        )

        res = process_parent_completion(100, apply=True, forge=fake_forge)

        assert res == {
            "status": "unsafe_final_pr",
            "pr_number": 321,
            "reason": "transient API failure",
        }

    @patch("orchestune.integrator.parent_completion.ensure_parent_final_pr")
    def test_waits_when_parent_has_no_children_yet(
        self, mock_ensure_pr, fake_forge: MagicMock
    ):
        fake_forge.get_merged_pr_timestamp.return_value = None
        fake_forge.list_sub_issues.return_value = []

        res = process_parent_completion(100, apply=True, forge=fake_forge)

        mock_ensure_pr.assert_not_called()
        assert res == {"status": "waiting_on_children", "open_children": []}

    @patch("orchestune.integrator.parent_completion.ensure_parent_final_pr")
    def test_reports_an_unsafe_legacy_final_pr_when_migration_fails(
        self, mock_ensure_pr, fake_forge: MagicMock
    ):
        """legacyのclosing referenceを外せない状態は、通常の待機として隠さず
        unsafeなPR番号と失敗理由をdispatch結果へ返す。"""
        fake_forge.list_sub_issues.return_value = [_issue(101, "CLOSED")]
        fake_forge.get_merged_pr_timestamp.return_value = None
        mock_ensure_pr.side_effect = ParentFinalPrMigrationError(
            321, RuntimeError("transient API failure")
        )

        res = process_parent_completion(100, apply=True, forge=fake_forge)

        assert res == {
            "status": "unsafe_final_pr",
            "pr_number": 321,
            "reason": "transient API failure",
        }

    def test_does_not_close_when_open_child_exists_despite_historical_merged_pr(
        self, fake_forge: MagicMock
    ):
        """Reproducer: 親Issueを再openし新しい子Issueを追加した場合、過去に
        同head/baseのmerged PRが存在していても即再closeしてはならない。"""
        fake_forge.list_sub_issues.return_value = [
            _issue(101, "CLOSED"),
            _issue(102, "OPEN"),
        ]
        fake_forge.get_merged_pr_timestamp.return_value = "2026-07-26T10:00:00Z"

        res = process_parent_completion(100, apply=True, forge=fake_forge)

        fake_forge.is_current_branch_tip_merged_into.assert_not_called()
        fake_forge.close_issue.assert_not_called()
        assert res == {"status": "waiting_on_children", "open_children": [102]}

    @patch("orchestune.integrator.parent_completion.ensure_parent_final_pr")
    def test_does_not_close_when_branch_has_new_unmerged_commit(
        self, mock_ensure_pr, fake_forge: MagicMock
    ):
        """Reproducer: 親Issueを再open後、子Issueを追加せずparent branchへ
        直接新commitを積んだ場合でも、過去のmerged PR記録だけでcloseせず、
        現在のtip SHA検証で未マージと判定する。"""
        fake_forge.list_sub_issues.return_value = [_issue(101, "CLOSED")]
        fake_forge.get_merged_pr_timestamp.return_value = "2026-07-26T10:00:00Z"
        fake_forge.get_issue_last_reopened_at.return_value = None
        fake_forge.is_current_branch_tip_merged_into.return_value = False
        mock_ensure_pr.return_value = 777

        res = process_parent_completion(100, apply=True, forge=fake_forge)

        fake_forge.close_issue.assert_not_called()
        mock_ensure_pr.assert_called_once_with(100, forge=fake_forge, children=ANY)
        assert res == {"status": "final_pr_ready", "pr_number": 777}

    def test_does_not_close_when_tip_verification_fails_for_unknown_reason(
        self, fake_forge: MagicMock
    ):
        """404以外の理由でtip検証自体が失敗した場合はfail closedとし、
        マージ未確認のまま次cycleへ持ち越す。"""
        fake_forge.list_sub_issues.return_value = [_issue(101, "CLOSED")]
        fake_forge.get_merged_pr_timestamp.return_value = "2026-07-26T10:00:00Z"
        fake_forge.get_issue_last_reopened_at.return_value = None
        fake_forge.is_current_branch_tip_merged_into.side_effect = (
            subprocess.CalledProcessError(
                1, ["gh", "api"], stderr="gh: rate limit exceeded"
            )
        )

        process_parent_completion(100, apply=True, forge=fake_forge)

        fake_forge.branch_exists.assert_not_called()
        fake_forge.close_issue.assert_not_called()

    @patch("orchestune.integrator.parent_completion.ensure_parent_final_pr")
    def test_does_not_close_when_reopened_after_last_merge(
        self, mock_ensure_pr, fake_forge: MagicMock
    ):
        """#276 P1 Reproducer: 親Issueが再openされた直後、まだ新しい子Issue
        や新commitが何も追加されていない状態でも、reopen後にマージされた
        ものが何もなければ即再closeしてはならない。"""
        fake_forge.list_sub_issues.return_value = [_issue(101, "CLOSED")]
        fake_forge.get_merged_pr_timestamp.return_value = "2026-07-26T10:00:00Z"
        fake_forge.get_issue_last_reopened_at.return_value = "2026-07-27T00:00:00Z"
        mock_ensure_pr.return_value = 777

        res = process_parent_completion(100, apply=True, forge=fake_forge)

        fake_forge.close_issue.assert_not_called()
        mock_ensure_pr.assert_called_once_with(100, forge=fake_forge, children=ANY)
        assert res == {"status": "final_pr_ready", "pr_number": 777}

    def test_closes_when_merge_happened_after_reopen(self, fake_forge: MagicMock):
        """reopen後に実際にマージされた場合は通常通りcloseする（回帰確認）。"""
        fake_forge.list_sub_issues.return_value = [_issue(101, "CLOSED")]
        fake_forge.get_issue_last_reopened_at.return_value = "2026-07-26T00:00:00Z"
        fake_forge.get_merged_pr_timestamp.return_value = "2026-07-27T00:00:00Z"
        fake_forge.is_current_branch_tip_merged_into.return_value = True
        fake_forge.get_issue_state.return_value = "OPEN"

        res = process_parent_completion(100, apply=True, forge=fake_forge)

        fake_forge.close_issue.assert_called_once_with(100, "completed", comment=ANY)
        assert res == {"status": "parent_closed", "parent_issue_number": 100}

    @patch("orchestune.integrator.parent_completion.ensure_parent_final_pr")
    def test_does_not_close_when_merged_at_equals_reopened_at(
        self, mock_ensure_pr, fake_forge: MagicMock
    ):
        """#276 P1 Reproducer(境界): GitHubのタイムスタンプは秒精度のため、
        最終マージによるcloseとその直後のreopenが同一秒に記録されうる。
        同値は「reopen後にマージされた」証拠にならないため、fail closed
        でcloseを見送らなければならない。"""
        fake_forge.list_sub_issues.return_value = [_issue(101, "CLOSED")]
        fake_forge.get_merged_pr_timestamp.return_value = "2026-07-27T00:00:00Z"
        fake_forge.get_issue_last_reopened_at.return_value = "2026-07-27T00:00:00Z"
        mock_ensure_pr.return_value = 777

        res = process_parent_completion(100, apply=True, forge=fake_forge)

        fake_forge.is_current_branch_tip_merged_into.assert_not_called()
        fake_forge.close_issue.assert_not_called()
        mock_ensure_pr.assert_called_once_with(100, forge=fake_forge, children=ANY)
        assert res == {"status": "final_pr_ready", "pr_number": 777}

    @patch("orchestune.integrator.parent_completion.ensure_parent_final_pr")
    def test_does_not_close_when_branch_still_exists_despite_tip_check_404(
        self, mock_ensure_pr, fake_forge: MagicMock
    ):
        """#276 P2 Reproducer: is_current_branch_tip_merged_intoが404で失敗
        しても、branch_existsで真に存在すると確認できた場合（compare呼び出し
        側の404など）はhistorical記録を信頼せず、closeしない。"""
        fake_forge.list_sub_issues.return_value = [_issue(101, "CLOSED")]
        fake_forge.get_merged_pr_timestamp.return_value = "2026-07-26T10:00:00Z"
        fake_forge.get_issue_last_reopened_at.return_value = None
        fake_forge.is_current_branch_tip_merged_into.side_effect = (
            subprocess.CalledProcessError(
                1, ["gh", "api"], stderr="gh: Not Found (HTTP 404)"
            )
        )
        fake_forge.branch_exists.return_value = True
        mock_ensure_pr.return_value = 777

        res = process_parent_completion(100, apply=True, forge=fake_forge)

        fake_forge.branch_exists.assert_called_once_with("parent/issue-100")
        fake_forge.close_issue.assert_not_called()
        mock_ensure_pr.assert_called_once_with(100, forge=fake_forge, children=ANY)
        assert res == {"status": "final_pr_ready", "pr_number": 777}

    @patch("orchestune.integrator.parent_completion.ensure_parent_final_pr")
    def test_does_not_close_when_branch_existence_check_itself_fails(
        self, mock_ensure_pr, fake_forge: MagicMock
    ):
        """branch存在確認自体が例外で失敗した場合もfail closedとする。"""
        fake_forge.list_sub_issues.return_value = [_issue(101, "CLOSED")]
        fake_forge.get_merged_pr_timestamp.return_value = "2026-07-26T10:00:00Z"
        fake_forge.get_issue_last_reopened_at.return_value = None
        fake_forge.is_current_branch_tip_merged_into.side_effect = (
            subprocess.CalledProcessError(
                1, ["gh", "api"], stderr="gh: Not Found (HTTP 404)"
            )
        )
        fake_forge.branch_exists.side_effect = RuntimeError("boom")
        mock_ensure_pr.return_value = 777

        res = process_parent_completion(100, apply=True, forge=fake_forge)

        fake_forge.close_issue.assert_not_called()
        mock_ensure_pr.assert_called_once_with(100, forge=fake_forge, children=ANY)
        assert res == {"status": "final_pr_ready", "pr_number": 777}


class TestProcessParentCompletionWithFakeForge:
    """#291: `mock.patch`によるグローバルなクラスメソッド差し替えではなく、
    `forge`引数への注入だけでテストが書けることを示す。"""

    def test_waits_on_open_children_using_injected_fake_forge(
        self, fake_forge: MagicMock
    ):
        fake_forge.list_sub_issues.return_value = [_issue(101, "OPEN")]

        res = process_parent_completion(100, apply=True, forge=fake_forge)

        assert res == {"status": "waiting_on_children", "open_children": [101]}
        fake_forge.list_sub_issues.assert_called_once_with(100)
        fake_forge.get_merged_pr_timestamp.assert_not_called()
        fake_forge.close_issue.assert_not_called()

    def test_sees_metadata_only_children_not_natively_linked(
        self, fake_forge: MagicMock
    ):
        """#485 review (P1): a child created without a native sub-issue
        link (relationship writes were unavailable) must still block
        completion, or the parent could be closed/final-PR'd while that
        child is still open."""
        fake_forge.list_sub_issues.return_value = []
        fake_forge.find_issues_by_parent_metadata.return_value = [
            IssueRecord(
                number=101,
                title="Issue 101",
                body="```yaml\nparent_issue_number: 100\n```\n",
                labels=(),
                created_at="2026-07-13T00:00:00Z",
                state="OPEN",
            )
        ]

        res = process_parent_completion(100, apply=True, forge=fake_forge)

        assert res == {"status": "waiting_on_children", "open_children": [101]}
