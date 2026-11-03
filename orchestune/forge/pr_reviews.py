"""GitHub PR review and check operations mixed into :class:`GitHubForge`."""

from __future__ import annotations

import json
from typing import Any

import orchestune.forge.prs_rest as prs_rest
from orchestune.models import CommitCheck
from orchestune.validation import validate_issue_number

from .graphql_fallback import GhRunner


class GitHubPullRequestReviewMixin:
    """PR review and commit check queries mixed into :class:`GitHubForge`."""

    _run: GhRunner

    def list_commit_checks(self, sha: str) -> tuple[CommitCheck, ...]:
        """#1554: checks of an explicit SHA; REST only, so a moved PR head cannot leak in."""
        return prs_rest.commit_checks(self._run, sha)

    def _list_pr_review_items(
        self, pr_number: int | str, section: str
    ) -> list[dict[str, Any]]:
        number = validate_issue_number(pr_number)
        raw = json.loads(
            self._run(
                [
                    "gh",
                    "api",
                    "--paginate",
                    "--slurp",
                    f"repos/{{owner}}/{{repo}}/pulls/{number}/{section}?per_page=100",
                ]
            )
        )
        if not isinstance(raw, list) or any(not isinstance(page, list) for page in raw):
            raise ValueError("GitHub review response must contain complete pages")
        items = [item for page in raw for item in page]
        if any(not isinstance(item, dict) for item in items):
            raise ValueError("GitHub review items must be objects")
        return items

    def list_pull_request_reviews(self, pr_number: int | str) -> list[dict[str, Any]]:
        """Fetch every native PR review, failing rather than returning partial pages."""
        return self._list_pr_review_items(pr_number, "reviews")

    def list_pull_request_review_comments(
        self, pr_number: int | str
    ) -> list[dict[str, Any]]:
        """Fetch all inline review comments, with native source and review IDs."""
        return self._list_pr_review_items(pr_number, "comments")
