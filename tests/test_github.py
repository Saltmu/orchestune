import json
import subprocess
from unittest.mock import patch

import pytest

from orchestune.forge import GitHubForge
from orchestune.models import IssueRecord, PrRecord
from orchestune.validation import (
    validate_issue_number as _validate_issue_number,
)
from orchestune.validation import validate_label as _validate_label
from orchestune.validation import validate_ref_name as _validate_ref_name
from orchestune.validation import validate_username as _validate_username

_forge = GitHubForge()
add_comment = _forge.add_comment
add_label = _forge.add_label
branch_exists = _forge.branch_exists
close_issue = _forge.close_issue
create_pull_request = _forge.create_pull_request
get_actor_permission = _forge.get_actor_permission
get_issue_labels = _forge.get_issue_labels
get_issue_last_reopened_at = _forge.get_issue_last_reopened_at
get_issue_state = _forge.get_issue_state
get_label_actor = _forge.get_label_actor
get_merged_pr_timestamp = _forge.get_merged_pr_timestamp
is_branch_merged_into = _forge.is_branch_merged_into
is_current_branch_tip_merged_into = _forge.is_current_branch_tip_merged_into
list_issues_by_label = _forge.list_issues_by_label
list_open_prs = _forge.list_open_prs
list_prs = _forge.list_prs
list_sub_issues = _forge.list_sub_issues
merge_pull_request = _forge.merge_pull_request
remove_label = _forge.remove_label


class TestValidateIssueNumber:
    def test_accepts_positive_int(self):
        assert _validate_issue_number(184) == 184

    def test_accepts_numeric_string(self):
        assert _validate_issue_number("184") == 184

    def test_rejects_non_numeric(self):
        with pytest.raises(ValueError, match="issue番号"):
            _validate_issue_number("184; rm -rf /")

    def test_rejects_negative(self):
        with pytest.raises(ValueError, match="issue番号"):
            _validate_issue_number(-1)

    def test_rejects_zero(self):
        with pytest.raises(ValueError, match="issue番号"):
            _validate_issue_number(0)


class TestValidateLabel:
    def test_accepts_known_label_pattern(self):
        assert _validate_label("status:queued") == "status:queued"
        assert _validate_label("priority:high") == "priority:high"
        assert _validate_label("risk:flagged") == "risk:flagged"

    def test_rejects_shell_metacharacters(self):
        with pytest.raises(ValueError, match="ラベル"):
            _validate_label("status:queued; rm -rf /")

    def test_rejects_spaces(self):
        with pytest.raises(ValueError, match="ラベル"):
            _validate_label("status queued")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="ラベル"):
            _validate_label("")


class TestValidateRefName:
    def test_accepts_normal_branch_name(self):
        assert _validate_ref_name("claude/issue-184-dispatcher") == (
            "claude/issue-184-dispatcher"
        )

    def test_rejects_shell_metacharacters(self):
        with pytest.raises(ValueError, match="ブランチ名"):
            _validate_ref_name("foo`rm -rf /`")

    def test_rejects_leading_dash(self):
        with pytest.raises(ValueError, match="ブランチ名"):
            _validate_ref_name("--force")

    def test_rejects_double_dot(self):
        with pytest.raises(ValueError, match="ブランチ名"):
            _validate_ref_name("foo..bar")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="ブランチ名"):
            _validate_ref_name("")


class TestValidateUsername:
    def test_accepts_normal_username(self):
        assert _validate_username("Saltmu") == "Saltmu"

    def test_accepts_bot_username(self):
        assert _validate_username("dependabot[bot]") == "dependabot[bot]"

    def test_rejects_shell_metacharacters(self):
        with pytest.raises(ValueError, match="ユーザー名"):
            _validate_username("foo; rm -rf /")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="ユーザー名"):
            _validate_username("")


