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
STATUS_PATH = f"repos/{{owner}}/{{repo}}/commits/{SHA}/status?per_page=100"


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
        STATUS_PATH: [{"total_count": len(statuses or []), "statuses": statuses or []}],
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

    def test_status_contexts_on_later_pages_count(self, forge: GitHubForge, fake_gh):
        """A pending context beyond the first page must not read as passing CI."""
        rest = _rest()
        rest[STATUS_PATH] = [
            {"total_count": 2, "statuses": [{"state": "success"}]},
            {"total_count": 2, "statuses": [{"state": "pending"}]},
        ]
        fake_gh(rest=rest)

        assert forge.get_pull_request(5).is_ci_passing is False

    def test_incomplete_status_pages_fail(self, forge: GitHubForge, fake_gh):
        rest = _rest()
        rest[STATUS_PATH] = [{"total_count": 31, "statuses": [{"state": "success"}]}]
        fake_gh(rest=rest)

        with pytest.raises(ValueError):
            forge.get_pull_request(5)

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


REPO_URL = "https://api.github.com/repos/o/r"
MERGED_AT = "2026-01-02T00:00:00Z"


def _timeline_path(issue: int) -> str:
    return f"repos/{{owner}}/{{repo}}/issues/{issue}/timeline?per_page=100"


def _merged_pull(**overrides: Any) -> dict[str, Any]:
    payload = _pull(
        state="closed",
        merged_at=MERGED_AT,
        closed_at=MERGED_AT,
        merged_by={"login": "merger"},
        base={"ref": "main", "repo": {"full_name": "o/r", "url": REPO_URL}},
    )
    payload.update(overrides)
    return payload


def _cross_reference(pr: int = 5, repository_url: str = REPO_URL) -> dict[str, Any]:
    return {
        "event": "cross-referenced",
        "source": {
            "type": "issue",
            "issue": {
                "number": pr,
                "pull_request": {"merged_at": MERGED_AT},
                "repository_url": repository_url,
            },
        },
    }


def _closed(at: str = "2026-01-02T00:00:02Z", actor: str = "merger") -> dict[str, Any]:
    return {"event": "closed", "created_at": at, "actor": {"login": actor}}


def _closing_events() -> list[dict[str, Any]]:
    return [_cross_reference(), _closed()]


