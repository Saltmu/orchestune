"""`GitHubForge` のIssue系契約: どんな `gh` コマンドを組み立て、その出力を
どうドメインモデルへ写すか。

`gh` の実行そのものは `gh_run` フィクスチャで置き換える。
"""

from __future__ import annotations

import json
import subprocess

import pytest

from orchestune.forge import GitHubForge
from orchestune.models import IssueRecord


class TestListIssuesByLabel:
    def test_calls_gh_with_list_args_and_parses_json(self, forge: GitHubForge, gh_run):
        gh_run.stdout(
            '[{"number": 1, "title": "t", "body": "b", '
            '"labels": [{"name": "status:queued"}], '
            '"createdAt": "2026-01-01T00:00:00Z"}]'
        )

        result = forge.list_issues_by_label("status:queued")

        called_args = gh_run.call_args.args[0]
        assert called_args[0] == "gh"
        assert "--label" in called_args
        assert "status:queued" in called_args
        assert gh_run.call_args.kwargs.get("shell", False) is False
        assert result == [
            IssueRecord(
                number=1,
                title="t",
                body="b",
                labels=("status:queued",),
                created_at="2026-01-01T00:00:00Z",
            )
        ]

    def test_rejects_invalid_label_before_calling_subprocess(
        self, forge: GitHubForge, gh_run
    ):
        with pytest.raises(ValueError):
            forge.list_issues_by_label("status:queued; evil")
        gh_run.assert_not_called()

    def test_defaults_to_open_state(self, forge: GitHubForge, gh_run):
        gh_run.stdout("[]")

        forge.list_issues_by_label("status:done")

        called_args = gh_run.call_args.args[0]
        assert called_args[called_args.index("--state") + 1] == "open"

    def test_state_all_includes_closed_issues(self, forge: GitHubForge, gh_run):
        """#236: closedなIssueもstatus:done判定に含められるよう、
        stateを明示的に指定できるようにする。"""
        gh_run.stdout(
            '[{"number": 1, "title": "t", "body": "b", '
            '"labels": [{"name": "status:done"}], '
            '"createdAt": "2026-01-01T00:00:00Z"}]'
        )

        result = forge.list_issues_by_label("status:done", state="all")

        called_args = gh_run.call_args.args[0]
        assert called_args[called_args.index("--state") + 1] == "all"
        assert result[0].number == 1

    def test_rejects_invalid_state(self, forge: GitHubForge, gh_run):
        with pytest.raises(ValueError):
            forge.list_issues_by_label("status:done", state="bogus")
        gh_run.assert_not_called()

    def test_calls_gh_with_limit_arg(self, forge: GitHubForge, gh_run):
        gh_run.stdout("[]")

        forge.list_issues_by_label("status:queued", limit=100)

        called_args = gh_run.call_args.args[0]
        assert called_args[called_args.index("--limit") + 1] == "100"

    def test_defaults_to_limit_1000(self, forge: GitHubForge, gh_run):
        gh_run.stdout("[]")

        forge.list_issues_by_label("status:queued")

        called_args = gh_run.call_args.args[0]
        assert called_args[called_args.index("--limit") + 1] == "1000"