class TestListIssuesByLabel:
    def test_calls_gh_with_list_args_and_parses_json(self):
        payload = (
            '[{"number": 1, "title": "t", "body": "b", '
            '"labels": [{"name": "status:queued"}], "createdAt": "2026-01-01T00:00:00Z"}]'
        )
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=payload, stderr=""
            )
            result = list_issues_by_label("status:queued")

        called_args = mock_run.call_args.args[0]
        assert called_args[0] == "gh"
        assert "--label" in called_args
        assert "status:queued" in called_args
        assert mock_run.call_args.kwargs.get("shell", False) is False
        assert result == [
            IssueRecord(
                number=1,
                title="t",
                body="b",
                labels=("status:queued",),
                created_at="2026-01-01T00:00:00Z",
            )
        ]

    def test_rejects_invalid_label_before_calling_subprocess(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                list_issues_by_label("status:queued; evil")
            mock_run.assert_not_called()

    def test_defaults_to_open_state(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="[]", stderr=""
            )
            list_issues_by_label("status:done")
        called_args = mock_run.call_args.args[0]
        assert called_args[called_args.index("--state") + 1] == "open"

    def test_state_all_includes_closed_issues(self):
        """#236: closedなIssueもstatus:done判定に含められるよう、
        stateを明示的に指定できるようにする。"""
        payload = (
            '[{"number": 1, "title": "t", "body": "b", '
            '"labels": [{"name": "status:done"}], "createdAt": "2026-01-01T00:00:00Z"}]'
        )
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=payload, stderr=""
            )
            result = list_issues_by_label("status:done", state="all")
        called_args = mock_run.call_args.args[0]
        assert called_args[called_args.index("--state") + 1] == "all"
        assert result[0].number == 1

    def test_rejects_invalid_state(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                list_issues_by_label("status:done", state="bogus")
            mock_run.assert_not_called()

    def test_calls_gh_with_limit_arg(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="[]", stderr=""
            )
            list_issues_by_label("status:queued", limit=100)
        called_args = mock_run.call_args.args[0]
        assert "--limit" in called_args
        assert called_args[called_args.index("--limit") + 1] == "100"

    def test_defaults_to_limit_1000(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="[]", stderr=""
            )
            list_issues_by_label("status:queued")
        called_args = mock_run.call_args.args[0]
        assert "--limit" in called_args
        assert called_args[called_args.index("--limit") + 1] == "1000"


class TestListSubIssues:
    """#156: parent_issue_number指定時のfast path。gh api graphqlの
    subIssuesフィールド経由で親Issue配下の子Issueをまとめて取得する。"""

    def _page(self, nodes, has_next_page=False, end_cursor=None):
        import json

        return json.dumps(
            {
                "data": {
                    "repository": {
                        "issue": {
                            "subIssues": {
                                "pageInfo": {
                                    "hasNextPage": has_next_page,
                                    "endCursor": end_cursor,
                                },
                                "nodes": nodes,
                            }
                        }
                    }
                }
            }
        )

    def _node(self, **overrides):
        defaults = dict(
            number=1,
            title="t",
            body="b",
            state="OPEN",
            createdAt="2026-01-01T00:00:00Z",
            labels={"nodes": [{"name": "status:queued"}]},
            parent={"number": 100},
            blockedBy={"nodes": []},
        )
        defaults.update(overrides)
        return defaults

    def test_calls_gh_api_graphql_with_parent_number(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=self._page([]), stderr=""
            )
            list_sub_issues(100)

        called_args = mock_run.call_args.args[0]
        assert called_args[0:3] == ["gh", "api", "graphql"]
        assert "number=100" in called_args

    def test_parses_full_issue_record_fields(self):
        node = self._node(
            number=1,
            title="task-a",
            body="body text",
            state="OPEN",
            createdAt="2026-01-01T00:00:00Z",
            labels={"nodes": [{"name": "status:queued"}]},
            parent={"number": 100},
            blockedBy={"nodes": [{"number": 5}]},
        )
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=self._page([node]), stderr=""
            )
            result = list_sub_issues(100)

        assert result == [
            IssueRecord(
                number=1,
                title="task-a",
                body="body text",
                labels=("status:queued",),
                created_at="2026-01-01T00:00:00Z",
                state="OPEN",
                parent={"number": 100},
                blocked_by=(5,),
            )
        ]

    def test_paginates_until_no_next_page(self):
        node_1 = self._node(number=1)
        node_2 = self._node(number=2)
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=self._page(
                        [node_1], has_next_page=True, end_cursor="cursor-1"
                    ),
                    stderr="",
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=self._page([node_2]), stderr=""
                ),
            ]
            result = list_sub_issues(100)

        assert [r.number for r in result] == [1, 2]
        assert mock_run.call_count == 2
        second_call_args = mock_run.call_args_list[1].args[0]
        assert "after=cursor-1" in second_call_args

    def test_rejects_invalid_parent_issue_number_before_calling_subprocess(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                list_sub_issues(-1)
            mock_run.assert_not_called()


class TestAddRemoveLabel:
    def test_add_label_calls_gh_issue_edit(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            add_label(184, "status:in-progress")
        called_args = mock_run.call_args.args[0]
        assert called_args == [
            "gh",
            "issue",
            "edit",
            "184",
            "--add-label",
            "status:in-progress",
        ]

    def test_remove_label_calls_gh_issue_edit(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            remove_label(184, "status:queued")
        called_args = mock_run.call_args.args[0]
        assert called_args == [
            "gh",
            "issue",
            "edit",
            "184",
            "--remove-label",
            "status:queued",
        ]

    def test_add_label_rejects_invalid_issue_number(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                add_label("184 && evil", "status:queued")
            mock_run.assert_not_called()


class TestAddComment:
    def test_passes_body_via_stdin_not_argv(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            add_comment(184, "some body with `backticks` and $(dangerous)")
        called_args = mock_run.call_args.args[0]
        assert called_args == ["gh", "issue", "comment", "184", "--body-file", "-"]
        assert (
            mock_run.call_args.kwargs.get("input")
            == "some body with `backticks` and $(dangerous)"
        )


class TestIsBranchMergedInto:
    def test_returns_true_when_exact_head_base_pr_was_merged(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout='[{"number":264}]', stderr=""
            )

            assert is_branch_merged_into("parent/issue-100", "main") is True

        assert mock_run.call_args.args[0] == [
            "gh",
            "pr",
            "list",
            "--state",
            "merged",
            "--head",
            "parent/issue-100",
            "--base",
            "main",
            "--json",
            "number",
            "--limit",
            "1",
        ]

    def test_returns_false_when_exact_head_base_pr_was_not_merged(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="[]", stderr=""
            )

            assert is_branch_merged_into("parent/issue-100", "main") is False


class TestIsCurrentBranchTipMergedInto:
    def test_returns_true_when_current_remote_tip_is_in_base(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=f"{'a' * 40}\n", stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="ahead\n", stderr=""
                ),
            ]

            result = is_current_branch_tip_merged_into("claude/issue-1-task-1", "main")

        assert result is True
        assert mock_run.call_args_list[0].args[0] == [
            "gh",
            "api",
            "repos/{owner}/{repo}/branches/claude%2Fissue-1-task-1",
            "--jq",
            ".commit.sha",
        ]
        assert mock_run.call_args_list[1].args[0] == [
            "gh",
            "api",
            f"repos/{{owner}}/{{repo}}/compare/{'a' * 40}...main",
            "--jq",
            ".status",
        ]

    @pytest.mark.parametrize("status", ["behind", "diverged"])
    def test_returns_false_when_current_tip_is_not_in_base(self, status):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=f"{'b' * 40}\n", stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=f"{status}\n", stderr=""
                ),
            ]

            assert (
                is_current_branch_tip_merged_into("claude/issue-1-task-1", "main")
                is False
            )

    @pytest.mark.parametrize("head,base", [("--evil", "main"), ("feat/x", "bad..base")])
    def test_rejects_invalid_refs(self, head, base):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                is_current_branch_tip_merged_into(head, base)
            mock_run.assert_not_called()


class TestGetMergedPrTimestamp:
    def test_returns_merged_at_timestamp(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='[{"mergedAt":"2026-07-26T10:00:00Z"}]',
                stderr="",
            )

            result = get_merged_pr_timestamp("parent/issue-100", "main")

        assert result == "2026-07-26T10:00:00Z"
        assert mock_run.call_args.args[0] == [
            "gh",
            "pr",
            "list",
            "--state",
            "merged",
            "--head",
            "parent/issue-100",
            "--base",
            "main",
            "--json",
            "mergedAt",
            "--limit",
            "1000",
        ]

    def test_returns_max_merged_at_when_multiple_merged_prs_exist(self):
        """#276レビュー対応 Reproducer: gh pr listはmergedAt順を保証しない
        ため、複数のmerged PR記録がある場合は最大（最新）のmergedAtを
        選ばなければならない。並び順がバラバラでも正しく最大値を選ぶ。"""
        payload = (
            "["
            '{"mergedAt":"2026-07-20T10:00:00Z"},'
            '{"mergedAt":"2026-07-27T10:00:00Z"},'
            '{"mergedAt":"2026-07-15T10:00:00Z"}'
            "]"
        )
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=payload, stderr=""
            )

            result = get_merged_pr_timestamp("parent/issue-100", "main")

        assert result == "2026-07-27T10:00:00Z"

    def test_returns_none_when_no_merged_pr(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="[]", stderr=""
            )

            assert get_merged_pr_timestamp("parent/issue-100", "main") is None


class TestGetIssueLastReopenedAt:
    def test_returns_latest_reopened_event_timestamp(self):
        payload = (
            '[[{"event":"closed","created_at":"2026-07-20T00:00:00Z"},'
            '{"event":"reopened","created_at":"2026-07-21T00:00:00Z"},'
            '{"event":"reopened","created_at":"2026-07-25T00:00:00Z"}]]'
        )
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=payload, stderr=""
            )

            result = get_issue_last_reopened_at(100)

        assert result == "2026-07-25T00:00:00Z"

    def test_returns_none_when_never_reopened(self):
        payload = '[[{"event":"closed","created_at":"2026-07-20T00:00:00Z"}]]'
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=payload, stderr=""
            )

            assert get_issue_last_reopened_at(100) is None


