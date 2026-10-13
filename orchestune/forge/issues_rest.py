"""REST fallbacks for Issue operations when GitHub GraphQL is unavailable (#1334).

Claude Code cloud sessions reject GitHub GraphQL with HTTP 403 while the REST
API keeps working, and older `gh` releases reject the GraphQL-only
`parent`/`blockedBy` JSON fields before any request. `GitHubIssueMixin` tries
its usual `gh issue`/`gh repo` subcommands first and switches to these helpers
only after such a failure. Values the REST API cannot confirm completely raise
`ForgeGraphQLUnavailableError` instead of being reported as empty.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from orchestune.models import IssueRecord, normalize_newlines

from .admin import ForgeGraphQLUnavailableError

Runner = Callable[..., str]

_GRAPHQL_ONLY_ISSUE_FIELDS = ("parent", "blockedby")


def is_graphql_unavailable(detail: str) -> bool:
    """Whether a lower-cased `gh` failure means GraphQL is rejected outright."""
    if "graphql is not available" in detail:
        return True
    return "http 403" in detail and "api.github.com/graphql" in detail


def is_unsupported_issue_field(detail: str, requested_fields: str) -> bool:
    """Whether this `gh` release rejected a requested `parent`/`blockedBy` field.

    Only the one call needs REST then; GraphQL itself still works.
    """
    fields = requested_fields.lower()
    return "unknown json field" in detail and any(
        field in fields for field in _GRAPHQL_ONLY_ISSUE_FIELDS
    )


def _issue_path(number: int) -> str:
    return f"repos/{{owner}}/{{repo}}/issues/{number}"


def fetch_issue(run: Runner, number: int) -> dict[str, Any]:
    raw = json.loads(run(["gh", "api", _issue_path(number)]))
    if not isinstance(raw, dict) or raw.get("number") != number:
        raise ValueError(f"Issue #{number} lookup returned an unusable payload")
    return raw


def issue_state(raw: dict[str, Any]) -> str:
    return str(raw.get("state") or "open").upper()


def issue_labels(raw: dict[str, Any]) -> tuple[str, ...]:
    return tuple(entry["name"] for entry in raw.get("labels", []))


def _parent(raw: dict[str, Any]) -> dict[str, int] | None:
    url = raw.get("parent_issue_url")
    if url is None:
        # github.com omits `parent_issue_url` for issues without a parent; the
        # omission proves "no parent" only where sub-issues are supported.
        if not isinstance(raw.get("sub_issues_summary"), dict):
            raise ForgeGraphQLUnavailableError(
                "the REST API did not report whether the issue has a parent"
            )
        return None
    tail = url.rstrip("/").rsplit("/", 1)[-1] if isinstance(url, str) else ""
    if not tail.isdigit():
        raise ForgeGraphQLUnavailableError(
            "the REST API returned an unusable parent issue reference"
        )
    return {"number": int(tail)}


def _blocked_by(run: Runner, number: int, raw: dict[str, Any]) -> tuple[int, ...]:
    summary = raw.get("issue_dependencies_summary")
    total = summary.get("total_blocked_by") if isinstance(summary, dict) else None
    if not isinstance(total, int) or isinstance(total, bool):
        raise ForgeGraphQLUnavailableError(
            "the REST API did not report the issue's blocked-by dependencies"
        )
    if total == 0:
        return ()
    pages = json.loads(
        run(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"{_issue_path(number)}/dependencies/blocked_by?per_page=100",
            ]
        )
    )
    numbers = [
        entry.get("number") if isinstance(entry, dict) else None
        for page in (pages if isinstance(pages, list) else [None])
        for entry in (page if isinstance(page, list) else [None])
    ]
    if len(numbers) != total or not all(
        isinstance(value, int) and not isinstance(value, bool) for value in numbers
    ):
        raise ForgeGraphQLUnavailableError(
            "the REST API returned an incomplete blocked-by dependency list"
        )
    return tuple(numbers)  # type: ignore[arg-type]


def issue_record(run: Runner, number: int, raw: dict[str, Any]) -> IssueRecord:
    return IssueRecord(
        number=number,
        title=str(raw.get("title") or ""),
        body=raw.get("body") or "",
        labels=issue_labels(raw),
        created_at=raw.get("created_at") or "",
        state=issue_state(raw),
        parent=_parent(raw),
        blocked_by=_blocked_by(run, number, raw),
    )


def add_label(run: Runner, number: int, label: str) -> None:
    run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"{_issue_path(number)}/labels",
            "-f",
            f"labels[]={label}",
        ]
    )


def remove_label_args(number: int, label: str) -> list[str]:
    return [
        "gh",
        "api",
        "--method",
        "DELETE",
        f"{_issue_path(number)}/labels/{quote(label, safe='')}",
    ]


def update_issue_body(run: Runner, number: int, body: str) -> None:
    payload = json.dumps({"body": normalize_newlines(body)}, ensure_ascii=False)
    run(
        ["gh", "api", "--method", "PATCH", _issue_path(number), "--input", "-"],
        input_text=payload,
    )


def repository_slug(run: Runner) -> str:
    return run(["gh", "api", "repos/{owner}/{repo}", "--jq", ".full_name"]).strip()
