"""#1334: head SHA and repository lookups use REST, which works without GraphQL.

Claude Code cloud sessions reject GitHub GraphQL (`gh pr view` / `gh repo view`),
which left `review_target_sha` unknown and blocked the done gate.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from scripts import wait_for_review

SHA = "a" * 40


def test_pr_head_sha_reads_the_rest_pull_request() -> None:
    with patch.object(wait_for_review, "_run_gh", return_value=f"{SHA}\n") as gh:
        assert wait_for_review._fetch_pr_head_sha(12) == SHA

    gh.assert_called_once_with(
        ["api", "repos/{owner}/{repo}/pulls/12", "--jq", ".head.sha"]
    )


def test_repository_slug_reads_the_rest_repository() -> None:
    with patch.object(wait_for_review, "_run_gh", return_value="org/repo\n") as gh:
        assert wait_for_review._fetch_repository_slug() == "org/repo"

    gh.assert_called_once_with(["api", "repos/{owner}/{repo}", "--jq", ".full_name"])


@pytest.mark.parametrize(
    "lookup",
    [
        lambda: wait_for_review._fetch_pr_head_sha(12),
        wait_for_review._fetch_repository_slug,
    ],
    ids=["head-sha", "repository"],
)
@pytest.mark.parametrize(
    "outcome",
    [RuntimeError("gh command failed"), "", "   \n"],
    ids=["gh-failure", "empty", "blank"],
)
def test_lookup_failure_or_empty_output_means_unknown(lookup, outcome) -> None:
    kwargs = (
        {"side_effect": outcome}
        if isinstance(outcome, Exception)
        else {"return_value": outcome}
    )
    with patch.object(wait_for_review, "_run_gh", **kwargs):
        assert lookup() is None
