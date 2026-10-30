"""REST fallback for pull request evidence when GitHub GraphQL is unavailable (#1336).

`gh pr view` needs GraphQL, which Claude Code cloud sessions reject with HTTP
403. The REST API cannot report `closingIssuesReferences` or `reviewDecision`,
so a record built here marks closing references as unknown and leaves the
review decision empty instead of guessing them.

#1341: for a merged pull request, an Issue still counts as closed by it when
the Issue's timeline proves it: GitHub linked this pull request to the Issue and
the merger closed the Issue right at the merge. Only that verified subset is
reported; the full list stays unknown.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from datetime import datetime
from typing import Any

from orchestune.models import PrRecord

Runner = Callable[..., str]


def _pages(run: Runner, path: str) -> list[Any]:
    """Every item of a paginated REST list, failing on anything but complete pages."""
    raw = json.loads(run(["gh", "api", "--paginate", "--slurp", path]))
    if not isinstance(raw, list) or any(not isinstance(page, list) for page in raw):
        raise ValueError(f"GitHub returned incomplete pages for {path}")
    return [item for page in raw for item in page]


def _check_runs(run: Runner, sha: str) -> list[dict[str, object]]:
    raw = json.loads(
        run(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs?per_page=100",
            ]
        )
    )
    if not isinstance(raw, list) or any(not isinstance(page, dict) for page in raw):
        raise ValueError("GitHub returned incomplete check-run pages")
    return [
        {
            "__typename": "CheckRun",
            "status": str(check.get("status") or "").upper(),
            "conclusion": str(check.get("conclusion") or "").upper(),
        }
        for page in raw
        for check in page.get("check_runs", [])
        if isinstance(check, dict)
    ]


def _status_contexts(run: Runner, sha: str) -> list[dict[str, object]]:
    """Every context of the combined status; it pages at 30 contexts by default."""
    pages = json.loads(
        run(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{{owner}}/{{repo}}/commits/{sha}/status?per_page=100",
            ]
        )
    )
    if not isinstance(pages, list) or any(not isinstance(page, dict) for page in pages):
        raise ValueError("GitHub returned incomplete combined-status pages")
    statuses = [
        status
        for page in pages
        for status in page.get("statuses", [])
        if isinstance(status, dict)
    ]
    total = pages[0].get("total_count") if pages else 0
    if not isinstance(total, int) or len(statuses) != total:
        raise ValueError("GitHub returned an incomplete list of commit statuses")
    return [
        {"__typename": "StatusContext", "state": str(status.get("state") or "").upper()}
        for status in statuses
    ]


def status_checks(run: Runner, sha: str) -> list[dict[str, object]]:
    """Check runs and commit statuses shaped like GraphQL's statusCheckRollup."""
    return _check_runs(run, sha) + _status_contexts(run, sha)


#: GitHub's closing keywords followed by a same-repository `#N` (#1341). The
#: `owner/repo#N` and URL forms are left out, so they never become evidence.
_CLOSING_REFERENCE = re.compile(
    r"(?<![\w/#])(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)(?::\s*|\s+)#(\d+)\b",
    re.IGNORECASE,
)
#: At most this many referenced Issues are checked, one timeline read each.
_MAX_CLOSING_CANDIDATES = 10
#: GitHub closes the Issue within seconds of the merge; later is a manual close.
_CLOSE_AFTER_MERGE_SECONDS = 60


def _closing_candidates(body: str) -> list[int]:
    numbers = dict.fromkeys(int(n) for n in _CLOSING_REFERENCE.findall(body))
    return list(numbers)[:_MAX_CLOSING_CANDIDATES]


