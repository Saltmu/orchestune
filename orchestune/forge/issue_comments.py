"""GitHub Issue comment operations used by the composed Forge facade."""

from __future__ import annotations

import json
import subprocess
from typing import Any

from orchestune.models import normalize_newlines
from orchestune.validation import (
    validate_issue_number,
    validate_label,
    validate_username,
)

from .graphql_fallback import GhRunner


def _extract_trusted_collaborator(item: Any) -> str | None:
    if not isinstance(item, dict):
        raise ValueError("invalid collaborator schema: item is not a dict")
    login = item.get("login")
    if not isinstance(login, str) or not login:
        raise ValueError("invalid collaborator schema: missing or invalid login")
    permissions = item.get("permissions")
    if not isinstance(permissions, dict):
        raise ValueError("invalid collaborator schema: missing or invalid permissions")
    for req_key in ("pull", "push", "admin"):
        if req_key not in permissions or not isinstance(permissions[req_key], bool):
            raise ValueError(
                f"invalid collaborator schema: missing or non-boolean {req_key}"
            )
    for opt_key in ("triage", "maintain"):
        if opt_key in permissions and not isinstance(permissions[opt_key], bool):
            raise ValueError(f"invalid collaborator schema: non-boolean {opt_key}")
    if (
        permissions.get("triage") is True
        or permissions["push"] is True
        or permissions.get("maintain") is True
        or permissions["admin"] is True
    ):
        return login
    return None


class GitHubIssueCommentMixin:
    """Issue comment and actor permission operations mixed into :class:`GitHubForge`."""

    _run: GhRunner

    def add_comment(self, issue_number: int | str, body: str) -> None:
        number = validate_issue_number(issue_number)
        self._run(
            ["gh", "issue", "comment", str(number), "--body-file", "-"],
            input_text=body,
        )

    def list_comments(self, issue_number: int | str) -> list[dict[str, Any]]:
        """#552: Issue/PRのコメント一覧を取得する。"""
        number = validate_issue_number(issue_number)
        try:
            stdout = self._run(
                ["gh", "issue", "view", str(number), "--json", "comments"]
            )
        except subprocess.CalledProcessError:
            stdout = self._run(["gh", "pr", "view", str(number), "--json", "comments"])
        raw = json.loads(stdout)
        comments = raw.get("comments", [])
        return [
            {
                # #664: 本文と同様、コメントもGitHubからCRLFで返る。読み出しの
                # 時点でLFへ揃え、マーカー検索を改行コードに依存させない。
                "body": normalize_newlines(c.get("body", "")),
                "created_at": c.get("createdAt") or c.get("created_at") or "",
                "author": c.get("author", {}).get("login", "")
                if isinstance(c.get("author"), dict)
                else (c.get("author") or ""),
            }
            for c in comments
            if isinstance(c, dict)
        ]

    def list_all_issue_comments(self, issue_number: int | str) -> list[dict[str, Any]]:
        """Return every native Issue comment, including durable API evidence."""
        number = validate_issue_number(issue_number)
        stdout = self._run(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{{owner}}/{{repo}}/issues/{number}/comments?per_page=100",
            ]
        )
        pages = json.loads(stdout)
        if not isinstance(pages, list) or not all(
            isinstance(page, list) for page in pages
        ):
            raise ValueError("GitHub comments response has an invalid page")
        return [
            comment for page in pages for comment in page if isinstance(comment, dict)
        ]

    def create_issue_comment(
        self, issue_number: int | str, body: str
    ) -> dict[str, Any]:
        """Create an Issue comment and return GitHub's durable response object."""
        number = validate_issue_number(issue_number)
        stdout = self._run(
            [
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{{owner}}/{{repo}}/issues/{number}/comments",
                "--input",
                "-",
            ],
            input_text=json.dumps({"body": body}),
        )
        response = json.loads(stdout)
        if not isinstance(response, dict):
            raise ValueError("GitHub did not return an Issue comment object")
        return response

    def list_notice_trusted_authors(self) -> frozenset[str]:
        """Return the logins of collaborators trusted to post PR link notices.

        Fetches all collaborators with permissions.triage, push, maintain, or admin.
        Requires pull/push/admin as boolean values in the GitHub API schema; triage and
        maintain are optional (default to False).
        """
        stdout = self._run(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                "repos/{owner}/{repo}/collaborators?affiliation=all&per_page=100",
            ]
        )
        pages = json.loads(stdout)
        if not isinstance(pages, list) or not all(
            isinstance(page, list) for page in pages
        ):
            raise ValueError("GitHub collaborators response has an invalid page")

        trusted: set[str] = set()
        for page in pages:
            for item in page:
                login = _extract_trusted_collaborator(item)
                if login is not None:
                    trusted.add(login)

        return frozenset(trusted)

    def get_authenticated_user(self) -> str:
        """Return the GitHub identity that will author Forge writes."""
        login = self._run(["gh", "api", "user", "--jq", ".login"]).strip()
        if not login:
            raise ValueError("GitHub returned an empty authenticated user")
        return login

    def get_label_actor(self, issue_number: int | str, label: str) -> str:
        number = validate_issue_number(issue_number)
        validate_label(label)
        stdout = self._run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/issues/{number}/events",
                "--paginate",
                "--slurp",
            ]
        )
        pages = json.loads(stdout)
        events = [event for page in pages for event in page]
        labeled_actors = [
            event["actor"]["login"]
            for event in events
            if event.get("event") == "labeled"
            and event.get("label", {}).get("name") == label
        ]
        if labeled_actors:
            return str(labeled_actors[-1])
        stdout = self._run(["gh", "issue", "view", str(number), "--json", "author"])
        author = json.loads(stdout).get("author") or {}
        return str(author.get("login", ""))

    def get_actor_permission(self, username: str) -> str:
        if not username:
            return "none"
        login = validate_username(username)
        try:
            stdout = self._run(
                [
                    "gh",
                    "api",
                    f"repos/{{owner}}/{{repo}}/collaborators/{login}/permission",
                ]
            )
        except subprocess.CalledProcessError:
            return "none"
        return str(json.loads(stdout).get("permission", "none"))

    def get_issue_last_reopened_at(self, issue_number: int | str) -> str | None:
        number = validate_issue_number(issue_number)
        stdout = self._run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/issues/{number}/events",
                "--paginate",
                "--slurp",
            ]
        )
        pages = json.loads(stdout)
        events = [event for page in pages for event in page]
        reopened_at = [
            event.get("created_at")
            for event in events
            if event.get("event") == "reopened"
        ]
        return str(reopened_at[-1]) if reopened_at else None