class TestListSubIssues:
    """#156: parent_issue_number指定時のfast path。gh api graphqlの
    subIssuesフィールド経由で親Issue配下の子Issueをまとめて取得する。"""

    def _page(self, nodes, has_next_page=False, end_cursor=None):
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

    def test_calls_gh_api_graphql_with_parent_number(self, forge: GitHubForge, gh_run):
        gh_run.stdout(self._page([]))

        forge.list_sub_issues(100)

        called_args = gh_run.call_args.args[0]
        assert called_args[0:3] == ["gh", "api", "graphql"]
        assert "number=100" in called_args

    def test_parses_full_issue_record_fields(self, forge: GitHubForge, gh_run):
        node = self._node(
            number=1,
            title="task-a",
            body="body text",
            blockedBy={"nodes": [{"number": 5}]},
        )
        gh_run.stdout(self._page([node]))

        assert forge.list_sub_issues(100) == [
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

    def test_paginates_until_no_next_page(self, forge: GitHubForge, gh_run):
        gh_run.stdout_sequence(
            self._page(
                [self._node(number=1)], has_next_page=True, end_cursor="cursor-1"
            ),
            self._page([self._node(number=2)]),
        )

        result = forge.list_sub_issues(100)

        assert [r.number for r in result] == [1, 2]
        assert gh_run.call_count == 2
        assert "after=cursor-1" in gh_run.call_args_list[1].args[0]

    def test_rejects_invalid_parent_issue_number(self, forge: GitHubForge, gh_run):
        with pytest.raises(ValueError):
            forge.list_sub_issues(-1)
        gh_run.assert_not_called()


class TestAddRemoveLabel:
    def test_add_label_calls_gh_issue_edit(self, forge: GitHubForge, gh_run):
        forge.add_label(184, "status:in-progress")

        assert gh_run.call_args.args[0] == [
            "gh",
            "issue",
            "edit",
            "184",
            "--add-label",
            "status:in-progress",
        ]

    def test_remove_label_calls_gh_issue_edit(self, forge: GitHubForge, gh_run):
        forge.remove_label(184, "status:queued")

        assert gh_run.call_args.args[0] == [
            "gh",
            "issue",
            "edit",
            "184",
            "--remove-label",
            "status:queued",
        ]

    def test_add_label_rejects_invalid_issue_number(self, forge: GitHubForge, gh_run):
        with pytest.raises(ValueError):
            forge.add_label("184 && evil", "status:queued")
        gh_run.assert_not_called()


class TestAddComment:
    def test_passes_body_via_stdin_not_argv(self, forge: GitHubForge, gh_run):
        body = "some body with `backticks` and $(dangerous)"

        forge.add_comment(184, body)

        assert gh_run.call_args.args[0] == [
            "gh",
            "issue",
            "comment",
            "184",
            "--body-file",
            "-",
        ]
        assert gh_run.call_args.kwargs.get("input") == body


class TestCloseIssue:
    def test_closes_with_reason(self, forge: GitHubForge, gh_run):
        forge.close_issue(280, "not planned")

        assert gh_run.call_args.args[0] == [
            "gh",
            "issue",
            "close",
            "280",
            "--reason",
            "not planned",
        ]

    def test_closes_with_comment(self, forge: GitHubForge, gh_run):
        forge.close_issue(
            280, "not planned", comment="既に実装済みのため対応不要でした。"
        )

        assert gh_run.call_args.args[0] == [
            "gh",
            "issue",
            "close",
            "280",
            "--reason",
            "not planned",
            "--comment",
            "既に実装済みのため対応不要でした。",
        ]

    def test_rejects_invalid_reason(self, forge: GitHubForge, gh_run):
        with pytest.raises(ValueError):
            forge.close_issue(280, "evil; rm -rf /")
        gh_run.assert_not_called()

    def test_rejects_invalid_issue_number(self, forge: GitHubForge, gh_run):
        with pytest.raises(ValueError):
            forge.close_issue("280; rm -rf /", "not planned")
        gh_run.assert_not_called()


class TestGetIssueLabels:
    def test_returns_label_names(self, forge: GitHubForge, gh_run):
        gh_run.stdout(
            '{"labels": [{"name": "semantic-review:passed"}, {"name": "status:done"}]}'
        )

        assert forge.get_issue_labels(181) == (
            "semantic-review:passed",
            "status:done",
        )

    def test_rejects_invalid_issue_number(self, forge: GitHubForge, gh_run):
        with pytest.raises(ValueError):
            forge.get_issue_labels("181; rm -rf /")
        gh_run.assert_not_called()


class TestGetIssueState:
    @pytest.mark.parametrize("state", ["OPEN", "CLOSED"])
    def test_returns_state_field(self, forge: GitHubForge, gh_run, state):
        gh_run.stdout(f'{{"state": "{state}"}}')

        assert forge.get_issue_state(170) == state

    def test_rejects_invalid_issue_number(self, forge: GitHubForge, gh_run):
        with pytest.raises(ValueError):
            forge.get_issue_state("170; rm -rf /")
        gh_run.assert_not_called()


class TestGetIssueLastReopenedAt:
    def test_returns_latest_reopened_event_timestamp(self, forge: GitHubForge, gh_run):
        gh_run.stdout(
            '[[{"event":"closed","created_at":"2026-07-20T00:00:00Z"},'
            '{"event":"reopened","created_at":"2026-07-21T00:00:00Z"},'
            '{"event":"reopened","created_at":"2026-07-25T00:00:00Z"}]]'
        )

        assert forge.get_issue_last_reopened_at(100) == "2026-07-25T00:00:00Z"

    def test_returns_none_when_never_reopened(self, forge: GitHubForge, gh_run):
        gh_run.stdout('[[{"event":"closed","created_at":"2026-07-20T00:00:00Z"}]]')

        assert forge.get_issue_last_reopened_at(100) is None


class TestGetLabelActor:
    """#119: `status:queued`ラベルを実際に付与したユーザーを特定する。"""

    def test_returns_actor_of_matching_labeled_event(self, forge: GitHubForge, gh_run):
        gh_run.stdout(
            '[[{"event": "labeled", "actor": {"login": "alice"}, '
            '"label": {"name": "status:queued"}}]]'
        )

        assert forge.get_label_actor(184, "status:queued") == "alice"
        assert gh_run.call_args.args[0] == [
            "gh",
            "api",
            "repos/{owner}/{repo}/issues/184/events",
            "--paginate",
            "--slurp",
        ]

    def test_ignores_labeled_events_for_other_labels(self, forge: GitHubForge, gh_run):
        gh_run.stdout_sequence(
            '[[{"event": "labeled", "actor": {"login": "bob"}, '
            '"label": {"name": "bug"}}]]',
            '{"author": {"login": "carol"}}',
        )

        assert forge.get_label_actor(184, "status:queued") == "carol"

    def test_falls_back_to_issue_author_when_no_labeled_event(
        self, forge: GitHubForge, gh_run
    ):
        """Issue作成時(`gh issue create --label`)に付与されたラベルは
        `labeled`イベントを残さないため、Issue作成者にフォールバックする。"""
        gh_run.stdout_sequence("[[]]", '{"author": {"login": "dave"}}')

        assert forge.get_label_actor(184, "status:queued") == "dave"
        assert gh_run.call_args_list[1].args[0] == [
            "gh",
            "issue",
            "view",
            "184",
            "--json",
            "author",
        ]

    def test_takes_most_recent_matching_event_across_pages(
        self, forge: GitHubForge, gh_run
    ):
        gh_run.stdout(
            "["
            '[{"event": "labeled", "actor": {"login": "alice"}, '
            '"label": {"name": "status:queued"}}],'
            '[{"event": "labeled", "actor": {"login": "mallory"}, '
            '"label": {"name": "status:queued"}}]'
            "]"
        )

        assert forge.get_label_actor(184, "status:queued") == "mallory"

    def test_rejects_invalid_issue_number(self, forge: GitHubForge, gh_run):
        with pytest.raises(ValueError):
            forge.get_label_actor("184; rm -rf /", "status:queued")
        gh_run.assert_not_called()

    def test_rejects_invalid_label(self, forge: GitHubForge, gh_run):
        with pytest.raises(ValueError):
            forge.get_label_actor(184, "status:queued; evil")
        gh_run.assert_not_called()


class TestGetActorPermission:
    """#119: actorのリポジトリ権限をGitHub APIから取得する。"""

    def test_returns_permission_field(self, forge: GitHubForge, gh_run):
        gh_run.stdout('{"permission": "write"}')

        assert forge.get_actor_permission("alice") == "write"
        assert gh_run.call_args.args[0] == [
            "gh",
            "api",
            "repos/{owner}/{repo}/collaborators/alice/permission",
        ]

    def test_treats_api_error_as_none(self, forge: GitHubForge, gh_run):
        gh_run.side_effect = subprocess.CalledProcessError(1, ["gh", "api", "..."])

        assert forge.get_actor_permission("mallory") == "none"

    def test_rejects_invalid_username(self, forge: GitHubForge, gh_run):
        with pytest.raises(ValueError):
            forge.get_actor_permission("alice; rm -rf /")
        gh_run.assert_not_called()

    def test_treats_empty_username_as_none_without_calling_subprocess(
        self, forge: GitHubForge, gh_run
    ):
        """#208: get_label_actorがghostユーザー等で空文字を返した場合、
        ValueErrorを送出せず安全側の`none`を返す。"""
        assert forge.get_actor_permission("") == "none"
        gh_run.assert_not_called()
