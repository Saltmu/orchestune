"""`GitHubForge` のPR・ブランチ系契約: どんな `gh` コマンドを組み立て、その
出力をどう `PrRecord` へ写すか。

`gh` の実行そのものは `gh_run` フィクスチャで置き換える。
"""

from __future__ import annotations

import json
import subprocess

import pytest

from orchestune.forge import GitHubForge
from orchestune.models import PrRecord


def _pr_list_payload(**fields) -> str:
    return json.dumps([{"number": 5, "headRefName": "feat/x", **fields}])


def _routed(**by_subcommand):
    """`gh pr list` / `gh api graphql` などをサブコマンド単位で振り分ける。"""

    def side_effect(args, **kwargs):
        for key, result in by_subcommand.items():
            if args[: len(key.split())] == key.split():
                if isinstance(result, BaseException):
                    raise result
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=result, stderr=""
                )
        raise ValueError(f"Unexpected command: {args}")

    return side_effect


class TestIsBranchMergedInto:
    def test_returns_true_when_exact_head_base_pr_was_merged(
        self, forge: GitHubForge, gh_run
    ):
        gh_run.stdout('[{"number":264}]')

        assert forge.is_branch_merged_into("parent/issue-100", "main") is True
        assert gh_run.call_args.args[0] == [
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

    def test_returns_false_when_exact_head_base_pr_was_not_merged(
        self, forge: GitHubForge, gh_run
    ):
        gh_run.stdout("[]")

        assert forge.is_branch_merged_into("parent/issue-100", "main") is False


class TestIsCurrentBranchTipMergedInto:
    def test_returns_true_when_current_remote_tip_is_in_base(
        self, forge: GitHubForge, gh_run
    ):
        gh_run.stdout_sequence(f"{'a' * 40}\n", "ahead\n")

        result = forge.is_current_branch_tip_merged_into(
            "claude/issue-1-task-1", "main"
        )

        assert result is True
        assert gh_run.call_args_list[0].args[0] == [
            "gh",
            "api",
            "repos/{owner}/{repo}/branches/claude%2Fissue-1-task-1",
            "--jq",
            ".commit.sha",
        ]
        assert gh_run.call_args_list[1].args[0] == [
            "gh",
            "api",
            f"repos/{{owner}}/{{repo}}/compare/{'a' * 40}...main",
            "--jq",
            ".status",
        ]

    @pytest.mark.parametrize("status", ["behind", "diverged"])
    def test_returns_false_when_current_tip_is_not_in_base(
        self, forge: GitHubForge, gh_run, status
    ):
        gh_run.stdout_sequence(f"{'b' * 40}\n", f"{status}\n")

        assert (
            forge.is_current_branch_tip_merged_into("claude/issue-1-task-1", "main")
            is False
        )

    @pytest.mark.parametrize("head,base", [("--evil", "main"), ("feat/x", "bad..base")])
    def test_rejects_invalid_refs(self, forge: GitHubForge, gh_run, head, base):
        with pytest.raises(ValueError):
            forge.is_current_branch_tip_merged_into(head, base)
        gh_run.assert_not_called()


class TestGetMergedPrTimestamp:
    def test_returns_merged_at_timestamp(self, forge: GitHubForge, gh_run):
        gh_run.stdout('[{"mergedAt":"2026-07-26T10:00:00Z"}]')

        assert forge.get_merged_pr_timestamp("parent/issue-100", "main") == (
            "2026-07-26T10:00:00Z"
        )
        assert gh_run.call_args.args[0] == [
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

    def test_returns_max_merged_at_when_multiple_merged_prs_exist(
        self, forge: GitHubForge, gh_run
    ):
        """#276レビュー対応 Reproducer: gh pr listはmergedAt順を保証しない
        ため、複数のmerged PR記録がある場合は最大（最新）のmergedAtを
        選ばなければならない。並び順がバラバラでも正しく最大値を選ぶ。"""
        gh_run.stdout(
            "["
            '{"mergedAt":"2026-07-20T10:00:00Z"},'
            '{"mergedAt":"2026-07-27T10:00:00Z"},'
            '{"mergedAt":"2026-07-15T10:00:00Z"}'
            "]"
        )

        assert forge.get_merged_pr_timestamp("parent/issue-100", "main") == (
            "2026-07-27T10:00:00Z"
        )

    def test_returns_none_when_no_merged_pr(self, forge: GitHubForge, gh_run):
        gh_run.stdout("[]")

        assert forge.get_merged_pr_timestamp("parent/issue-100", "main") is None


class TestBranchExists:
    def test_returns_true_when_branch_lookup_succeeds(self, forge: GitHubForge, gh_run):
        gh_run.stdout('{"name":"parent/issue-100"}')

        assert forge.branch_exists("parent/issue-100") is True

    def test_returns_false_on_404(self, forge: GitHubForge, gh_run):
        gh_run.side_effect = subprocess.CalledProcessError(
            1, ["gh", "api"], stderr="gh: Branch not found (HTTP 404)"
        )

        assert forge.branch_exists("parent/issue-100") is False

    def test_propagates_non_404_failures(self, forge: GitHubForge, gh_run):
        gh_run.side_effect = subprocess.CalledProcessError(
            1, ["gh", "api"], stderr="gh: rate limit exceeded"
        )

        with pytest.raises(subprocess.CalledProcessError):
            forge.branch_exists("parent/issue-100")


class TestDeleteBranch:
    def test_succeeds_when_branch_deleted(self, forge: GitHubForge, gh_run):
        gh_run.stdout("")

        forge.delete_branch("claude/issue-1-task-1")
        assert gh_run.call_args.args[0] == [
            "gh",
            "api",
            "--method",
            "DELETE",
            "repos/{owner}/{repo}/git/refs/heads/claude%2Fissue-1-task-1",
        ]

    def test_ignores_404(self, forge: GitHubForge, gh_run):
        gh_run.side_effect = subprocess.CalledProcessError(
            1, ["gh", "api"], stderr="gh: Reference does not exist (HTTP 404)"
        )

        forge.delete_branch("claude/issue-1-task-1")

    def test_propagates_non_404_failures(self, forge: GitHubForge, gh_run):
        gh_run.side_effect = subprocess.CalledProcessError(
            1, ["gh", "api"], stderr="gh: rate limit exceeded"
        )

        with pytest.raises(subprocess.CalledProcessError):
            forge.delete_branch("claude/issue-1-task-1")


class TestListPrsQuery:
    def test_can_include_all_pr_states(self, forge: GitHubForge, gh_run):
        gh_run.stdout("[]")

        assert forge.list_prs(state="all") == []

        called_args = gh_run.call_args.args[0]
        assert called_args[called_args.index("--state") + 1] == "all"
        assert "state" in called_args[called_args.index("--json") + 1]

    def test_rejects_unsupported_state(self, forge: GitHubForge, gh_run):
        with pytest.raises(ValueError, match="Unsupported PR state"):
            forge.list_prs(state="draft")

    def test_calls_gh_with_limit_arg(self, forge: GitHubForge, gh_run):
        gh_run.stdout("[]")

        forge.list_open_prs(limit=50)

        called_args = gh_run.call_args.args[0]
        assert called_args[called_args.index("--limit") + 1] == "50"

    def test_defaults_to_limit_1000(self, forge: GitHubForge, gh_run):
        gh_run.stdout("[]")

        forge.list_open_prs()

        called_args = gh_run.call_args.args[0]
        assert called_args[called_args.index("--limit") + 1] == "1000"


class TestListPrsRecordMapping:
    def test_records_each_pr_state(self, forge: GitHubForge, gh_run):
        gh_run.stdout(
            '[{"number": 5, "headRefName": "feat/x", "state": "MERGED"},'
            '{"number": 6, "headRefName": "feat/y", "state": "CLOSED"}]'
        )

        assert [pr.state for pr in forge.list_prs(state="all")] == ["MERGED", "CLOSED"]

    def test_records_closed_at(self, forge: GitHubForge, gh_run):
        gh_run.stdout(
            '[{"number": 6, "headRefName": "feat/y", "state": "CLOSED", '
            '"createdAt": "2026-01-01T00:00:00Z", '
            '"closedAt": "2026-01-03T00:00:00Z"}]'
        )

        prs = forge.list_prs(state="all")
        called_args = gh_run.call_args.args[0]

        assert prs[0].closed_at == "2026-01-03T00:00:00Z"
        assert "closedAt" in called_args[called_args.index("--json") + 1]

    def test_fetches_pr_list_and_per_pr_files(self, forge: GitHubForge, gh_run):
        gh_run.stdout(
            "["
            '{"number": 5, "headRefName": "feat/x", "reviewDecision": "APPROVED", '
            '"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}], '
            '"files": [{"path": "src/a.py"}, {"path": "src/b.py"}], '
            '"closingIssuesReferences": []},'
            '{"number": 6, "headRefName": "feat/y", '
            '"reviewDecision": "CHANGES_REQUESTED", '
            '"statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": null}], '
            '"files": [], "closingIssuesReferences": []}'
            "]"
        )

        prs = forge.list_open_prs()
        json_fields = gh_run.call_args.args[0][
            gh_run.call_args.args[0].index("--json") + 1
        ]

        assert "files" in json_fields
        assert "closingIssuesReferences" in json_fields
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

    def test_includes_closing_issue_references(self, forge: GitHubForge, gh_run):
        """#239: ブランチ名がAIセッションの指示通りにならない場合でも
        自己PR判定できるよう、PRが閉じるIssue番号一覧も取得する。"""
        gh_run.stdout(
            "["
            '{"number": 5, "headRefName": "claude/elegant-noether-5rli7u", '
            '"files": [{"path": "src/a.py"}], '
            '"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}], '
            '"closingIssuesReferences": [{"number": 218}]}'
            "]"
        )

        prs = forge.list_open_prs()
        json_fields = gh_run.call_args.args[0][
            gh_run.call_args.args[0].index("--json") + 1
        ]

        assert "closingIssuesReferences" in json_fields
        assert prs == [
            PrRecord(
                number=5,
                head_ref="claude/elegant-noether-5rli7u",
                changed_files=("src/a.py",),
                closes_issue_numbers=(218,),
            )
        ]

    def test_records_base_ref_and_head_repository_identity(
        self, forge: GitHubForge, gh_run
    ):
        """#243: 外部fork・別baseの同名branch PRを識別できるよう、
        base refとhead repositoryのidentityを取得・保持する。"""
        gh_run.stdout(
            "["
            '{"number": 5, "headRefName": "integration/temp-parent-issue-100", '
            '"baseRefName": "parent/issue-100", "isCrossRepository": false, '
            '"files": []},'
            '{"number": 6, "headRefName": "integration/temp-parent-issue-100", '
            '"baseRefName": "main", "isCrossRepository": true, "files": []}'
            "]"
        )

        prs = forge.list_open_prs()
        json_fields = gh_run.call_args.args[0][
            gh_run.call_args.args[0].index("--json") + 1
        ]

        assert "baseRefName" in json_fields
        assert "isCrossRepository" in json_fields
        assert prs[0].base_ref == "parent/issue-100"
        assert prs[0].is_cross_repository is False
        assert prs[1].base_ref == "main"
        assert prs[1].is_cross_repository is True

    def test_missing_identity_fields_default_to_unknown(
        self, forge: GitHubForge, gh_run
    ):
        """#243: identityフィールドが取得できないPRは「不明」として保持し、
        呼び出し側がfail closedに判定できるようにする。"""
        gh_run.stdout(_pr_list_payload(files=[]))

        prs = forge.list_open_prs()

        assert prs[0].base_ref == ""
        assert prs[0].is_cross_repository is None

    def test_closes_issue_numbers_defaults_to_empty_tuple(
        self, forge: GitHubForge, gh_run
    ):
        gh_run.stdout(_pr_list_payload(files=[]))

        assert forge.list_open_prs()[0].closes_issue_numbers == ()


class TestListPrsCiStatus:
    """`statusCheckRollup`はCheckRunとlegacy StatusContextのunionであり、
    gh CLIのバージョンによって形も揺れる。すべての形で正しく判定する。"""

    def test_accepts_nested_status_check_rollup_contexts(
        self, forge: GitHubForge, gh_run
    ):
        gh_run.stdout_sequence(
            "["
            '{"number": 5, "headRefName": "feat/x", "reviewDecision": "APPROVED", '
            '"statusCheckRollup": {"contexts": '
            '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]}},'
            '{"number": 6, "headRefName": "feat/y", "reviewDecision": "APPROVED", '
            '"statusCheckRollup": {"contexts": '
            '[{"status": "QUEUED", "conclusion": null}]}}'
            "]",
            '{"files": []}',
            '{"files": []}',
        )

        prs = forge.list_open_prs()

        assert prs[0].is_ci_passing is True
        assert prs[1].is_ci_passing is False

    @pytest.mark.parametrize("status_check_rollup", ["[]", '{"contexts": []}', "null"])
    def test_treats_absent_or_empty_status_checks_as_not_passing(
        self, forge: GitHubForge, gh_run, status_check_rollup
    ):
        gh_run.stdout(
            '[{"number": 5, "headRefName": "feat/x", '
            f'"statusCheckRollup": {status_check_rollup}}}]'
        )

        assert forge.list_open_prs()[0].is_ci_passing is False

    def test_legacy_status_context_success_counts_as_passing(
        self, forge: GitHubForge, gh_run
    ):
        """#256 Reproducer: statusCheckRollupはCheckRunとlegacy StatusContext
        のunionだが、全要素へCheckRun用のstatus/conclusionだけを適用すると
        StatusContextは常にfailing扱いになる。SUCCESSなStatusContextは
        passingとして扱わなければならない。"""
        gh_run.stdout(
            _pr_list_payload(
                statusCheckRollup=[{"__typename": "StatusContext", "state": "SUCCESS"}]
            )
        )

        assert forge.list_open_prs()[0].is_ci_passing is True

    @pytest.mark.parametrize("state", ["PENDING", "ERROR", "FAILURE"])
    def test_legacy_status_context_non_success_states_are_not_passing(
        self, forge: GitHubForge, gh_run, state
    ):
        gh_run.stdout(
            _pr_list_payload(
                statusCheckRollup=[{"__typename": "StatusContext", "state": state}]
            )
        )

        assert forge.list_open_prs()[0].is_ci_passing is False

    def test_mixed_check_run_and_status_context_all_passing(
        self, forge: GitHubForge, gh_run
    ):
        """CheckRunとStatusContextが混在するrollupで、両方passingならTrue。"""
        gh_run.stdout(
            _pr_list_payload(
                statusCheckRollup=[
                    {
                        "__typename": "CheckRun",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                    },
                    {"__typename": "StatusContext", "state": "SUCCESS"},
                ]
            )
        )

        assert forge.list_open_prs()[0].is_ci_passing is True

    def test_mixed_check_run_and_status_context_one_failing(
        self, forge: GitHubForge, gh_run
    ):
        """混在rollupで、片方でもfailingならis_ci_passingはFalseのまま。"""
        gh_run.stdout(
            _pr_list_payload(
                statusCheckRollup=[
                    {
                        "__typename": "CheckRun",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                    },
                    {"__typename": "StatusContext", "state": "PENDING"},
                ]
            )
        )

        assert forge.list_open_prs()[0].is_ci_passing is False

    def test_status_context_without_typename_is_detected_by_field_shape(
        self, forge: GitHubForge, gh_run
    ):
        """__typenameが欠落していても、statusやconclusionを持たずstateのみ
        持つ要素はStatusContextとしてshapeで判定できる（gh CLIバージョン差異
        への耐性）。"""
        gh_run.stdout(_pr_list_payload(statusCheckRollup=[{"state": "SUCCESS"}]))

        assert forge.list_open_prs()[0].is_ci_passing is True


class TestListPrsFilePagination:
    """`gh pr list --json files`は100件で打ち切られるため、超過時のみGraphQLで
    全件を取り直す。"""

    _LIST_PAYLOAD = json.dumps(
        [
            {
                "number": 10,
                "headRefName": "feat/large",
                "files": [{"path": f"file{i}.py"} for i in range(1, 101)],
                "closingIssuesReferences": [],
            }
        ]
    )
    _GRAPHQL_PAYLOAD = json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "files": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [{"path": f"file{i}.py"} for i in range(1, 102)],
                        }
                    }
                }
            }
        }
    )

    def test_paginates_files_when_100_or_more(self, forge: GitHubForge, gh_run):
        gh_run.side_effect = _routed(
            **{
                "gh pr list": self._LIST_PAYLOAD,
                "gh api graphql": self._GRAPHQL_PAYLOAD,
            }
        )

        prs = forge.list_open_prs(paginate_files=True)

        assert len(prs) == 1
        assert len(prs[0].changed_files) == 101

    def test_skips_pagination_when_paginate_files_false(
        self, forge: GitHubForge, gh_run
    ):
        """paginate_files=False（デフォルト）なら、100件でもGraphQLへ行かない。"""
        gh_run.side_effect = _routed(**{"gh pr list": self._LIST_PAYLOAD})

        prs = forge.list_open_prs()

        assert len(prs) == 1
        assert len(prs[0].changed_files) == 100
        assert prs[0].is_files_truncated is False

    def test_skips_pagination_for_non_open_state(self, forge: GitHubForge, gh_run):
        """state != 'open' なら、paginate_files=TrueでもGraphQLへ行かない。"""
        gh_run.side_effect = _routed(**{"gh pr list": self._LIST_PAYLOAD})

        prs = forge.list_prs(state="closed", paginate_files=True)

        assert len(prs) == 1
        assert len(prs[0].changed_files) == 100
        assert prs[0].is_files_truncated is False

    def test_marks_files_truncated_on_graphql_failure(self, forge: GitHubForge, gh_run):
        gh_run.side_effect = _routed(
            **{
                "gh pr list": self._LIST_PAYLOAD,
                "gh api graphql": subprocess.CalledProcessError(
                    1, ["gh", "api", "graphql"], stderr="GraphQL error"
                ),
            }
        )

        prs = forge.list_open_prs(paginate_files=True)

        assert len(prs) == 1
        assert prs[0].is_files_truncated is True


