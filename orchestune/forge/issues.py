"""GitHub Issue operations used by the composed Forge facade."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import quote

import orchestune.forge.issues_rest as issues_rest
from orchestune.models import IssueRecord
from orchestune.validation import (
    validate_issue_number,
    validate_label,
)

from .admin import ForgeGraphQLUnavailableError
from .graphql_fallback import GhRunner, GraphQLFallbackMixin
from .graphql_fallback import error_detail as _error_detail
from .issue_comments import GitHubIssueCommentMixin
from .issue_relationships import GitHubIssueRelationshipMixin

_T = TypeVar("_T")
_ISSUE_VIEW_FIELDS = "number,title,body,state,labels,createdAt,parent,blockedBy"

_VALID_ISSUE_STATES = frozenset({"open", "closed", "all"})
_VALID_CLOSE_REASONS = frozenset({"completed", "not planned"})


@dataclass(frozen=True)
class IssueReference:
    """An Issue-or-PR number as the REST issues endpoint reports it."""

    number: int
    state: str
    is_pull_request: bool


def _is_not_found(exc: subprocess.CalledProcessError) -> bool:
    detail = _error_detail(exc)
    return "404" in detail or "not found" in detail


class GitHubIssueMixin(
    GitHubIssueCommentMixin, GitHubIssueRelationshipMixin, GraphQLFallbackMixin
):
    """Issue-specific implementation mixed into :class:`GitHubForge`."""

    _run: GhRunner

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

    def add_label(self, issue_number: int | str, label: str) -> None:
        number = validate_issue_number(issue_number)
        validate_label(label)
        self._graphql_or_rest(
            lambda: self._run(
                ["gh", "issue", "edit", str(number), "--add-label", label]
            ),
            lambda: issues_rest.add_label(self._run, number, label),
        )

    def remove_label(self, issue_number: int | str, label: str) -> None:
        number = validate_issue_number(issue_number)
        validate_label(label)
        self._graphql_or_rest(
            lambda: self._run(
                ["gh", "issue", "edit", str(number), "--remove-label", label]
            ),
            lambda: self._remove_label_rest(number, label),
        )

    def _remove_label_rest(self, number: int, label: str) -> None:
        try:
            self._run(issues_rest.remove_label_args(number, label))
        except subprocess.CalledProcessError as exc:
            # Like `gh issue edit --remove-label`, removing an absent label is a no-op.
            if not _is_not_found(exc):
                raise

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

    def get_issue_state(self, issue_number: int | str) -> str:
        number = validate_issue_number(issue_number)
        return self._graphql_or_rest(
            lambda: str(
                json.loads(
                    self._run(["gh", "issue", "view", str(number), "--json", "state"])
                ).get("state", "OPEN")
            ),
            lambda: issues_rest.issue_state(issues_rest.fetch_issue(self._run, number)),
        )

    def get_issue_labels(self, issue_number: int | str) -> tuple[str, ...]:
        number = validate_issue_number(issue_number)
        return self._graphql_or_rest(
            lambda: issues_rest.issue_labels(
                json.loads(
                    self._run(["gh", "issue", "view", str(number), "--json", "labels"])
                )
            ),
            lambda: issues_rest.issue_labels(
                issues_rest.fetch_issue(self._run, number)
            ),
        )

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
        self._graphql_or_rest(
            lambda: self._run(
                ["gh", "issue", "edit", str(number), "--body-file", "-"],
                input_text=body,
            ),
            lambda: issues_rest.update_issue_body(self._run, number, body),
        )

    def update_issue_title(self, issue_number: int | str, title: str) -> None:
        number = validate_issue_number(issue_number)
        self._run(["gh", "issue", "edit", str(number), "--title", title])

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
        return self._graphql_or_rest(
            lambda: self._get_issue_graphql(number),
            lambda: self._get_issue_rest(number),
            _ISSUE_VIEW_FIELDS,
        )

    def _get_issue_graphql(self, number: int) -> IssueRecord | None:
        try:
            stdout = self._run(
                ["gh", "issue", "view", str(number), "--json", _ISSUE_VIEW_FIELDS]
            )
        except subprocess.CalledProcessError as exc:
            if _is_not_found(exc):
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

    def _get_issue_rest(self, number: int) -> IssueRecord | None:
        try:
            raw = issues_rest.fetch_issue(self._run, number)
        except subprocess.CalledProcessError as exc:
            if _is_not_found(exc):
                return None
            raise
        try:
            return issues_rest.issue_record(self._run, number, raw)
        except subprocess.CalledProcessError as exc:
            raise ForgeGraphQLUnavailableError(
                "the REST API could not list the issue's blocked-by dependencies"
            ) from exc

    def get_issue_reference(self, issue_number: int | str) -> IssueReference | None:
        """The Issue-or-PR `issue_number`, or None when the forge confirms it is absent.

        `gh issue view` also resolves a PR number, so this reads the REST issues
        endpoint, whose payload carries a `pull_request` key only for PRs. Any
        failure other than a 404 (auth, rate limit, network) or a payload that
        lacks the identifying attributes raises: it never proves absence.
        """
        number = validate_issue_number(issue_number)
        try:
            stdout = self._run(
                ["gh", "api", f"repos/{{owner}}/{{repo}}/issues/{number}"]
            )
        except subprocess.CalledProcessError as exc:
            detail = _error_detail(exc)
            if "404" in detail or "not found" in detail:
                return None
            raise
        raw = json.loads(stdout)
        if (
            not isinstance(raw, dict)
            or raw.get("number") != number
            or not isinstance(raw.get("state"), str)
        ):
            raise ValueError(f"Issue #{number} lookup returned an unusable payload")
        return IssueReference(
            number=number,
            state=raw["state"],
            is_pull_request="pull_request" in raw,
        )

    def list_issues_in_range(
        self,
        *,
        since: str | None = None,
        state: str = "open",
        max_pages: int | None = None,
    ) -> tuple[list[IssueRecord], bool]:
        """List issues updated at or after ``since``, one REST page at a time.

        Exhaustion follows the raw page length. Pull requests are removed after
        that decision, so a page of only pull requests can still be a full page.
        """
        _validate_issue_range(state, max_pages)
        collected: list[IssueRecord] = []
        page = 1
        while True:
            raw_page = _load_issue_range_page(self._run, since, state, page)
            collected.extend(_range_records(raw_page))
            if len(raw_page) < _RANGE_PAGE_SIZE:
                return collected, True
            if max_pages is not None and page >= max_pages:
                return collected, False
            page += 1

    def get_repository_slug(self) -> str:
        """`owner/repo` of the repository the Forge commands run against."""
        slug: object = self._graphql_or_rest(
            lambda: _name_with_owner(
                json.loads(self._run(["gh", "repo", "view", "--json", "nameWithOwner"]))
            ),
            lambda: issues_rest.repository_slug(self._run),
        )
        if not isinstance(slug, str) or slug.count("/") != 1 or not slug.strip("/"):
            raise ValueError("GitHub returned an unusable repository identity")
        return slug


def _name_with_owner(raw: object) -> object:
    return raw.get("nameWithOwner") if isinstance(raw, dict) else None


_RANGE_PAGE_SIZE = 100


def _validate_issue_range(state: str, max_pages: int | None) -> None:
    if state not in _VALID_ISSUE_STATES:
        raise ValueError(f"stateが不正です: {state!r}")
    if max_pages is None:
        return
    if isinstance(max_pages, bool) or max_pages < 1:
        raise ValueError(f"max_pagesが不正です: {max_pages!r}")


def _range_query(since: str | None, state: str, page: int) -> str:
    params = [f"state={state}", f"per_page={_RANGE_PAGE_SIZE}", f"page={page}"]
    if since is not None:
        params.append("since=" + quote(since, safe=""))
    return "repos/{owner}/{repo}/issues?" + "&".join(params)


def _load_issue_range_page(
    run: GhRunner, since: str | None, state: str, page: int
) -> list[dict[str, Any]]:
    payload = json.loads(run(["gh", "api", _range_query(since, state, page)]))
    if not isinstance(payload, list) or any(
        not isinstance(item, dict) for item in payload
    ):
        raise ValueError("Issue range lookup returned an unusable payload")
    return payload


def _range_records(raw_page: list[dict[str, Any]]) -> list[IssueRecord]:
    records: list[IssueRecord] = []
    for raw in raw_page:
        record = _range_issue(raw)
        if record is not None:
            records.append(record)
    return records


def _range_issue(raw: dict[str, Any]) -> IssueRecord | None:
    if "pull_request" in raw:
        return None
    number = raw.get("number")
    if isinstance(number, bool) or not isinstance(number, int):
        raise ValueError("Issue range lookup returned an unusable payload")
    body = raw.get("body")
    created = raw.get("created_at")
    return IssueRecord(
        number=number,
        title=str(raw.get("title") or ""),
        body=body if isinstance(body, str) else "",
        labels=issues_rest.issue_labels(raw),
        created_at=created if isinstance(created, str) else "",
        state=issues_rest.issue_state(raw),
        parent=None,
        blocked_by=(),
    )
