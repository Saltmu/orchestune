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


class TestCreateIssue:
    """#306: orchestune provisionが親/サブタスクIssueを起票する際に使う。"""

    def test_creates_issue_and_returns_number_parsed_from_url(
        self, forge: GitHubForge, gh_run
    ):
        gh_run.stdout("https://github.com/Saltmu/orchestune/issues/321\n")

        number = forge.create_issue("[FEAT] task-a: Do the thing", "body text")

        assert number == 321
        assert gh_run.call_args.args[0] == [
            "gh",
            "issue",
            "create",
            "--title",
            "[FEAT] task-a: Do the thing",
            "--body-file",
            "-",
        ]
        assert gh_run.call_args.kwargs.get("input") == "body text"

    def test_passes_each_label_as_a_separate_flag(self, forge: GitHubForge, gh_run):
        gh_run.stdout("https://github.com/Saltmu/orchestune/issues/1\n")

        forge.create_issue("t", "b", labels=("status:queued", "priority:high"))

        called_args = gh_run.call_args.args[0]
        assert called_args.count("--label") == 2
        assert "status:queued" in called_args
        assert "priority:high" in called_args

    def test_rejects_invalid_label_before_calling_subprocess(
        self, forge: GitHubForge, gh_run
    ):
        with pytest.raises(ValueError):
            forge.create_issue("t", "b", labels=("status:queued; evil",))
        gh_run.assert_not_called()


class TestAddSubIssue:
    def test_calls_gh_issue_edit_with_set_parent(self, forge: GitHubForge, gh_run):
        forge.add_sub_issue(100, 101)

        assert gh_run.call_args.args[0] == [
            "gh",
            "issue",
            "edit",
            "101",
            "--parent",
            "100",
        ]

    def test_rejects_invalid_issue_number(self, forge: GitHubForge, gh_run):
        with pytest.raises(ValueError):
            forge.add_sub_issue("100; evil", 101)
        gh_run.assert_not_called()


class TestSetBlockedBy:
    def test_calls_gh_issue_edit_with_add_blocked_by(self, forge: GitHubForge, gh_run):
        forge.set_blocked_by(102, 101)

        assert gh_run.call_args.args[0] == [
            "gh",
            "issue",
            "edit",
            "102",
            "--add-blocked-by",
            "101",
        ]

    def test_rejects_invalid_issue_number(self, forge: GitHubForge, gh_run):
        with pytest.raises(ValueError):
            forge.set_blocked_by(102, "101; evil")
        gh_run.assert_not_called()


class TestFindOpenIssuesByExactTitle:
    """#323 review: 親Issueの重複作成を防ぐための、部分失敗後のオーファン復旧に使う。"""

    def test_returns_only_exact_title_matches(self, forge: GitHubForge, gh_run):
        gh_run.stdout(
            '[{"number": 100, "title": "[EPIC] Big rock", "body": "marker text", '
            '"createdAt": "2026-01-01T00:00:00Z"}, '
            '{"number": 101, "title": "[EPIC] Big rock v2", "body": "", '
            '"createdAt": "2026-01-01T00:00:00Z"}]'
        )

        results = forge.find_open_issues_by_exact_title("[EPIC] Big rock")

        assert [r.number for r in results] == [100]
        assert results[0].body == "marker text"
        called_args = gh_run.call_args.args[0]
        assert called_args[:4] == ["gh", "issue", "list", "--search"]
        assert "--state" in called_args
        assert called_args[called_args.index("--state") + 1] == "open"

    def test_returns_every_exact_title_match(self, forge: GitHubForge, gh_run):
        """#323 review: two issues can share the exact title (an unrelated
        one and our own orphaned parent); the caller must be able to see
        all of them, not just the first."""
        gh_run.stdout(
            '[{"number": 100, "title": "[EPIC] Big rock", "body": "no marker", '
            '"createdAt": "2026-01-01T00:00:00Z"}, '
            '{"number": 102, "title": "[EPIC] Big rock", "body": "has marker", '
            '"createdAt": "2026-01-02T00:00:00Z"}]'
        )

        results = forge.find_open_issues_by_exact_title("[EPIC] Big rock")

        assert [r.number for r in results] == [100, 102]

    def test_returns_empty_list_when_no_exact_title_match(
        self, forge: GitHubForge, gh_run
    ):
        gh_run.stdout('[{"number": 101, "title": "[EPIC] Big rock v2"}]')

        assert forge.find_open_issues_by_exact_title("[EPIC] Big rock") == []

    def test_returns_empty_list_when_search_finds_nothing(
        self, forge: GitHubForge, gh_run
    ):
        gh_run.stdout("[]")

        assert forge.find_open_issues_by_exact_title("[EPIC] Big rock") == []


class TestFindIssuesByParentMetadata:
    """#485: ネイティブSub-issue関係が使えない環境向けの本文metadata検索。"""

    def test_calls_gh_search_with_parent_number_and_all_states(
        self, forge: GitHubForge, gh_run
    ):
        gh_run.stdout(
            '[{"number": 200, "title": "[FEAT] task-a: x", '
            '"body": "```yaml\\nparent_issue_number: 100\\n```", '
            '"labels": [], "createdAt": "2026-01-01T00:00:00Z", "state": "OPEN"}]'
        )

        results = forge.find_issues_by_parent_metadata(100)

        called_args = gh_run.call_args.args[0]
        assert called_args[:4] == ["gh", "issue", "list", "--search"]
        assert "100" in called_args[called_args.index("--search") + 1]
        assert called_args[called_args.index("--state") + 1] == "all"
        assert [r.number for r in results] == [200]

    def test_returns_empty_list_when_search_finds_nothing(
        self, forge: GitHubForge, gh_run
    ):
        gh_run.stdout("[]")

        assert forge.find_issues_by_parent_metadata(100) == []

    def test_rejects_invalid_parent_number(self, forge: GitHubForge, gh_run):
        with pytest.raises(ValueError):
            forge.find_issues_by_parent_metadata("1; evil")
        gh_run.assert_not_called()