class TestCreatePullRequest:
    def test_creates_pr_and_returns_number_parsed_from_url(
        self, forge: GitHubForge, gh_run
    ):
        gh_run.stdout("https://github.com/Saltmu/manuscriptune/pull/315\n")

        number = forge.create_pull_request(
            head="integration/temp-main",
            base="main",
            title="Integrate completed tasks",
            body="body text",
        )

        assert number == 315
        assert gh_run.call_args.args[0] == [
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
        assert gh_run.call_args.kwargs.get("input") == b"body text"

    def test_rejects_invalid_head_ref(self, forge: GitHubForge, gh_run):
        with pytest.raises(ValueError):
            forge.create_pull_request(
                head="evil; rm -rf /", base="main", title="t", body="b"
            )
        gh_run.assert_not_called()

    def test_rejects_invalid_base_ref(self, forge: GitHubForge, gh_run):
        with pytest.raises(ValueError):
            forge.create_pull_request(
                head="integration/temp-main",
                base="evil; rm -rf /",
                title="t",
                body="b",
            )
        gh_run.assert_not_called()


class TestUpdatePullRequest:
    def test_edits_title_and_body_via_stdin(self, forge: GitHubForge, gh_run):
        forge.update_pull_request(
            315, title="Integrate completed tasks (task-a)", body="body text"
        )

        assert gh_run.call_args.args[0] == [
            "gh",
            "pr",
            "edit",
            "315",
            "--title",
            "Integrate completed tasks (task-a)",
            "--body-file",
            "-",
        ]
        assert gh_run.call_args.kwargs.get("input") == b"body text"

    def test_rejects_invalid_pr_number(self, forge: GitHubForge, gh_run):
        with pytest.raises(ValueError):
            forge.update_pull_request("not-a-number", title="t", body="b")
        gh_run.assert_not_called()


class TestMergePullRequest:
    def test_merges_with_merge_commit(self, forge: GitHubForge, gh_run):
        forge.merge_pull_request(315)

        assert gh_run.call_args.args[0] == ["gh", "pr", "merge", "315", "--merge"]

    def test_propagates_failure_on_unmergeable_pr(self, forge: GitHubForge, gh_run):
        gh_run.side_effect = subprocess.CalledProcessError(
            1, ["gh", "pr", "merge"], stderr="not mergeable"
        )

        with pytest.raises(subprocess.CalledProcessError):
            forge.merge_pull_request(315)

    def test_rejects_invalid_pr_number(self, forge: GitHubForge, gh_run):
        with pytest.raises(ValueError):
            forge.merge_pull_request("315; rm -rf /")  # type: ignore[arg-type]
        gh_run.assert_not_called()
