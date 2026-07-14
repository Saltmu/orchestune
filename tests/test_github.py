import subprocess
from io import StringIO
from unittest.mock import patch

import pytest

from orchestune.github import (
    IssueRecord,
    PrRecord,
    _validate_issue_number,
    _validate_label,
    _validate_ref_name,
    _validate_username,
    add_comment,
    add_label,
    branch_changed_files,
    close_issue,
    create_pull_request,
    get_actor_permission,
    get_issue_labels,
    get_issue_state,
    get_label_actor,
    is_branch_merged_into,
    list_issues_by_label,
    list_open_prs,
    list_remote_branches,
    list_sub_issues,
    merge_pull_request,
    remove_label,
)


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
        with patch("orchestune.github.subprocess.run") as mock_run:
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
        with patch("orchestune.github.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                list_issues_by_label("status:queued; evil")
            mock_run.assert_not_called()

    def test_defaults_to_open_state(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
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
        with patch("orchestune.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=payload, stderr=""
            )
            result = list_issues_by_label("status:done", state="all")
        called_args = mock_run.call_args.args[0]
        assert called_args[called_args.index("--state") + 1] == "all"
        assert result[0].number == 1

    def test_rejects_invalid_state(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                list_issues_by_label("status:done", state="bogus")
            mock_run.assert_not_called()

    def test_calls_gh_with_limit_arg(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="[]", stderr=""
            )
            list_issues_by_label("status:queued", limit=100)
        called_args = mock_run.call_args.args[0]
        assert "--limit" in called_args
        assert called_args[called_args.index("--limit") + 1] == "100"

    def test_defaults_to_limit_1000(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
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
        with patch("orchestune.github.subprocess.run") as mock_run:
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
        with patch("orchestune.github.subprocess.run") as mock_run:
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
        with patch("orchestune.github.subprocess.run") as mock_run:
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
        with patch("orchestune.github.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                list_sub_issues(-1)
            mock_run.assert_not_called()


class TestAddRemoveLabel:
    def test_add_label_calls_gh_issue_edit(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
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
        with patch("orchestune.github.subprocess.run") as mock_run:
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
        with patch("orchestune.github.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                add_label("184 && evil", "status:queued")
            mock_run.assert_not_called()


class TestAddComment:
    def test_passes_body_via_stdin_not_argv(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
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


class TestListRemoteBranches:
    def test_parses_branch_lines(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="origin/main\norigin/feat/foo\n",
                stderr="",
            )
            branches = list_remote_branches()
        assert branches == ["origin/main", "origin/feat/foo"]


class TestIsBranchMergedInto:
    def test_returns_true_for_matching_merged_pr(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout='[{"number": 42}]', stderr=""
            )

            result = is_branch_merged_into("claude/issue-1-task-1", "main")

        assert result is True
        assert mock_run.call_args.args[0] == [
            "gh",
            "pr",
            "list",
            "--state",
            "merged",
            "--head",
            "claude/issue-1-task-1",
            "--base",
            "main",
            "--json",
            "number",
            "--limit",
            "1",
        ]

    def test_returns_false_when_no_matching_merged_pr_exists(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="[]", stderr=""
            )

            assert is_branch_merged_into("claude/issue-1-task-1", "main") is False

    @pytest.mark.parametrize("head,base", [("--evil", "main"), ("feat/x", "bad..base")])
    def test_rejects_invalid_refs(self, head, base):
        with patch("orchestune.github.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                is_branch_merged_into(head, base)
            mock_run.assert_not_called()


class TestListOpenPrs:
    def test_fetches_pr_list_and_per_pr_files(self):
        list_payload = (
            "["
            '{"number": 5, "headRefName": "feat/x", "reviewDecision": "APPROVED", "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}], "files": [{"path": "src/a.py"}, {"path": "src/b.py"}], "closingIssuesReferences": []},'
            '{"number": 6, "headRefName": "feat/y", "reviewDecision": "CHANGES_REQUESTED", "statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": null}], "files": [], "closingIssuesReferences": []}'
            "]"
        )

        with patch("orchestune.github.subprocess.run") as mock_run:
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

        with patch("orchestune.github.subprocess.run") as mock_run:
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

    def test_includes_closing_issue_references(self):
        """#239: ブランチ名がAIセッションの指示通りにならない場合でも
        自己PR判定できるよう、PRが閉じるIssue番号一覧も取得する。"""
        list_payload = (
            "["
            '{"number": 5, "headRefName": "claude/elegant-noether-5rli7u", '
            '"files": [{"path": "src/a.py"}], '
            '"closingIssuesReferences": [{"number": 218}]}'
            "]"
        )

        with patch("orchestune.github.subprocess.run") as mock_run:
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

    def test_closes_issue_numbers_defaults_to_empty_tuple(self):
        list_payload = '[{"number": 5, "headRefName": "feat/x", "files": []}]'

        with patch("orchestune.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=list_payload, stderr=""
            )
            prs = list_open_prs()

        assert prs[0].closes_issue_numbers == ()

    def test_calls_gh_with_limit_arg(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="[]", stderr=""
            )
            list_open_prs(limit=50)
        called_args = mock_run.call_args.args[0]
        assert "--limit" in called_args
        assert called_args[called_args.index("--limit") + 1] == "50"

    def test_defaults_to_limit_1000(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="[]", stderr=""
            )
            list_open_prs()
        called_args = mock_run.call_args.args[0]
        assert "--limit" in called_args
        assert called_args[called_args.index("--limit") + 1] == "1000"


class TestBranchChangedFiles:
    def test_calls_git_diff_name_only(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="src/a.py\nsrc/b.py\n", stderr=""
            )
            files = branch_changed_files("origin/feat/x")
        called_args = mock_run.call_args.args[0]
        assert called_args[:2] == ["git", "diff"]
        assert "origin/main...origin/feat/x" in called_args
        assert files == ["src/a.py", "src/b.py"]

    def test_rejects_invalid_branch_name(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                branch_changed_files("--upload-pack=evil")
            mock_run.assert_not_called()

    def test_returns_empty_list_when_branch_has_no_merge_base(self):
        """#232: mainと共通の祖先を持たない(orphanな)ブランチとの3点diffは
        `fatal: no merge base`でexit 128になる。dispatch-cycle全体をクラッシュ
        させず、footprint差分なし（ロック対象外）として扱うべき。"""
        stderr = StringIO()
        with (
            patch("orchestune.github.subprocess.run") as mock_run,
            patch("sys.stderr", stderr),
        ):
            mock_run.side_effect = subprocess.CalledProcessError(
                128,
                ["git", "diff", "--name-only", "origin/main...origin/orphan"],
                stderr="fatal: no merge base\n",
            )
            files = branch_changed_files("origin/orphan")
        assert files == []
        assert "Warning: failed to diff changed files" in stderr.getvalue()
        assert "origin/orphan" in stderr.getvalue()
        assert "fatal: no merge base" in stderr.getvalue()

    def test_logs_warning_when_git_diff_cannot_run(self):
        stderr = StringIO()
        with (
            patch("orchestune.github.subprocess.run") as mock_run,
            patch("sys.stderr", stderr),
        ):
            mock_run.side_effect = OSError("git binary missing")
            files = branch_changed_files("origin/feat/x")

        assert files == []
        assert "Warning: unable to inspect changed files" in stderr.getvalue()
        assert "git binary missing" in stderr.getvalue()


class TestGetIssueLabels:
    def test_returns_label_names(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='{"labels": [{"name": "semantic-review:passed"}, {"name": "status:done"}]}',
            )
            labels = get_issue_labels(181)
        assert labels == ("semantic-review:passed", "status:done")

    def test_rejects_invalid_issue_number(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
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
        with patch("orchestune.github.subprocess.run") as mock_run:
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
        with patch("orchestune.github.subprocess.run") as mock_run:
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
        with patch("orchestune.github.subprocess.run") as mock_run:
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
        with patch("orchestune.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=events_payload, stderr=""
            )
            actor = get_label_actor(184, "status:queued")
        assert actor == "mallory"

    def test_rejects_invalid_issue_number(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                get_label_actor("184; rm -rf /", "status:queued")
            mock_run.assert_not_called()

    def test_rejects_invalid_label(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                get_label_actor(184, "status:queued; evil")
            mock_run.assert_not_called()


class TestGetActorPermission:
    """#119: actorのリポジトリ権限をGitHub APIから取得する。"""

    def test_returns_permission_field(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
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
        with patch("orchestune.github.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, ["gh", "api", "..."]
            )
            permission = get_actor_permission("mallory")
        assert permission == "none"

    def test_rejects_invalid_username(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                get_actor_permission("alice; rm -rf /")
            mock_run.assert_not_called()


class TestCloseIssue:
    def test_closes_with_reason(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
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
        with patch("orchestune.github.subprocess.run") as mock_run:
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
        with patch("orchestune.github.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                close_issue(280, "evil; rm -rf /")
            mock_run.assert_not_called()

    def test_rejects_invalid_issue_number(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                close_issue("280; rm -rf /", "not planned")
            mock_run.assert_not_called()


class TestCreatePullRequest:
    def test_creates_pr_and_returns_number_parsed_from_url(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
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
        with patch("orchestune.github.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                create_pull_request(
                    head="evil; rm -rf /", base="main", title="t", body="b"
                )
            mock_run.assert_not_called()

    def test_rejects_invalid_base_ref(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
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
        with patch("orchestune.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            merge_pull_request(315)
        called_args = mock_run.call_args.args[0]
        assert called_args == ["gh", "pr", "merge", "315", "--merge"]

    def test_propagates_failure_on_unmergeable_pr(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, ["gh", "pr", "merge"], stderr="not mergeable"
            )
            with pytest.raises(subprocess.CalledProcessError):
                merge_pull_request(315)

    def test_rejects_invalid_pr_number(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                merge_pull_request("315; rm -rf /")  # type: ignore[arg-type]
            mock_run.assert_not_called()


class TestGetIssueState:
    def test_returns_open_state(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout='{"state": "OPEN"}'
            )
            state = get_issue_state(170)
        assert state == "OPEN"

    def test_returns_closed_state(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout='{"state": "CLOSED"}'
            )
            state = get_issue_state(170)
        assert state == "CLOSED"

    def test_rejects_invalid_issue_number(self):
        with patch("orchestune.github.subprocess.run") as mock_run:
            with pytest.raises(ValueError):
                get_issue_state("170; rm -rf /")
            mock_run.assert_not_called()


class TestEnsureParentBranch:
    def test_ensure_parent_branch_does_not_checkout_and_pushes_directly(self):
        # リモートに親ブランチが存在しない場合、checkoutを行わずに直接pushする
        with patch("orchestune.github._run") as mock_run:
            from orchestune.github import ensure_parent_branch

            # 1. ls-remote -> "" (存在しない)
            # 2. fetch main -> ""
            # 3. push -> ""
            # 4. fetch parent -> ""
            mock_run.side_effect = ["", "", "", ""]

            ensure_parent_branch(129)

            called_commands = [call[0][0] for call in mock_run.call_args_list]

            # git checkout は一度も呼ばれないべき
            for cmd in called_commands:
                assert "checkout" not in cmd
                assert "pull" not in cmd

            # 期待されるコマンドが実行されたことを検証
            assert mock_run.call_count == 4
            assert called_commands[0] == [
                "git",
                "ls-remote",
                "origin",
                "refs/heads/parent/issue-129",
            ]
            assert called_commands[1] == ["git", "fetch", "origin", "main"]
            assert called_commands[2] == [
                "git",
                "push",
                "origin",
                "FETCH_HEAD:refs/heads/parent/issue-129",
            ]
            assert called_commands[3] == [
                "git",
                "fetch",
                "origin",
                "+refs/heads/parent/issue-129:refs/remotes/origin/parent/issue-129",
            ]

    def test_ensure_parent_branch_does_nothing_if_remote_exists(self):
        with patch("orchestune.github._run") as mock_run:
            from orchestune.github import ensure_parent_branch

            # ls-remote が結果（ハッシュ値など）を返す（存在する）
            # fetch -> ""
            mock_run.side_effect = ["abcdef123456... refs/heads/parent/issue-129", ""]

            ensure_parent_branch(129)

            called_commands = [call[0][0] for call in mock_run.call_args_list]
            assert len(called_commands) == 2
            assert called_commands[0] == [
                "git",
                "ls-remote",
                "origin",
                "refs/heads/parent/issue-129",
            ]
            assert called_commands[1] == [
                "git",
                "fetch",
                "origin",
                "+refs/heads/parent/issue-129:refs/remotes/origin/parent/issue-129",
            ]

    def test_ensure_parent_branch_handles_ls_remote_error(self):
        # ls-remote が例外を投げた場合、存在しないものとみなして作成処理へ進む
        with patch("orchestune.github._run") as mock_run:
            from orchestune.github import ensure_parent_branch

            # 1. ls-remote -> Exception
            # 2. fetch main -> ""
            # 3. push -> ""
            # 4. fetch parent -> ""
            mock_run.side_effect = [Exception("network error"), "", "", ""]

            ensure_parent_branch(129)

            called_commands = [call[0][0] for call in mock_run.call_args_list]
            assert mock_run.call_count == 4
            assert called_commands[0] == [
                "git",
                "ls-remote",
                "origin",
                "refs/heads/parent/issue-129",
            ]
            assert called_commands[1] == ["git", "fetch", "origin", "main"]
            assert called_commands[2] == [
                "git",
                "push",
                "origin",
                "FETCH_HEAD:refs/heads/parent/issue-129",
            ]
            assert called_commands[3] == [
                "git",
                "fetch",
                "origin",
                "+refs/heads/parent/issue-129:refs/remotes/origin/parent/issue-129",
            ]

    def test_ensure_parent_branch_handles_push_error_without_crashing(self):
        # fetch または push が失敗しても、警告を出力してクラッシュしない
        with patch("orchestune.github._run") as mock_run:
            from orchestune.github import ensure_parent_branch

            # 1. ls-remote -> ""
            # 2. fetch -> Exception
            mock_run.side_effect = ["", Exception("fetch failed")]

            # 例外がスローされないことを確認
            ensure_parent_branch(129)

            called_commands = [call[0][0] for call in mock_run.call_args_list]
            assert len(called_commands) == 2

    def test_ensure_parent_branch_real_git_fetch_head(self, tmp_path):
        import os
        import subprocess

        from orchestune.github import ensure_parent_branch

        # 1. リモートとローカルリポジトリを準備
        remote_dir = tmp_path / "remote.git"
        remote_dir.mkdir()
        subprocess.run(["git", "init", "--bare"], cwd=str(remote_dir), check=True)
        # リモートのデフォルトブランチを main に設定
        subprocess.run(
            ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
            cwd=str(remote_dir),
            check=True,
        )

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(local_dir), check=True)
        # ローカルのデフォルトブランチを main に切り替える
        subprocess.run(
            ["git", "checkout", "-b", "main"], cwd=str(local_dir), check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(local_dir),
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(local_dir),
            check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote_dir)],
            cwd=str(local_dir),
            check=True,
        )

        # 最初のコミットを作成して push (これが古い main になる)
        dummy_file = local_dir / "dummy.txt"
        dummy_file.write_text("commit 1")
        subprocess.run(["git", "add", "dummy.txt"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "commit 1"], cwd=str(local_dir), check=True
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=str(local_dir),
            check=True,
        )

        # ローカルの refs/remotes/origin/main が commit 1 を指している状態
        old_sha = subprocess.check_output(
            ["git", "rev-parse", "refs/remotes/origin/main"],
            cwd=str(local_dir),
            text=True,
        ).strip()

        # 2. リモートの main を進める（別のクローンで新しいコミットを push）
        clone_dir = tmp_path / "clone"
        subprocess.run(["git", "clone", str(remote_dir), str(clone_dir)], check=True)
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(clone_dir),
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(clone_dir),
            check=True,
        )
        # 変更をコミットして push
        (clone_dir / "dummy.txt").write_text("commit 2")
        subprocess.run(["git", "add", "dummy.txt"], cwd=str(clone_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "commit 2"], cwd=str(clone_dir), check=True
        )
        subprocess.run(
            ["git", "push", "origin", "main"], cwd=str(clone_dir), check=True
        )

        new_sha = subprocess.check_output(
            ["git", "rev-parse", "main"], cwd=str(clone_dir), text=True
        ).strip()
        assert old_sha != new_sha

        # 3. local_dir 側で ensure_parent_branch を実行
        original_cwd = os.getcwd()
        os.chdir(str(local_dir))
        try:
            ensure_parent_branch(999)
        finally:
            os.chdir(original_cwd)

        # 4. 作成されたリモートの parent/issue-999 ブランチの SHA が、古い SHA ではなく、リモートの最新 main (new_sha) であることを確認する
        parent_sha = (
            subprocess.check_output(
                ["git", "ls-remote", str(remote_dir), "refs/heads/parent/issue-999"],
                text=True,
            )
            .split()[0]
            .strip()
        )

        assert parent_sha == new_sha

    def test_ensure_parent_branch_fetch_existing_branch(self, tmp_path):
        import os
        import subprocess

        from orchestune.github import (
            ensure_parent_branch,
            resolve_local_or_remote_branch,
        )

        # 1. リモートとローカルリポジトリを準備
        remote_dir = tmp_path / "remote.git"
        remote_dir.mkdir()
        subprocess.run(["git", "init", "--bare"], cwd=str(remote_dir), check=True)
        subprocess.run(
            ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
            cwd=str(remote_dir),
            check=True,
        )

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "checkout", "-b", "main"], cwd=str(local_dir), check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(local_dir),
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(local_dir),
            check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote_dir)],
            cwd=str(local_dir),
            check=True,
        )

        # 最初のコミットを作成して push (これが古い SHA になる)
        dummy_file = local_dir / "dummy.txt"
        dummy_file.write_text("commit 1")
        subprocess.run(["git", "add", "dummy.txt"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "commit 1"], cwd=str(local_dir), check=True
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=str(local_dir),
            check=True,
        )
        sha1 = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(local_dir), text=True
        ).strip()

        # 2. 別のクローンから、リモート上に親ブランチ parent/issue-888 を作成する
        clone_dir = tmp_path / "clone"
        subprocess.run(["git", "clone", str(remote_dir), str(clone_dir)], check=True)
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(clone_dir),
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(clone_dir),
            check=True,
        )

        # 新しいコミットを作成して親ブランチとして push する
        (clone_dir / "dummy.txt").write_text("commit 2")
        subprocess.run(["git", "add", "dummy.txt"], cwd=str(clone_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "commit 2"], cwd=str(clone_dir), check=True
        )
        subprocess.run(
            ["git", "push", "origin", "HEAD:refs/heads/parent/issue-888"],
            cwd=str(clone_dir),
            check=True,
        )
        sha2 = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(clone_dir), text=True
        ).strip()

        assert sha1 != sha2

        # 3. 対象 clone (local_dir) にて、古いローカルブランチ parent/issue-888 を sha1 で作成する
        # この時点では、local_dir 側には refs/remotes/origin/parent/issue-888 はまだ存在しない (fetchしていないため)
        subprocess.run(
            ["git", "branch", "parent/issue-888", sha1],
            cwd=str(local_dir),
            check=True,
        )

        # 4. ensure_parent_branch(888) を実行する
        original_cwd = os.getcwd()
        os.chdir(str(local_dir))
        try:
            ensure_parent_branch(888)
        finally:
            os.chdir(original_cwd)

        # 5. リモート追跡ブランチ refs/remotes/origin/parent/issue-888 が自動で取得され、sha2 を指していることを確認する
        tracking_sha = subprocess.check_output(
            ["git", "rev-parse", "refs/remotes/origin/parent/issue-888"],
            cwd=str(local_dir),
            text=True,
        ).strip()
        assert tracking_sha == sha2

        # 6. prefer_remote=True で解決した際、古いローカルブランチ (sha1) ではなく、最新のリモート側 (origin/parent/issue-888) が返ることを確認する
        assert (
            resolve_local_or_remote_branch(
                local_dir, "parent/issue-888", prefer_remote=True
            )
            == "origin/parent/issue-888"
        )


class TestResolveLocalOrRemoteBranch:
    def test_resolve_local_or_remote_branch(self, tmp_path):
        import subprocess

        from orchestune.github import resolve_local_or_remote_branch

        # 1. ローカル・リモートリポジトリの初期化
        remote_dir = tmp_path / "remote.git"
        remote_dir.mkdir()
        subprocess.run(["git", "init", "--bare"], cwd=str(remote_dir), check=True)

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(local_dir), check=True)
        # 初期ユーザーとメールのアドレスを設定しないとコミットに失敗することがあるため設定
        subprocess.run(
            ["git", "config", "user.name", "Test User"], cwd=str(local_dir), check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(local_dir),
            check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote_dir)],
            cwd=str(local_dir),
            check=True,
        )

        # 最初のダミーコミットを作成してプッシュ
        dummy_file = local_dir / "dummy.txt"
        dummy_file.write_text("hello")
        subprocess.run(["git", "add", "dummy.txt"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial commit"], cwd=str(local_dir), check=True
        )
        subprocess.run(
            ["git", "push", "origin", "HEAD:refs/heads/main"],
            cwd=str(local_dir),
            check=True,
        )

        # 2. ローカルブランチが存在する場合の解決
        # ローカルブランチ "parent/issue-129" を作成する
        subprocess.run(
            ["git", "checkout", "-b", "parent/issue-129"],
            cwd=str(local_dir),
            check=True,
        )
        assert (
            resolve_local_or_remote_branch(local_dir, "parent/issue-129")
            == "parent/issue-129"
        )

        # 3. ローカルには存在せず、リモート追跡ブランチのみが存在する場合の解決
        # リモートに push する
        subprocess.run(
            ["git", "push", "origin", "parent/issue-129"],
            cwd=str(local_dir),
            check=True,
        )
        # mainに戻る
        subprocess.run(["git", "checkout", "main"], cwd=str(local_dir), check=True)
        # ローカルの parent/issue-129 を削除
        subprocess.run(
            ["git", "branch", "-D", "parent/issue-129"], cwd=str(local_dir), check=True
        )

        # origin/parent/issue-129 というリモート追跡ブランチが存在することを確認
        assert (
            resolve_local_or_remote_branch(local_dir, "parent/issue-129")
            == "origin/parent/issue-129"
        )

        # 4. どちらも存在しない場合
        # 存在しないブランチ名を解決しようとすると、そのままのブランチ名が返る
        assert (
            resolve_local_or_remote_branch(local_dir, "nonexistent-branch")
            == "nonexistent-branch"
        )

    def test_resolve_local_or_remote_branch_prefer_remote(self, tmp_path):
        import subprocess

        from orchestune.github import resolve_local_or_remote_branch

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(local_dir),
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(local_dir),
            check=True,
        )

        # ダミーコミット 1
        dummy_file = local_dir / "dummy.txt"
        dummy_file.write_text("hello 1")
        subprocess.run(["git", "add", "dummy.txt"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "commit 1"], cwd=str(local_dir), check=True
        )
        sha1 = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(local_dir), text=True
        ).strip()

        # ローカルブランチ "parent/issue-129" を作成 (sha1 を指す)
        subprocess.run(
            ["git", "branch", "parent/issue-129"], cwd=str(local_dir), check=True
        )

        # ダミーコミット 2 (SHAを進める)
        dummy_file.write_text("hello 2")
        subprocess.run(["git", "add", "dummy.txt"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "commit 2"], cwd=str(local_dir), check=True
        )
        sha2 = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(local_dir), text=True
        ).strip()

        # リモート追跡参照を作成 (sha2 を指す)
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/parent/issue-129", sha2],
            cwd=str(local_dir),
            check=True,
        )

        assert sha1 != sha2

        # prefer_remote=False の場合は、ローカルブランチが優先されて "parent/issue-129" が返る
        assert (
            resolve_local_or_remote_branch(
                local_dir, "parent/issue-129", prefer_remote=False
            )
            == "parent/issue-129"
        )

        # prefer_remote=True の場合は、リモート追跡ブランチが優先されて "origin/parent/issue-129" が返る
        assert (
            resolve_local_or_remote_branch(
                local_dir, "parent/issue-129", prefer_remote=True
            )
            == "origin/parent/issue-129"
        )
