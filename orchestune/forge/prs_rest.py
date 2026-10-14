"""REST fallback for pull request evidence when GitHub GraphQL is unavailable (#1336).

`gh pr view` needs GraphQL, which Claude Code cloud sessions reject with HTTP
403. The REST API cannot report `closingIssuesReferences` or `reviewDecision`,
so a record built here marks closing references as unknown and leaves the
review decision empty instead of guessing them.
"""

from __future__ import annotations

import json
from collections.abc import Callable
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
    raw = json.loads(
        run(["gh", "api", f"repos/{{owner}}/{{repo}}/commits/{sha}/status"])
    )
    statuses = raw.get("statuses") if isinstance(raw, dict) else None
    if not isinstance(statuses, list):
        raise ValueError("GitHub returned an unusable combined status")
    return [
        {"__typename": "StatusContext", "state": str(status.get("state") or "").upper()}
        for status in statuses
        if isinstance(status, dict)
    ]


def status_checks(run: Runner, sha: str) -> list[dict[str, object]]:
    """Check runs and commit statuses shaped like GraphQL's statusCheckRollup."""
    return _check_runs(run, sha) + _status_contexts(run, sha)


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
        # The REST API has no closingIssuesReferences; see closing_references_known.
        closes_issue_numbers=(),
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
