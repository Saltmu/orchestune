"""GitHub Issue relationship operations used by the composed Forge facade."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

from orchestune.models import IssueRecord
from orchestune.validation import validate_issue_number

from .admin import RelationshipUnavailableError
from .graphql_fallback import GhRunner
from .graphql_fallback import error_detail as _error_detail

_SUB_ISSUES_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      subIssues(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          number
          title
          body
          state
          createdAt
          labels(first: 50) { nodes { name } }
          parent { number }
          blockedBy(first: 50) { nodes { number } }
        }
      }
    }
  }
}
"""


def _is_relationship_unavailable_error(exc: subprocess.CalledProcessError) -> bool:
    detail = _error_detail(exc)
    return any(
        marker in detail
        for marker in (
            "unknown flag",
            "unknown option",
            "not supported",
            "unknown field",
        )
    )


class GitHubIssueRelationshipMixin:
    """Sub-issue and blocked-by relationship operations mixed into :class:`GitHubForge`."""

    _run: GhRunner

    if TYPE_CHECKING:

        def get_issue(self, issue_number: int | str) -> IssueRecord | None: ...

    def list_sub_issues(self, parent_issue_number: int | str) -> list[IssueRecord]:
        """親Issue配下のサブタスクIssueをGraphQLで取得する。"""
        number = validate_issue_number(parent_issue_number)
        records: list[IssueRecord] = []
        after: str | None = None
        while True:
            args = [
                "gh",
                "api",
                "graphql",
                "-F",
                "owner={owner}",
                "-F",
                "name={repo}",
                "-F",
                f"number={number}",
                "-f",
                f"query={_SUB_ISSUES_QUERY}",
            ]
            if after is not None:
                args += ["-F", f"after={after}"]
            stdout = self._run(args)
            sub_issues = json.loads(stdout)["data"]["repository"]["issue"]["subIssues"]
            for node in sub_issues["nodes"]:
                records.append(
                    IssueRecord(
                        number=node["number"],
                        title=node["title"],
                        body=node["body"],
                        labels=tuple(
                            entry["name"]
                            for entry in node.get("labels", {}).get("nodes", [])
                        ),
                        created_at=node["createdAt"],
                        state=node.get("state", "OPEN"),
                        parent=node.get("parent"),
                        blocked_by=tuple(
                            blocked["number"]
                            for blocked in node.get("blockedBy", {}).get("nodes", [])
                        ),
                    )
                )
            page_info = sub_issues["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            after = page_info["endCursor"]
        return records

    def _get_issue_for_relationship(self, issue_number: int) -> IssueRecord | None:
        try:
            return self.get_issue(issue_number)
        except subprocess.CalledProcessError as exc:
            if _is_relationship_unavailable_error(exc):
                raise RelationshipUnavailableError(
                    "native issue relationships are unavailable"
                ) from exc
            raise

    def add_sub_issue(
        self, parent_issue_number: int | str, child_issue_number: int | str
    ) -> None:
        parent = validate_issue_number(parent_issue_number)
        child = validate_issue_number(child_issue_number)
        current = self._get_issue_for_relationship(child)
        if current is not None and current.parent is not None:
            if current.parent.get("number") == parent:
                return
        self._run_relationship_command(
            ["gh", "issue", "edit", str(child), "--parent", str(parent)]
        )

    def remove_sub_issue(
        self, parent_issue_number: int | str, child_issue_number: int | str
    ) -> None:
        parent = validate_issue_number(parent_issue_number)
        child = validate_issue_number(child_issue_number)
        current = self._get_issue_for_relationship(child)
        if current is None or current.parent is None:
            return
        if current.parent.get("number") != parent:
            raise ValueError(
                f"Issue #{child} has parent #{current.parent.get('number')}, not #{parent}"
            )
        self._run_relationship_command(
            ["gh", "issue", "edit", str(child), "--remove-parent"]
        )

    def set_blocked_by(
        self, issue_number: int | str, blocking_issue_number: int | str
    ) -> None:
        number = validate_issue_number(issue_number)
        blocker = validate_issue_number(blocking_issue_number)
        current = self._get_issue_for_relationship(number)
        if current is not None and blocker in current.blocked_by:
            return
        self._run_relationship_command(
            ["gh", "issue", "edit", str(number), "--add-blocked-by", str(blocker)]
        )

    def remove_blocked_by(
        self, issue_number: int | str, blocking_issue_number: int | str
    ) -> None:
        number = validate_issue_number(issue_number)
        blocker = validate_issue_number(blocking_issue_number)
        current = self._get_issue_for_relationship(number)
        if current is None or blocker not in current.blocked_by:
            return
        self._run_relationship_command(
            ["gh", "issue", "edit", str(number), "--remove-blocked-by", str(blocker)]
        )

    def _run_relationship_command(self, args: list[str]) -> None:
        try:
            self._run(args)
        except subprocess.CalledProcessError as exc:
            if _is_relationship_unavailable_error(exc):
                raise RelationshipUnavailableError(
                    "native issue relationships are unavailable"
                ) from exc
            raise