class TestBranchExists:
    def test_returns_true_when_branch_lookup_succeeds(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout='{"name":"parent/issue-100"}', stderr=""
            )

            assert branch_exists("parent/issue-100") is True

    def test_returns_false_on_404(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, ["gh", "api"], stderr="gh: Branch not found (HTTP 404)"
            )

            assert branch_exists("parent/issue-100") is False

    def test_propagates_non_404_failures(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, ["gh", "api"], stderr="gh: rate limit exceeded"
            )

            with pytest.raises(subprocess.CalledProcessError):
                branch_exists("parent/issue-100")


class TestListOpenPrs:
    def test_list_prs_can_include_all_pr_states(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="[]", stderr=""
            )

            assert list_prs(state="all") == []

        called_args = mock_run.call_args.args[0]
        assert called_args[called_args.index("--state") + 1] == "all"
        assert "state" in called_args[called_args.index("--json") + 1]

    def test_list_prs_records_each_pr_state(self):
        payload = (
            '[{"number": 5, "headRefName": "feat/x", "state": "MERGED"},'
            '{"number": 6, "headRefName": "feat/y", "state": "CLOSED"}]'
        )
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=payload, stderr=""
            )

            prs = list_prs(state="all")

        assert [pr.state for pr in prs] == ["MERGED", "CLOSED"]

    def test_list_prs_records_closed_at(self):
        payload = (
            '[{"number": 6, "headRefName": "feat/y", "state": "CLOSED", '
            '"createdAt": "2026-01-01T00:00:00Z", '
            '"closedAt": "2026-01-03T00:00:00Z"}]'
        )
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=payload, stderr=""
            )

            prs = list_prs(state="all")
            called_args = mock_run.call_args.args[0]

        assert prs[0].closed_at == "2026-01-03T00:00:00Z"
        assert "closedAt" in called_args[called_args.index("--json") + 1]

    def test_list_prs_rejects_unsupported_state(self):
        with pytest.raises(ValueError, match="Unsupported PR state"):
            list_prs(state="draft")

    def test_fetches_pr_list_and_per_pr_files(self):
        list_payload = (
            "["
            '{"number": 5, "headRefName": "feat/x", "reviewDecision": "APPROVED", "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}], "files": [{"path": "src/a.py"}, {"path": "src/b.py"}], "closingIssuesReferences": []},'
            '{"number": 6, "headRefName": "feat/y", "reviewDecision": "CHANGES_REQUESTED", "statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": null}], "files": [], "closingIssuesReferences": []}'
            "]"
        )

        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=list_payload, stderr=""
            )
            prs = list_open_prs()
            called_args = mock_run.call_args.args[0]

        assert "files" in called_args[called_args.index("--json") + 1]
        assert "closingIssuesReferences" in called_args[called_args.index("--json") + 1]
        assert prs == [
            PrRecord(
                number=5,
                head_ref="feat/x",
                changed_files=("src/a.py", "src/b.py"),
                review_decision="APPROVED",
                is_ci_passing=True,
            ),
            PrRecord(
                number=6,
                head_ref="feat/y",
                changed_files=(),
                review_decision="CHANGES_REQUESTED",
                is_ci_passing=False,
            ),
        ]

    def test_accepts_nested_status_check_rollup_contexts(self):
        list_payload = (
            "["
            '{"number": 5, "headRefName": "feat/x", "reviewDecision": "APPROVED", '
            '"statusCheckRollup": {"contexts": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]}},'
            '{"number": 6, "headRefName": "feat/y", "reviewDecision": "APPROVED", '
            '"statusCheckRollup": {"contexts": [{"status": "QUEUED", "conclusion": null}]}}'
            "]"
        )
        files_payload = '{"files": []}'

        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=list_payload, stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=files_payload, stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=files_payload, stderr=""
                ),
            ]
            prs = list_open_prs()

        assert prs[0].is_ci_passing is True
        assert prs[1].is_ci_passing is False

    @pytest.mark.parametrize(
        "status_check_rollup",
        [
            "[]",
            '{"contexts": []}',
            "null",
        ],
    )
    def test_treats_absent_or_empty_status_checks_as_not_passing(
        self, status_check_rollup
    ):
        list_payload = (
            "["
            '{"number": 5, "headRefName": "feat/x", '
            f'"statusCheckRollup": {status_check_rollup}}}'
            "]"
        )

        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=list_payload, stderr=""
            )
            prs = list_open_prs()

        assert prs[0].is_ci_passing is False

    def test_legacy_status_context_success_counts_as_passing(self):
        """#256 Reproducer: statusCheckRollupはCheckRunとlegacy StatusContext
        のunionだが、全要素へCheckRun用のstatus/conclusionだけを適用すると
        StatusContextは常にfailing扱いになる。SUCCESSなStatusContextは
        passingとして扱わなければならない。"""
        list_payload = (
            "["
            '{"number": 5, "headRefName": "feat/x", '
            '"statusCheckRollup": [{"__typename": "StatusContext", "state": "SUCCESS"}]}'
            "]"
        )

        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=list_payload, stderr=""
            )
            prs = list_open_prs()

        assert prs[0].is_ci_passing is True

    @pytest.mark.parametrize("state", ["PENDING", "ERROR", "FAILURE"])
    def test_legacy_status_context_non_success_states_are_not_passing(self, state):
        list_payload = (
            "["
            '{"number": 5, "headRefName": "feat/x", '
            f'"statusCheckRollup": [{{"__typename": "StatusContext", "state": "{state}"}}]}}'
            "]"
        )

        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=list_payload, stderr=""
            )
            prs = list_open_prs()

        assert prs[0].is_ci_passing is False

    def test_mixed_check_run_and_status_context_all_passing(self):
        """CheckRunとStatusContextが混在するrollupで、両方passingならTrue。"""
        list_payload = (
            "["
            '{"number": 5, "headRefName": "feat/x", "statusCheckRollup": ['
            '{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},'
            '{"__typename": "StatusContext", "state": "SUCCESS"}'
            "]}"
            "]"
        )

        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=list_payload, stderr=""
            )
            prs = list_open_prs()

        assert prs[0].is_ci_passing is True

    def test_mixed_check_run_and_status_context_one_failing(self):
        """混在rollupで、片方でもfailingならis_ci_passingはFalseのまま。"""
        list_payload = (
            "["
            '{"number": 5, "headRefName": "feat/x", "statusCheckRollup": ['
            '{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},'
            '{"__typename": "StatusContext", "state": "PENDING"}'
            "]}"
            "]"
        )

        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=list_payload, stderr=""
            )
            prs = list_open_prs()

        assert prs[0].is_ci_passing is False

    def test_status_context_without_typename_is_detected_by_field_shape(self):
        """__typenameが欠落していても、statusやconclusionを持たずstateのみ
        持つ要素はStatusContextとしてshapeで判定できる（gh CLIバージョン差異
        への耐性）。"""
        list_payload = (
            "["
            '{"number": 5, "headRefName": "feat/x", '
            '"statusCheckRollup": [{"state": "SUCCESS"}]}'
            "]"
        )

        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=list_payload, stderr=""
            )
            prs = list_open_prs()

        assert prs[0].is_ci_passing is True

    def test_includes_closing_issue_references(self):
        """#239: ブランチ名がAIセッションの指示通りにならない場合でも
        自己PR判定できるよう、PRが閉じるIssue番号一覧も取得する。"""
        list_payload = (
            "["
            '{"number": 5, "headRefName": "claude/elegant-noether-5rli7u", '
            '"files": [{"path": "src/a.py"}], '
            '"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}], '
            '"closingIssuesReferences": [{"number": 218}]}'
            "]"
        )

        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=list_payload, stderr=""
            )
            prs = list_open_prs()
            called_args = mock_run.call_args.args[0]

        assert "closingIssuesReferences" in called_args[called_args.index("--json") + 1]
        assert prs == [
            PrRecord(
                number=5,
                head_ref="claude/elegant-noether-5rli7u",
                changed_files=("src/a.py",),
                closes_issue_numbers=(218,),
            )
        ]

    def test_records_base_ref_and_head_repository_identity(self):
        """#243: 外部fork・別baseの同名branch PRを識別できるよう、
        base refとhead repositoryのidentityを取得・保持する。"""
        list_payload = (
            "["
            '{"number": 5, "headRefName": "integration/temp-parent-issue-100", '
            '"baseRefName": "parent/issue-100", "isCrossRepository": false, '
            '"files": []},'
            '{"number": 6, "headRefName": "integration/temp-parent-issue-100", '
            '"baseRefName": "main", "isCrossRepository": true, '
            '"files": []}'
            "]"
        )

        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=list_payload, stderr=""
            )
            prs = list_open_prs()
            called_args = mock_run.call_args.args[0]

        json_fields = called_args[called_args.index("--json") + 1]
        assert "baseRefName" in json_fields
        assert "isCrossRepository" in json_fields
        assert prs[0].base_ref == "parent/issue-100"
        assert prs[0].is_cross_repository is False
        assert prs[1].base_ref == "main"
        assert prs[1].is_cross_repository is True

    def test_missing_identity_fields_default_to_unknown(self):
        """#243: identityフィールドが取得できないPRは「不明」として保持し、
        呼び出し側がfail closedに判定できるようにする。"""
        list_payload = '[{"number": 5, "headRefName": "feat/x", "files": []}]'

        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=list_payload, stderr=""
            )
            prs = list_open_prs()

        assert prs[0].base_ref == ""
        assert prs[0].is_cross_repository is None

    def test_closes_issue_numbers_defaults_to_empty_tuple(self):
        list_payload = '[{"number": 5, "headRefName": "feat/x", "files": []}]'

        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=list_payload, stderr=""
            )
            prs = list_open_prs()

        assert prs[0].closes_issue_numbers == ()

    def test_list_prs_paginates_files_when_100_or_more(self):
        """100件以上のchanged filesを返すPRに対し、GraphQL APIで全件取得する。"""
        files = [{"path": f"file{i}.py"} for i in range(1, 101)]
        list_payload = (
            "["
            f'{{"number": 10, "headRefName": "feat/large", "files": {json.dumps(files)}, "closingIssuesReferences": []}}'
            "]"
        )
        graphql_payload = (
            '{"data": {"repository": {"pullRequest": {"files": {'
            '"pageInfo": {"hasNextPage": false, "endCursor": null}, '
            f'"nodes": {[{"path": f"file{i}.py"} for i in range(1, 102)]}'
            "}}}}}".replace("'", '"')
        )

        def mock_run_side_effect(args, **kwargs):
            if args[0] == "gh" and args[1] == "pr" and args[2] == "list":
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=list_payload, stderr=""
                )
            if args[0] == "gh" and args[1] == "api" and args[2] == "graphql":
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=graphql_payload, stderr=""
                )
            raise ValueError(f"Unexpected command: {args}")

        with patch("orchestune.forge.subprocess.run", side_effect=mock_run_side_effect):
            prs = list_open_prs(paginate_files=True)

        assert len(prs) == 1
        assert len(prs[0].changed_files) == 101

    def test_list_prs_skips_pagination_when_paginate_files_false(self):
        """paginate_files=False (デフォルト) の場合、100件以上のchanged filesがあってもGraphQLページネーションを実行しない。"""
        files = [{"path": f"file{i}.py"} for i in range(1, 101)]
        list_payload = (
            "["
            f'{{"number": 10, "headRefName": "feat/large", "files": {json.dumps(files)}, "closingIssuesReferences": []}}'
            "]"
        )

        def mock_run_side_effect(args, **kwargs):
            if args[0] == "gh" and args[1] == "pr" and args[2] == "list":
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=list_payload, stderr=""
                )
            raise ValueError(f"Unexpected command: {args}")

        with patch("orchestune.forge.subprocess.run", side_effect=mock_run_side_effect):
            prs = list_open_prs()

        assert len(prs) == 1
        assert len(prs[0].changed_files) == 100
        assert prs[0].is_files_truncated is False

    def test_list_prs_skips_pagination_for_non_open_state(self):
        """state != 'open' の場合、paginate_files=TrueでもGraphQLページネーションを実行しない。"""
        files = [{"path": f"file{i}.py"} for i in range(1, 101)]
        list_payload = (
            "["
            f'{{"number": 10, "headRefName": "feat/large", "files": {json.dumps(files)}, "closingIssuesReferences": []}}'
            "]"
        )

        def mock_run_side_effect(args, **kwargs):
            if args[0] == "gh" and args[1] == "pr" and args[2] == "list":
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=list_payload, stderr=""
                )
            raise ValueError(f"Unexpected command: {args}")

        with patch("orchestune.forge.subprocess.run", side_effect=mock_run_side_effect):
            prs = list_prs(state="closed", paginate_files=True)

        assert len(prs) == 1
        assert len(prs[0].changed_files) == 100
        assert prs[0].is_files_truncated is False

    def test_list_prs_sets_is_files_truncated_true_on_graphql_failure(self):
        """100件のchanged filesを持つPR的GraphQL全件取得が失敗した場合、is_files_truncated=Trueとする。"""
        files = [{"path": f"file{i}.py"} for i in range(1, 101)]
        list_payload = (
            "["
            f'{{"number": 10, "headRefName": "feat/large", "files": {json.dumps(files)}, "closingIssuesReferences": []}}'
            "]"
        )

        def mock_run_side_effect(args, **kwargs):
            if args[0] == "gh" and args[1] == "pr" and args[2] == "list":
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=list_payload, stderr=""
                )
            if args[0] == "gh" and args[1] == "api" and args[2] == "graphql":
                raise subprocess.CalledProcessError(1, args, stderr="GraphQL error")
            raise ValueError(f"Unexpected command: {args}")

        with patch("orchestune.forge.subprocess.run", side_effect=mock_run_side_effect):
            prs = list_open_prs(paginate_files=True)

        assert len(prs) == 1
        assert prs[0].is_files_truncated is True

    def test_calls_gh_with_limit_arg(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="[]", stderr=""
            )
            list_open_prs(limit=50)
        called_args = mock_run.call_args.args[0]
        assert "--limit" in called_args
        assert called_args[called_args.index("--limit") + 1] == "50"

    def test_defaults_to_limit_1000(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="[]", stderr=""
            )
            list_open_prs()
        called_args = mock_run.call_args.args[0]
        assert "--limit" in called_args
        assert called_args[called_args.index("--limit") + 1] == "1000"


