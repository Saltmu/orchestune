import subprocess
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
    get_label_actor,
    is_branch_merged_into,
    list_issues_by_label,
    list_open_prs,
    list_remote_branches,
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
            '{"number": 5, "headRefName": "feat/x", "reviewDecision": "APPROVED", "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]},'
            '{"number": 6, "headRefName": "feat/y", "reviewDecision": "CHANGES_REQUESTED", "statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": null}]}'
            "]"
        )
        files_payload_5 = '{"files": [{"path": "src/a.py"}, {"path": "src/b.py"}]}'
        files_payload_6 = '{"files": []}'

        with patch("orchestune.github.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=list_payload, stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=files_payload_5, stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=files_payload_6, stderr=""
                ),
            ]
            prs = list_open_prs()

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
        list_payload = '[{"number": 5, "headRefName": "claude/elegant-noether-5rli7u"}]'
        detail_payload = (
            '{"files": [{"path": "src/a.py"}], '
            '"closingIssuesReferences": [{"number": 218}]}'
        )

        with patch("orchestune.github.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=list_payload, stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=detail_payload, stderr=""
                ),
            ]
            prs = list_open_prs()
            called_args = mock_run.call_args_list[1].args[0]

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
        list_payload = '[{"number": 5, "headRefName": "feat/x"}]'
        detail_payload = '{"files": []}'

        with patch("orchestune.github.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=list_payload, stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=detail_payload, stderr=""
                ),
            ]
            prs = list_open_prs()

        assert prs[0].closes_issue_numbers == ()


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
        with patch("orchestune.github.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                128, ["git", "diff", "--name-only", "origin/main...origin/orphan"]
            )
            files = branch_changed_files("origin/orphan")
        assert files == []


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
