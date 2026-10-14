"""#1336: `get_pull_request` reads pull request evidence through REST without GraphQL.

The REST API has no closing-issue references or review decision, so those stay
unknown/empty instead of being guessed, and an open pull request never reports
GitHub's test merge commit as its merge commit.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from orchestune.forge import GitHubForge
from tests.test_forge_issues_rest_fallback import _FakeGh

SHA = "a" * 40
PR_PATH = "repos/{owner}/{repo}/pulls/5"
FILES_PATH = f"{PR_PATH}/files?per_page=100"
CHECKS_PATH = f"repos/{{owner}}/{{repo}}/commits/{SHA}/check-runs?per_page=100"
STATUS_PATH = f"repos/{{owner}}/{{repo}}/commits/{SHA}/status"


@pytest.fixture
def fake_gh(gh_run) -> Callable[..., _FakeGh]:
    def install(**kwargs: Any) -> _FakeGh:
        fake = _FakeGh(**kwargs)
        gh_run.side_effect = fake
        return fake

    return install


def _pull(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "number": 5,
        "title": "t",
        "body": "Closes #42",
        "state": "open",
        "created_at": "2026-01-01T00:00:00Z",
        "closed_at": None,
        "merged_at": None,
        "merge_commit_sha": "b" * 40,
        "changed_files": 2,
        "head": {
            "ref": "claude/issue-42-task",
            "sha": SHA,
            "repo": {"full_name": "o/r"},
        },
        "base": {"ref": "main", "repo": {"full_name": "o/r"}},
    }
    payload.update(overrides)
    return payload


def _rest(
    pull: dict[str, Any] | None = None,
    files: list[list[dict[str, Any]]] | None = None,
    checks: list[dict[str, Any]] | None = None,
    statuses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        PR_PATH: pull or _pull(),
        FILES_PATH: files
        if files is not None
        else [[{"filename": "a.py"}], [{"filename": "b.py"}]],
        CHECKS_PATH: [
            {
                "check_runs": checks
                if checks is not None
                else [{"status": "completed", "conclusion": "success"}]
            }
        ],
        STATUS_PATH: {"statuses": statuses or []},
    }


class TestGetPullRequestFallback:
    def test_open_pull_request_from_rest(self, forge: GitHubForge, fake_gh):
        fake = fake_gh(rest=_rest())

        pr = forge.get_pull_request(5)

        assert pr.number == 5
        assert pr.head_ref == "claude/issue-42-task"
        assert pr.head_sha == SHA
        assert pr.base_ref == "main"
        assert pr.state == "OPEN"
        assert pr.changed_files == ("a.py", "b.py")
        assert pr.is_files_truncated is False
        assert pr.is_cross_repository is False
        assert pr.is_ci_passing is True
        # Not guessed: REST has no closing references or review decision.
        assert pr.closes_issue_numbers == ()
        assert pr.closing_references_known is False
        assert pr.review_decision == ""
        # The open pull request's merge_commit_sha is only GitHub's test merge.
        assert pr.merge_commit_oid == ""
        assert fake.calls[0][:3] == ["gh", "pr", "view"]

    def test_merged_pull_request_reports_merged_state_and_commit(
        self, forge: GitHubForge, fake_gh
    ):
        fake_gh(
            rest=_rest(
                pull=_pull(
                    state="closed",
                    merged_at="2026-01-02T00:00:00Z",
                    closed_at="2026-01-02T00:00:00Z",
                )
            )
        )

        pr = forge.get_pull_request(5)

        assert pr.state == "MERGED"
        assert pr.merged_at == "2026-01-02T00:00:00Z"
        assert pr.merge_commit_oid == "b" * 40

    def test_closed_unmerged_pull_request(self, forge: GitHubForge, fake_gh):
        fake_gh(rest=_rest(pull=_pull(state="closed")))

        pr = forge.get_pull_request(5)

        assert pr.state == "CLOSED"
        assert pr.merge_commit_oid == ""

    @pytest.mark.parametrize(
        ("head_repo", "expected"),
        [({"full_name": "fork/r"}, True), (None, None)],
        ids=["fork", "deleted-fork"],
    )
    def test_cross_repository(
        self, forge: GitHubForge, fake_gh, head_repo: Any, expected: bool | None
    ):
        head = {"ref": "x", "sha": SHA, "repo": head_repo}
        fake_gh(rest=_rest(pull=_pull(head=head)))

        assert forge.get_pull_request(5).is_cross_repository is expected

    def test_fewer_files_than_reported_is_truncated(self, forge: GitHubForge, fake_gh):
        fake_gh(rest=_rest(pull=_pull(changed_files=3)))

        assert forge.get_pull_request(5).is_files_truncated is True

    @pytest.mark.parametrize(
        ("checks", "statuses", "passing"),
        [
            ([{"status": "completed", "conclusion": "success"}], [], True),
            (
                [{"status": "completed", "conclusion": "skipped"}],
                [{"state": "success"}],
                True,
            ),
            ([{"status": "in_progress", "conclusion": None}], [], False),
            ([{"status": "completed", "conclusion": "failure"}], [], False),
            ([], [{"state": "pending"}], False),
            ([], [], False),
        ],
        ids=[
            "success",
            "skipped+status",
            "running",
            "failure",
            "pending-status",
            "none",
        ],
    )
    def test_ci_status_follows_the_graphql_rule(
        self,
        forge: GitHubForge,
        fake_gh,
        checks: list[dict[str, Any]],
        statuses: list[dict[str, Any]],
        passing: bool,
    ):
        fake_gh(rest=_rest(checks=checks, statuses=statuses))

        assert forge.get_pull_request(5).is_ci_passing is passing

    def test_rest_failures_propagate(self, forge: GitHubForge, fake_gh):
        fake_gh(rest=_rest(), rest_errors={FILES_PATH: "gh: Server Error (HTTP 502)"})

        with pytest.raises(subprocess.CalledProcessError):
            forge.get_pull_request(5)

    def test_switch_is_shared_with_issue_operations(self, forge: GitHubForge, fake_gh):
        """A GraphQL 403 seen by an issue call sends the PR lookup straight to REST."""
        fake = fake_gh(
            rest={
                **_rest(),
                "repos/{owner}/{repo}/issues/42": {"number": 42, "state": "open"},
            }
        )

        forge.get_issue_state(42)
        forge.get_pull_request(5)

        assert len(fake.non_api_calls) == 1


class TestGraphQLPathUnchanged:
    def test_graphql_pull_request_keeps_closing_references_known(
        self, forge: GitHubForge, gh_run
    ):
        gh_run.stdout(
            '{"number": 5, "headRefName": "h", "headRefOid": "'
            + SHA
            + '", "baseRefName": "main", "closingIssuesReferences": [{"number": 42}], '
            '"files": [], "state": "OPEN"}'
        )

        pr = forge.get_pull_request(5)

        assert pr.closes_issue_numbers == (42,)
        assert pr.closing_references_known is True
        assert gh_run.call_args.args[0][:3] == ["gh", "pr", "view"]