def _merged_rest(
    pull: dict[str, Any] | None = None,
    timelines: dict[int, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    rest = _rest(pull=pull or _merged_pull())
    for issue, events in (timelines or {42: _closing_events()}).items():
        # Two pages, so a verdict cannot depend on reading only the first page.
        rest[_timeline_path(issue)] = [events[:1], events[1:]]
    return rest


class TestMergedClosingEvidence:
    """#1341: REST proves that this merge closed an Issue, never the full list."""

    def test_issue_closed_by_this_merge_is_verified(self, forge: GitHubForge, fake_gh):
        fake_gh(rest=_merged_rest())

        pr = forge.get_pull_request(5)

        assert pr.closes_issue_numbers == (42,)
        # Only a verified subset: the full list is still unknown.
        assert pr.closing_references_known is False

    @pytest.mark.parametrize(
        "body",
        ["closes #42", "Fixes: #42", "RESOLVED #42", "fixed  #42.", "close #42\n"],
    )
    def test_closing_keywords(self, forge: GitHubForge, fake_gh, body: str):
        fake_gh(rest=_merged_rest(pull=_merged_pull(body=body)))

        assert forge.get_pull_request(5).closes_issue_numbers == (42,)

    @pytest.mark.parametrize(
        "body",
        ["Refs #42", "Closes o/r#42", "Closes https://github.com/o/r/issues/42", ""],
    )
    def test_body_without_a_short_closing_reference_is_not_verified(
        self, forge: GitHubForge, fake_gh, body: str
    ):
        fake = fake_gh(rest=_merged_rest(pull=_merged_pull(body=body)))

        assert forge.get_pull_request(5).closes_issue_numbers == ()
        assert not any("timeline" in arg for call in fake.calls for arg in call)

    @pytest.mark.parametrize(
        "events",
        [
            [_closed()],
            [_cross_reference(pr=6), _closed()],
            [
                _cross_reference(repository_url="https://api.github.com/repos/x/y"),
                _closed(),
            ],
            [_cross_reference(), _closed(at="2026-01-01T23:59:59Z")],
            [_cross_reference(), _closed(at="2026-01-02T00:05:00Z")],
            [_cross_reference(), _closed(actor="someone-else")],
            [
                _cross_reference(),
                _closed(),
                {"event": "reopened", "created_at": "2026-01-03T00:00:00Z"},
            ],
            [_cross_reference()],
        ],
        ids=[
            "no-cross-reference",
            "cross-reference-from-another-pr",
            "cross-reference-from-another-repository",
            "closed-before-merge",
            "closed-long-after-merge",
            "closed-by-someone-else",
            "reopened-later",
            "not-closed",
        ],
    )
    def test_missing_evidence_is_not_verified(
        self, forge: GitHubForge, fake_gh, events: list[dict[str, Any]]
    ):
        fake_gh(rest=_merged_rest(timelines={42: events}))

        assert forge.get_pull_request(5).closes_issue_numbers == ()

    def test_cross_reference_must_come_from_a_pull_request(
        self, forge: GitHubForge, fake_gh
    ):
        reference = _cross_reference()
        del reference["source"]["issue"]["pull_request"]
        fake_gh(rest=_merged_rest(timelines={42: [reference, _closed()]}))

        assert forge.get_pull_request(5).closes_issue_numbers == ()

    def test_only_the_verified_issues_are_reported(self, forge: GitHubForge, fake_gh):
        fake_gh(
            rest=_merged_rest(
                pull=_merged_pull(body="Closes #42, fixes #43 and resolves #44"),
                timelines={
                    42: _closing_events(),
                    43: [_closed()],
                    44: _closing_events(),
                },
            )
        )

        assert forge.get_pull_request(5).closes_issue_numbers == (42, 44)

    def test_candidates_beyond_the_limit_are_not_checked(
        self, forge: GitHubForge, fake_gh
    ):
        body = " ".join(f"Closes #{n}" for n in range(1, 12))
        fake = fake_gh(
            rest=_merged_rest(
                pull=_merged_pull(body=body),
                timelines={n: _closing_events() for n in range(1, 12)},
            )
        )

        assert forge.get_pull_request(5).closes_issue_numbers == tuple(range(1, 11))
        assert not any(_timeline_path(11) in call for call in fake.calls)

    @pytest.mark.parametrize(
        "rest_errors",
        [{_timeline_path(42): "gh: Server Error (HTTP 502)"}, {}],
        ids=["error", "malformed"],
    )
    def test_unreadable_timeline_is_not_evidence(
        self, forge: GitHubForge, fake_gh, rest_errors: dict[str, str]
    ):
        rest = _merged_rest()
        if not rest_errors:
            rest[_timeline_path(42)] = {"message": "not a page list"}
        fake_gh(rest=rest, rest_errors=rest_errors)

        pr = forge.get_pull_request(5)

        assert pr.state == "MERGED"
        assert pr.closes_issue_numbers == ()

    def test_missing_merger_is_not_evidence(self, forge: GitHubForge, fake_gh):
        fake_gh(rest=_merged_rest(pull=_merged_pull(merged_by=None)))

        assert forge.get_pull_request(5).closes_issue_numbers == ()

    def test_unmerged_pull_request_never_reads_timelines(
        self, forge: GitHubForge, fake_gh
    ):
        fake = fake_gh(rest=_merged_rest(pull=_pull()))

        assert forge.get_pull_request(5).closes_issue_numbers == ()
        assert not any("timeline" in arg for call in fake.calls for arg in call)