class TestGetIssue:
    """#323 review: verifies a persisted issue_number actually belongs to
    this subtask before reusing/mutating it, independent of whether it's
    already linked to the parent."""

    def test_returns_record_with_body(self, forge: GitHubForge, gh_run):
        gh_run.stdout(
            '{"number": 42, "title": "[FEAT] task-a: x", "body": "b", '
            '"state": "OPEN", "labels": [{"name": "status:queued"}], '
            '"createdAt": "2026-01-01T00:00:00Z"}'
        )

        result = forge.get_issue(42)

        assert result is not None
        assert result.number == 42
        assert result.body == "b"
        assert result.labels == ("status:queued",)
        called_args = gh_run.call_args.args[0]
        assert called_args[:3] == ["gh", "issue", "view"]
        assert "42" in called_args

    def test_includes_native_parent_and_blocked_by(self, forge: GitHubForge, gh_run):
        """#485 review round 5 (P2): provisioning's reuse path needs to see
        an already-established native `parent` to avoid wrongly concluding
        a legacy issue has no discovery mechanism when the current forge
        merely can't *re-write* one that already exists."""
        gh_run.stdout(
            '{"number": 42, "title": "t", "body": "b", "state": "OPEN", '
            '"labels": [], "createdAt": "2026-01-01T00:00:00Z", '
            '"parent": {"number": 100}, '
            '"blockedBy": {"nodes": [{"number": 10}, {"number": 11}]}}'
        )

        result = forge.get_issue(42)

        assert result is not None
        assert result.parent == {"number": 100}
        assert result.blocked_by == (10, 11)
        called_args = gh_run.call_args.args[0]
        assert "parent" in called_args[called_args.index("--json") + 1]
        assert "blockedBy" in called_args[called_args.index("--json") + 1]

    def test_returns_none_on_404(self, forge: GitHubForge, gh_run):
        gh_run.side_effect = subprocess.CalledProcessError(
            1,
            ["gh", "issue", "view", "999"],
            stderr="GraphQL: Could not resolve to an Issue (HTTP 404)",
        )

        assert forge.get_issue(999) is None

    def test_propagates_non_not_found_failures(self, forge: GitHubForge, gh_run):
        """#323 review round 9 (P2): a transient failure (auth, rate limit,
        network) must not be conflated with a genuinely missing issue — the
        persisted-issue-identity verification callers rely on `None`
        meaning "confirmed gone", not "the CLI call failed for some
        reason", or a transient error would wrongly make provisioning
        create a duplicate issue."""
        gh_run.side_effect = subprocess.CalledProcessError(
            1, ["gh", "issue", "view", "999"], stderr="gh: rate limit exceeded"
        )

        with pytest.raises(subprocess.CalledProcessError):
            forge.get_issue(999)

    def test_rejects_invalid_issue_number(self, forge: GitHubForge, gh_run):
        with pytest.raises(ValueError):
            forge.get_issue("1; evil")
        gh_run.assert_not_called()


class TestListComments:
    def test_returns_parsed_and_normalized_comments(self, forge: GitHubForge, gh_run):
        gh_run.stdout(
            '{"comments": ['
            '{"body": "hello world", "createdAt": "2026-01-01T00:00:00Z", "author": {"login": "alice"}},'
            '{"body": "second comment", "createdAt": "2026-01-02T00:00:00Z", "author": {"login": "bob"}}'
            "]}"
        )

        comments = forge.list_comments(42)

        assert comments == [
            {
                "body": "hello world",
                "created_at": "2026-01-01T00:00:00Z",
                "author": "alice",
            },
            {
                "body": "second comment",
                "created_at": "2026-01-02T00:00:00Z",
                "author": "bob",
            },
        ]
        called_args = gh_run.call_args.args[0]
        assert called_args == ["gh", "issue", "view", "42", "--json", "comments"]

    def test_returns_empty_list_when_no_comments(self, forge: GitHubForge, gh_run):
        gh_run.stdout('{"comments": []}')

        assert forge.list_comments(42) == []

    def test_rejects_invalid_issue_number(self, forge: GitHubForge, gh_run):
        with pytest.raises(ValueError):
            forge.list_comments("invalid; injection")
        gh_run.assert_not_called()

    def test_falls_back_to_pr_view_when_issue_view_fails(
        self, forge: GitHubForge, gh_run
    ):
        gh_run.side_effect = [
            subprocess.CalledProcessError(
                1,
                ["gh", "issue", "view", "101", "--json", "comments"],
                stderr="GraphQL: Could not resolve to an issue with the number of 101.",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='{"comments": [{"body": "pr comment", "createdAt": "2026-01-01T00:00:00Z", "author": {"login": "carol"}}]}',
            ),
        ]

        comments = forge.list_comments(101)

        assert comments == [
            {
                "body": "pr comment",
                "created_at": "2026-01-01T00:00:00Z",
                "author": "carol",
            }
        ]
        assert gh_run.call_args_list[0].args[0] == [
            "gh",
            "issue",
            "view",
            "101",
            "--json",
            "comments",
        ]
        assert gh_run.call_args_list[1].args[0] == [
            "gh",
            "pr",
            "view",
            "101",
            "--json",
            "comments",
        ]