def _timestamp(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _links_this_pull(event: dict[str, Any], number: int, repository_url: str) -> bool:
    source = event.get("source")
    issue = source.get("issue") if isinstance(source, dict) else None
    return (
        event.get("event") == "cross-referenced"
        and isinstance(issue, dict)
        and issue.get("number") == number
        and isinstance(issue.get("pull_request"), dict)
        and issue.get("repository_url") == repository_url
    )


def _closed_at_merge(event: dict[str, Any], merged: datetime, merger: str) -> bool:
    if event.get("event") != "closed":
        return False
    actor = event.get("actor")
    closed = _timestamp(event.get("created_at"))
    return (
        isinstance(actor, dict)
        and actor.get("login") == merger
        and closed is not None
        and 0 <= (closed - merged).total_seconds() <= _CLOSE_AFTER_MERGE_SECONDS
    )


def _closed_by_this_merge(
    events: list[Any], number: int, repository_url: str, merged: datetime, merger: str
) -> bool:
    events = [event for event in events if isinstance(event, dict)]
    closes = [i for i, e in enumerate(events) if _closed_at_merge(e, merged, merger)]
    return (
        bool(closes)
        and any(_links_this_pull(e, number, repository_url) for e in events)
        and not any(e.get("event") == "reopened" for e in events[closes[-1] + 1 :])
    )


def _verified_closing_issues(run: Runner, raw: dict[str, Any]) -> tuple[int, ...]:
    """Issues whose timeline proves that this merge closed them (#1341)."""
    merged = _timestamp(raw.get("merged_at"))
    merger = (raw.get("merged_by") or {}).get("login")
    repository_url = ((raw.get("base") or {}).get("repo") or {}).get("url")
    if merged is None or not merger or not repository_url:
        return ()
    verified = []
    for issue in _closing_candidates(str(raw.get("body") or "")):
        path = f"repos/{{owner}}/{{repo}}/issues/{issue}/timeline?per_page=100"
        try:
            events = _pages(run, path)
        except (subprocess.CalledProcessError, ValueError):
            continue  # An unreadable timeline is no evidence, not a failed lookup.
        if _closed_by_this_merge(events, raw["number"], repository_url, merged, merger):
            verified.append(issue)
    return tuple(verified)


def pull_request_record(
    run: Runner, number: int, is_check_passing: Callable[[dict[str, object]], bool]
) -> PrRecord:
    raw = json.loads(run(["gh", "api", f"repos/{{owner}}/{{repo}}/pulls/{number}"]))
    if not isinstance(raw, dict) or raw.get("number") != number:
        raise ValueError(f"Pull request #{number} lookup returned an unusable payload")
    head = raw.get("head") or {}
    base = raw.get("base") or {}
    head_sha = str(head.get("sha") or "")
    merged_at = raw.get("merged_at") or ""
    files = _pages(run, f"repos/{{owner}}/{{repo}}/pulls/{number}/files?per_page=100")
    changed_files = tuple(str(entry["filename"]) for entry in files)
    expected_files = raw.get("changed_files")
    checks = status_checks(run, head_sha) if head_sha else []
    head_repo = head.get("repo")
    base_repo = base.get("repo") or {}
    return PrRecord(
        number=number,
        head_ref=str(head.get("ref") or ""),
        changed_files=changed_files,
        created_at=raw.get("created_at") or "",
        closed_at=raw.get("closed_at") or "",
        # The REST API has no closingIssuesReferences, so only the Issues this
        # merge provably closed are listed; see closing_references_known.
        closes_issue_numbers=_verified_closing_issues(run, raw) if merged_at else (),
        closing_references_known=False,
        review_decision="",
        is_ci_passing=bool(checks) and all(is_check_passing(c) for c in checks),
        state="MERGED" if merged_at else str(raw.get("state") or "open").upper(),
        base_ref=str(base.get("ref") or ""),
        merged_at=merged_at,
        # For an open pull request this field is GitHub's test merge, not a merge.
        merge_commit_oid=str(raw.get("merge_commit_sha") or "") if merged_at else "",
        is_cross_repository=(
            None
            if not isinstance(head_repo, dict)
            else head_repo.get("full_name") != base_repo.get("full_name")
        ),
        is_files_truncated=(
            isinstance(expected_files, int) and len(changed_files) < expected_files
        ),
        title=raw.get("title") or "",
        body=raw.get("body") or "",
        head_sha=head_sha,
    )


def close_pull_request(run: Runner, number: int) -> None:
    """Close one pull request through REST (#1440); closing a closed one is a no-op.

    A merged pull request stays merged and is reported by the caller's read-back.
    The answer is checked so an unusable payload is never taken for success.
    """
    raw = json.loads(
        run(
            [
                "gh",
                "api",
                "--method",
                "PATCH",
                f"repos/{{owner}}/{{repo}}/pulls/{number}",
                "-f",
                "state=closed",
            ]
        )
    )
    if (
        not isinstance(raw, dict)
        or raw.get("number") != number
        or raw.get("state") != "closed"
    ):
        raise ValueError(f"Pull request #{number} close returned an unusable payload")