class TestGetIssueLabels:
    def test_returns_label_names(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='{"labels": [{"name": "semantic-review:passed"}, {"name": "status:done"}]}',
            )
            labels = get_issue_labels(181)
        assert labels == ("semantic-review:passed", "status:done")

    def test_rejects_invalid_issue_number(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                get_issue_labels("181; rm -rf /")
            mock_run.assert_not_called()


class TestGetLabelActor:
    """#119: `status:queued`ラベルを実際に付与したユーザーを特定する。"""

    def test_returns_actor_of_matching_labeled_event(self):
        events_payload = (
            '[[{"event": "labeled", "actor": {"login": "alice"}, '
            '"label": {"name": "status:queued"}}]]'
        )
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=events_payload, stderr=""
            )
            actor = get_label_actor(184, "status:queued")
        assert actor == "alice"
        called_args = mock_run.call_args.args[0]
        assert called_args == [
            "gh",
            "api",
            "repos/{owner}/{repo}/issues/184/events",
            "--paginate",
            "--slurp",
        ]

    def test_ignores_labeled_events_for_other_labels(self):
        events_payload = (
            '[[{"event": "labeled", "actor": {"login": "bob"}, '
            '"label": {"name": "bug"}}]]'
        )
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=events_payload, stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout='{"author": {"login": "carol"}}',
                    stderr="",
                ),
            ]
            actor = get_label_actor(184, "status:queued")
        assert actor == "carol"

    def test_falls_back_to_issue_author_when_no_labeled_event(self):
        """Issue作成時(`gh issue create --label`)に付与されたラベルは
        `labeled`イベントを残さないため、Issue作成者にフォールバックする。"""
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="[[]]", stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout='{"author": {"login": "dave"}}',
                    stderr="",
                ),
            ]
            actor = get_label_actor(184, "status:queued")
        assert actor == "dave"
        second_call_args = mock_run.call_args_list[1].args[0]
        assert second_call_args == [
            "gh",
            "issue",
            "view",
            "184",
            "--json",
            "author",
        ]

    def test_takes_most_recent_matching_event_across_pages(self):
        events_payload = (
            "["
            '[{"event": "labeled", "actor": {"login": "alice"}, '
            '"label": {"name": "status:queued"}}],'
            '[{"event": "labeled", "actor": {"login": "mallory"}, '
            '"label": {"name": "status:queued"}}]'
            "]"
        )
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=events_payload, stderr=""
            )
            actor = get_label_actor(184, "status:queued")
        assert actor == "mallory"

    def test_rejects_invalid_issue_number(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                get_label_actor("184; rm -rf /", "status:queued")
            mock_run.assert_not_called()

    def test_rejects_invalid_label(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                get_label_actor(184, "status:queued; evil")
            mock_run.assert_not_called()


class TestGetActorPermission:
    """#119: actorのリポジトリ権限をGitHub APIから取得する。"""

    def test_returns_permission_field(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout='{"permission": "write"}', stderr=""
            )
            permission = get_actor_permission("alice")
        assert permission == "write"
        called_args = mock_run.call_args.args[0]
        assert called_args == [
            "gh",
            "api",
            "repos/{owner}/{repo}/collaborators/alice/permission",
        ]

    def test_treats_api_error_as_none(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, ["gh", "api", "..."]
            )
            permission = get_actor_permission("mallory")
        assert permission == "none"

    def test_rejects_invalid_username(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                get_actor_permission("alice; rm -rf /")
            mock_run.assert_not_called()

    def test_treats_empty_username_as_none_without_calling_subprocess(self):
        """#208: get_label_actorがghostユーザー等で空文字を返した場合、
        ValueErrorを送出せず安全側の`none`を返す。"""
        with patch("orchestune.forge.subprocess.run") as mock_run:
            permission = get_actor_permission("")
        assert permission == "none"
        mock_run.assert_not_called()


class TestCloseIssue:
    def test_closes_with_reason(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            close_issue(280, "not planned")
        called_args = mock_run.call_args.args[0]
        assert called_args == [
            "gh",
            "issue",
            "close",
            "280",
            "--reason",
            "not planned",
        ]

    def test_closes_with_comment(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            close_issue(
                280, "not planned", comment="既に実装済みのため対応不要でした。"
            )
        called_args = mock_run.call_args.args[0]
        assert called_args == [
            "gh",
            "issue",
            "close",
            "280",
            "--reason",
            "not planned",
            "--comment",
            "既に実装済みのため対応不要でした。",
        ]

    def test_rejects_invalid_reason(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                close_issue(280, "evil; rm -rf /")
            mock_run.assert_not_called()

    def test_rejects_invalid_issue_number(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                close_issue("280; rm -rf /", "not planned")
            mock_run.assert_not_called()


class TestCreatePullRequest:
    def test_creates_pr_and_returns_number_parsed_from_url(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="https://github.com/Saltmu/manuscriptune/pull/315\n",
            )
            number = create_pull_request(
                head="integration/temp-main",
                base="main",
                title="Integrate completed tasks",
                body="body text",
            )
        assert number == 315
        called_args = mock_run.call_args.args[0]
        assert called_args == [
            "gh",
            "pr",
            "create",
            "--head",
            "integration/temp-main",
            "--base",
            "main",
            "--title",
            "Integrate completed tasks",
            "--body-file",
            "-",
        ]
        assert mock_run.call_args.kwargs.get("input") == "body text"

    def test_rejects_invalid_head_ref(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                create_pull_request(
                    head="evil; rm -rf /", base="main", title="t", body="b"
                )
            mock_run.assert_not_called()

    def test_rejects_invalid_base_ref(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                create_pull_request(
                    head="integration/temp-main",
                    base="evil; rm -rf /",
                    title="t",
                    body="b",
                )
            mock_run.assert_not_called()


class TestMergePullRequest:
    def test_merges_with_merge_commit(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            merge_pull_request(315)
        called_args = mock_run.call_args.args[0]
        assert called_args == ["gh", "pr", "merge", "315", "--merge"]

    def test_propagates_failure_on_unmergeable_pr(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, ["gh", "pr", "merge"], stderr="not mergeable"
            )
            with pytest.raises(subprocess.CalledProcessError):
                merge_pull_request(315)

    def test_rejects_invalid_pr_number(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                merge_pull_request("315; rm -rf /")  # type: ignore[arg-type]
            mock_run.assert_not_called()


class TestGetIssueState:
    def test_returns_open_state(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout='{"state": "OPEN"}'
            )
            state = get_issue_state(170)
        assert state == "OPEN"

    def test_returns_closed_state(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout='{"state": "CLOSED"}'
            )
            state = get_issue_state(170)
        assert state == "CLOSED"

    def test_rejects_invalid_issue_number(self):
        with patch("orchestune.forge.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                get_issue_state("170; rm -rf /")
            mock_run.assert_not_called()
