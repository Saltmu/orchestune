"""#1334: `GitHubForge` falls back to the REST API when GitHub GraphQL is unavailable.

Claude Code cloud sessions answer every GraphQL-backed `gh issue`/`gh repo`
subcommand with HTTP 403, and older `gh` releases reject the `parent` /
`blockedBy` JSON fields locally. The claim-path operations switch to `gh api`
REST endpoints in that case, and relationship data the REST API cannot confirm
fails closed instead of reading as "no dependencies".
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from orchestune.forge import ForgeGraphQLUnavailableError, GitHubForge
from orchestune.models import IssueRecord

GRAPHQL_403 = (
    "HTTP 403: GitHub GraphQL is not available from Claude Code sessions; use the "
    "REST API (gh api repos/{owner}/{repo}/...). (https://api.github.com/graphql)"
)
UNKNOWN_PARENT_FIELD = 'Unknown JSON field: "parent"\nAvailable fields:\n  assignees\n'
ISSUE_PATH = "repos/{owner}/{repo}/issues/42"


def _failed(args: list[str], stderr: str) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, args, output="", stderr=stderr)


def _ok(args: list[str], stdout: Any = "") -> subprocess.CompletedProcess[Any]:
    return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")


class _FakeGh:
    """Answer GraphQL-backed subcommands with a failure and `gh api` from a table."""

    def __init__(
        self,
        rest: dict[str, Any],
        graphql_error: str = GRAPHQL_403,
        rest_errors: dict[str, str] | None = None,
    ) -> None:
        self.rest = rest
        self.graphql_error = graphql_error
        self.rest_errors = rest_errors or {}
        self.calls: list[list[str]] = []
        self.inputs: list[bytes | None] = []

    def __call__(self, args: list[str], **kwargs: Any) -> Any:
        self.calls.append(list(args))
        self.inputs.append(kwargs.get("input"))
        stdin_mode = "input" in kwargs
        if args[1] != "api":
            if stdin_mode:
                return subprocess.CompletedProcess(
                    args, 1, stdout=b"", stderr=self.graphql_error.encode()
                )
            raise _failed(args, self.graphql_error)
        key = next(
            (arg for arg in args[2:] if arg.startswith("repos/")),
            args[-1],
        )
        if key in self.rest_errors:
            if stdin_mode:
                return subprocess.CompletedProcess(
                    args, 1, stdout=b"", stderr=self.rest_errors[key].encode()
                )
            raise _failed(args, self.rest_errors[key])
        value = self.rest.get(key, "")
        stdout = value if isinstance(value, str) else json.dumps(value)
        return _ok(args, stdout.encode() if stdin_mode else stdout)

    @property
    def non_api_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[1] != "api"]


@pytest.fixture
def fake_gh(gh_run) -> Callable[..., _FakeGh]:
    def install(**kwargs: Any) -> _FakeGh:
        fake = _FakeGh(**kwargs)
        gh_run.side_effect = fake
        return fake

    return install


def _issue(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "number": 42,
        "title": "t",
        "body": "b",
        "state": "open",
        "labels": [{"name": "status:queued"}],
        "created_at": "2026-01-01T00:00:00Z",
        "issue_dependencies_summary": {"total_blocked_by": 0},
        "sub_issues_summary": {"total": 0, "completed": 0, "percent_completed": 0},
    }
    payload.update(overrides)
    return payload


class TestGetIssueFallback:
    def test_graphql_403_reads_issue_parent_and_dependencies_from_rest(
        self, forge: GitHubForge, fake_gh
    ):
        fake = fake_gh(
            rest={
                ISSUE_PATH: _issue(
                    parent_issue_url="https://api.github.com/repos/o/r/issues/100",
                    issue_dependencies_summary={"total_blocked_by": 2},
                ),
                f"{ISSUE_PATH}/dependencies/blocked_by?per_page=100": [
                    [{"number": 10}],
                    [{"number": 11}],
                ],
            }
        )

        result = forge.get_issue(42)

        assert result == IssueRecord(
            number=42,
            title="t",
            body="b",
            labels=("status:queued",),
            created_at="2026-01-01T00:00:00Z",
            state="OPEN",
            parent={"number": 100},
            blocked_by=(10, 11),
        )
        assert fake.calls[0][:3] == ["gh", "issue", "view"]
        assert fake.calls[1] == ["gh", "api", ISSUE_PATH]
        assert fake.calls[2][:4] == ["gh", "api", "--paginate", "--slurp"]

    def test_unknown_parent_field_also_switches_to_rest(
        self, forge: GitHubForge, fake_gh
    ):
        fake_gh(rest={ISSUE_PATH: _issue()}, graphql_error=UNKNOWN_PARENT_FIELD)

        result = forge.get_issue(42)

        assert result is not None
        assert result.parent is None
        assert result.blocked_by == ()

    def test_unknown_parent_field_does_not_switch_other_operations(
        self, forge: GitHubForge, fake_gh
    ):
        """GraphQL itself still works there; only the unsupported field needs REST."""
        fake = fake_gh(rest={ISSUE_PATH: _issue()}, graphql_error=UNKNOWN_PARENT_FIELD)

        forge.get_issue(42)
        with pytest.raises(subprocess.CalledProcessError):
            forge.get_issue_state(42)

        assert fake.calls[-1][:3] == ["gh", "issue", "view"]

    def test_unknown_field_without_relationship_fields_is_not_a_fallback(
        self, forge: GitHubForge, fake_gh
    ):
        fake_gh(rest={ISSUE_PATH: _issue()}, graphql_error=UNKNOWN_PARENT_FIELD)

        with pytest.raises(subprocess.CalledProcessError):
            forge.get_issue_labels(42)

    def test_rest_404_means_missing_issue(self, forge: GitHubForge, fake_gh):
        fake_gh(rest={}, rest_errors={ISSUE_PATH: "gh: Not Found (HTTP 404)"})

        assert forge.get_issue(42) is None

    def test_rest_failure_other_than_404_propagates(self, forge: GitHubForge, fake_gh):
        fake_gh(rest={}, rest_errors={ISSUE_PATH: "gh: rate limit exceeded"})

        with pytest.raises(subprocess.CalledProcessError):
            forge.get_issue(42)

    @pytest.mark.parametrize(
        "payload",
        [
            _issue(issue_dependencies_summary=None),
            _issue(issue_dependencies_summary={"blocked_by": 0}),
            _issue(parent_issue_url="https://api.github.com/repos/o/r/issues/x"),
            # Without sub-issue support a missing parent_issue_url proves nothing.
            _issue(sub_issues_summary=None),
        ],
        ids=["no-summary", "no-total", "bad-parent-url", "no-sub-issue-support"],
    )
    def test_unverifiable_relationships_fail_closed(
        self, forge: GitHubForge, fake_gh, payload: dict[str, Any]
    ):
        fake_gh(rest={ISSUE_PATH: payload})

        with pytest.raises(ForgeGraphQLUnavailableError):
            forge.get_issue(42)

    def test_dependency_count_mismatch_fails_closed(self, forge: GitHubForge, fake_gh):
        fake_gh(
            rest={
                ISSUE_PATH: _issue(issue_dependencies_summary={"total_blocked_by": 2}),
                f"{ISSUE_PATH}/dependencies/blocked_by?per_page=100": [
                    [{"number": 10}]
                ],
            }
        )

        with pytest.raises(ForgeGraphQLUnavailableError):
            forge.get_issue(42)

    def test_dependency_lookup_failure_fails_closed(self, forge: GitHubForge, fake_gh):
        dependencies = f"{ISSUE_PATH}/dependencies/blocked_by?per_page=100"
        fake_gh(
            rest={
                ISSUE_PATH: _issue(issue_dependencies_summary={"total_blocked_by": 1})
            },
            rest_errors={dependencies: "gh: Server Error (HTTP 502)"},
        )

        with pytest.raises(ForgeGraphQLUnavailableError):
            forge.get_issue(42)

    def test_switch_is_remembered_by_the_forge_instance(
        self, forge: GitHubForge, fake_gh
    ):
        fake = fake_gh(rest={ISSUE_PATH: _issue()})

        forge.get_issue(42)
        forge.get_issue(42)
        forge.get_issue_state(42)

        assert len(fake.non_api_calls) == 1

    def test_other_graphql_failures_are_not_treated_as_unavailable(
        self, forge: GitHubForge, fake_gh
    ):
        fake = fake_gh(rest={ISSUE_PATH: _issue()}, graphql_error="gh: rate limit")

        with pytest.raises(subprocess.CalledProcessError):
            forge.get_issue(42)
        assert fake.non_api_calls == fake.calls


class TestIssueReadFallback:
    def test_state_and_labels_come_from_the_rest_issue(
        self, forge: GitHubForge, fake_gh
    ):
        fake_gh(rest={ISSUE_PATH: _issue(state="closed")})

        assert forge.get_issue_state(42) == "CLOSED"
        assert forge.get_issue_labels(42) == ("status:queued",)

    def test_repository_slug_comes_from_the_rest_repository(
        self, forge: GitHubForge, fake_gh
    ):
        fake = fake_gh(rest={"repos/{owner}/{repo}": "Saltmu/orchestune\n"})

        assert forge.get_repository_slug() == "Saltmu/orchestune"
        assert fake.calls[-1] == [
            "gh",
            "api",
            "repos/{owner}/{repo}",
            "--jq",
            ".full_name",
        ]


class TestIssueWriteFallback:
    def test_add_label_posts_to_the_labels_endpoint(self, forge: GitHubForge, fake_gh):
        fake = fake_gh(rest={})

        forge.add_label(42, "status:in-progress")

        assert fake.calls[-1] == [
            "gh",
            "api",
            "--method",
            "POST",
            f"{ISSUE_PATH}/labels",
            "-f",
            "labels[]=status:in-progress",
        ]

    def test_remove_label_deletes_the_encoded_label(self, forge: GitHubForge, fake_gh):
        fake = fake_gh(rest={})

        forge.remove_label(42, "status:queued")

        assert fake.calls[-1] == [
            "gh",
            "api",
            "--method",
            "DELETE",
            f"{ISSUE_PATH}/labels/status%3Aqueued",
        ]

    def test_removing_an_absent_label_is_not_an_error(
        self, forge: GitHubForge, fake_gh
    ):
        fake_gh(
            rest={},
            rest_errors={
                f"{ISSUE_PATH}/labels/status%3Aqueued": "gh: Label does not exist (HTTP 404)"
            },
        )

        forge.remove_label(42, "status:queued")

    def test_remove_label_propagates_other_failures(self, forge: GitHubForge, fake_gh):
        fake_gh(
            rest={},
            rest_errors={
                f"{ISSUE_PATH}/labels/status%3Aqueued": "gh: Server Error (HTTP 502)"
            },
        )

        with pytest.raises(subprocess.CalledProcessError):
            forge.remove_label(42, "status:queued")

    def test_update_issue_body_patches_lf_normalized_json(
        self, forge: GitHubForge, fake_gh
    ):
        fake = fake_gh(rest={})

        forge.update_issue_body(42, "line1\r\nline2 ✓")

        assert fake.calls[-1] == [
            "gh",
            "api",
            "--method",
            "PATCH",
            ISSUE_PATH,
            "--input",
            "-",
        ]
        sent = fake.inputs[-1]
        assert isinstance(sent, bytes)
        assert json.loads(sent.decode("utf-8")) == {"body": "line1\nline2 ✓"}


class TestGraphQLPathUnchanged:
    def test_successful_graphql_never_calls_rest(self, forge: GitHubForge, gh_run):
        gh_run.stdout('{"state": "OPEN"}')

        assert forge.get_issue_state(42) == "OPEN"
        assert gh_run.call_args.args[0] == [
            "gh",
            "issue",
            "view",
            "42",
            "--json",
            "state",
        ]
