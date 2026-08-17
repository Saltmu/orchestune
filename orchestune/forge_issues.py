"""GitHub Issue operations used by the composed Forge facade."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from typing import Protocol

from orchestune.models import IssueRecord
from orchestune.validation import (
    validate_issue_number,
    validate_label,
    validate_username,
)

_VALID_ISSUE_STATES = frozenset({"open", "closed", "all"})
_VALID_CLOSE_REASONS = frozenset({"completed", "not planned"})

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


class _Runner(Protocol):
    def __call__(self, args: list[str], input_text: str | None = None) -> str: ...


class GitHubIssueMixin:
    """Issue-specific implementation mixed into :class:`GitHubForge`."""

    _run: _Runner

    def list_issues_by_label(
        self, label: str, state: str = "open", limit: int = 1000
    ) -> list[IssueRecord]:
        """ラベルと状態でIssueを取得する。"""
        validate_label(label)
        if state not in _VALID_ISSUE_STATES:
            raise ValueError(f"stateが不正です: {state!r}")
        stdout = self._run(
            [
                "gh",
                "issue",
                "list",
                "--label",
                label,
                "--state",
                state,
                "--limit",
                str(limit),
                "--json",
                "number,title,body,labels,createdAt,parent,blockedBy,state",
            ]
        )
        raw_issues = json.loads(stdout)
        return [
            IssueRecord(
                number=raw["number"],
                title=raw["title"],
                body=raw["body"],
                labels=tuple(entry["name"] for entry in raw.get("labels", [])),
                created_at=raw["createdAt"],
                state=raw.get("state", "OPEN"),
                parent=raw.get("parent"),
                blocked_by=tuple(
                    node["number"] for node in raw.get("blockedBy", {}).get("nodes", [])
                ),
            )
            for raw in raw_issues
        ]

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

    def add_label(self, issue_number: int | str, label: str) -> None:
        number = validate_issue_number(issue_number)
        validate_label(label)
        self._run(["gh", "issue", "edit", str(number), "--add-label", label])

    def remove_label(self, issue_number: int | str, label: str) -> None:
        number = validate_issue_number(issue_number)
        validate_label(label)
        self._run(["gh", "issue", "edit", str(number), "--remove-label", label])

    def close_issue(
        self, issue_number: int | str, reason: str, comment: str | None = None
    ) -> None:
        number = validate_issue_number(issue_number)
        if reason not in _VALID_CLOSE_REASONS:
            raise ValueError(f"reasonが不正です: {reason!r}")
        args = ["gh", "issue", "close", str(number), "--reason", reason]
        if comment is not None:
            args.extend(["--comment", comment])
        self._run(args)

    def add_comment(self, issue_number: int | str, body: str) -> None:
        number = validate_issue_number(issue_number)
        self._run(
            ["gh", "issue", "comment", str(number), "--body-file", "-"],
            input_text=body,
        )

    def get_issue_state(self, issue_number: int | str) -> str:
        number = validate_issue_number(issue_number)
        stdout = self._run(["gh", "issue", "view", str(number), "--json", "state"])
        return str(json.loads(stdout).get("state", "OPEN"))

    def get_issue_labels(self, issue_number: int | str) -> tuple[str, ...]:
        number = validate_issue_number(issue_number)
        stdout = self._run(["gh", "issue", "view", str(number), "--json", "labels"])
        raw = json.loads(stdout)
        return tuple(entry["name"] for entry in raw.get("labels", []))

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

    def create_issue(self, title: str, body: str, labels: Sequence[str] = ()) -> int:
        args = ["gh", "issue", "create", "--title", title, "--body-file", "-"]
        for label in labels:
            validate_label(label)
            args.extend(["--label", label])
        stdout = self._run(args, input_text=body)
        url = stdout.strip().splitlines()[-1]
        return int(url.rstrip("/").rsplit("/", 1)[-1])

    def update_issue_body(self, issue_number: int | str, body: str) -> None:
        number = validate_issue_number(issue_number)
        self._run(
            ["gh", "issue", "edit", str(number), "--body-file", "-"],
            input_text=body,
        )

    def add_sub_issue(
        self, parent_issue_number: int | str, child_issue_number: int | str
    ) -> None:
        parent = validate_issue_number(parent_issue_number)
        child = validate_issue_number(child_issue_number)
        self._run(["gh", "issue", "edit", str(child), "--parent", str(parent)])

    def set_blocked_by(
        self, issue_number: int | str, blocking_issue_number: int | str
    ) -> None:
        number = validate_issue_number(issue_number)
        blocker = validate_issue_number(blocking_issue_number)
        self._run(
            ["gh", "issue", "edit", str(number), "--add-blocked-by", str(blocker)]
        )

    def find_open_issues_by_exact_title(self, title: str) -> list[IssueRecord]:
        stdout = self._run(
            [
                "gh",
                "issue",
                "list",
                "--search",
                f'in:title "{title}"',
                "--state",
                "open",
                "--json",
                "number,title,body,createdAt",
                "--limit",
                "100",
            ]
        )
        return [
            IssueRecord(
                number=int(entry["number"]),
                title=str(entry["title"]),
                body=entry.get("body") or "",
                labels=(),
                created_at=entry.get("createdAt") or "",
            )
            for entry in json.loads(stdout)
            if entry.get("title") == title
        ]

    def find_issues_by_parent_metadata(
        self, parent_issue_number: int | str
    ) -> list[IssueRecord]:
        """#485: ネイティブSub-issue関係が使えない環境向けのフォールバック。

        `gh issue list --search`は本文の部分文字列マッチ（`in:body`）しか
        できず"12"が"120"にもヒットするような誤検出がありうるため、
        検索結果はFootprint YAMLフェンスの`parent_issue_number`を
        `parent_issue_number_from_body`（呼び出し元）で厳密に再検証する
        前提の候補集合として返す。
        """
        number = validate_issue_number(parent_issue_number)
        stdout = self._run(
            [
                "gh",
                "issue",
                "list",
                "--search",
                f"in:body parent_issue_number: {number}",
                "--state",
                "all",
                "--json",
                "number,title,body,labels,createdAt,parent,blockedBy,state",
                "--limit",
                "1000",
            ]
        )
        raw_issues = json.loads(stdout)
        return [
            IssueRecord(
                number=raw["number"],
                title=raw["title"],
                body=raw["body"],
                labels=tuple(entry["name"] for entry in raw.get("labels", [])),
                created_at=raw["createdAt"],
                state=raw.get("state", "OPEN"),
                parent=raw.get("parent"),
                blocked_by=tuple(
                    node["number"] for node in raw.get("blockedBy", {}).get("nodes", [])
                ),
            )
            for raw in raw_issues
        ]

    def get_issue(self, issue_number: int | str) -> IssueRecord | None:
        number = validate_issue_number(issue_number)
        try:
            stdout = self._run(
                [
                    "gh",
                    "issue",
                    "view",
                    str(number),
                    "--json",
                    "number,title,body,state,labels,createdAt,parent,blockedBy",
                ]
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").lower()
            if "404" in detail or "not found" in detail:
                return None
            raise
        raw = json.loads(stdout)
        return IssueRecord(
            number=int(raw["number"]),
            title=str(raw["title"]),
            body=raw.get("body") or "",
            labels=tuple(entry["name"] for entry in raw.get("labels", [])),
            created_at=raw.get("createdAt") or "",
            state=raw.get("state", "OPEN"),
            # #485 review round 5 (P2): provisioning's reuse path needs to
            # know a candidate's *already-established* native parent (not
            # just whether it can currently re-write one) to avoid
            # wrongly concluding a legacy issue has no discovery mechanism
            # left just because the current forge can't re-assert it.
            parent=raw.get("parent"),
            blocked_by=tuple(
                node["number"] for node in raw.get("blockedBy", {}).get("nodes", [])
            ),
        )
