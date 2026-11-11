"""Read-only census of open blocked-human-review issue comments."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence

from orchestune.blocked_report import CATEGORIES, classify_comments
from orchestune.forge import GitHubForge
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord

_LIST_LIMIT = 1000
_LABEL = StatusLabel.BLOCKED_HUMAN_REVIEW
_NO_ISSUES = "対象の Issue はありません"
_NO_COMMENTS = "コメントなし"


class BlockedReadError(Exception):
    """A census read failed. The message names the target and the reason."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestune blocked",
        description=(
            "Count known markers on open status:blocked-human-review issues. "
            "Read-only."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print category counts as a JSON object.",
    )
    return parser


def _issues(forge: GitHubForge) -> list[IssueRecord]:
    try:
        records = forge.list_issues_by_label(_LABEL, state="open", limit=_LIST_LIMIT)
    except Exception as error:  # noqa: BLE001 - boundary: report the list failure and stop
        raise BlockedReadError(f"blocked: failed to list issues: {error}") from error
    if not isinstance(records, list):
        raise BlockedReadError("blocked: failed to list issues: invalid issue list")
    if len(records) >= _LIST_LIMIT:
        raise BlockedReadError("blocked: issue list reached the limit of 1000")
    chosen: dict[int, IssueRecord] = {}
    for record in records:
        if not isinstance(record, IssueRecord):
            raise BlockedReadError("blocked: failed to list issues: invalid issue list")
        if record.state != "OPEN" or _LABEL not in record.labels:
            continue
        chosen.setdefault(record.number, record)
    return [chosen[number] for number in sorted(chosen)]


def _bodies(forge: GitHubForge, number: int) -> list[str]:
    try:
        comments = forge.list_all_issue_comments(number)
    except Exception as error:  # noqa: BLE001 - boundary: report the comment read and stop
        raise BlockedReadError(
            f"blocked: failed to read comments for issue {number}: {error}"
        ) from error
    if not isinstance(comments, list):
        raise BlockedReadError(
            f"blocked: issue {number} comment body is missing or not a string"
        )
    bodies: list[str] = []
    for comment in comments:
        body = comment.get("body") if isinstance(comment, dict) else None
        if not isinstance(body, str):
            raise BlockedReadError(
                f"blocked: issue {number} comment body is missing or not a string"
            )
        bodies.append(body)
    return bodies


def _collect(forge: GitHubForge) -> tuple[Mapping[str, int], list[str]]:
    totals = {category: 0 for category in CATEGORIES}
    issues = _issues(forge)
    if not issues:
        return totals, [f"{_NO_ISSUES}\n"]
    lines: list[str] = []
    for issue in issues:
        bodies = _bodies(forge, issue.number)
        if not bodies:
            lines.append(f"{issue.number} {_NO_COMMENTS}\n")
            continue
        counts = classify_comments(bodies)
        for category in CATEGORIES:
            count = counts[category]
            totals[category] += count
            if count:
                lines.append(f"{issue.number} {category} {count}\n")
    return totals, lines


def _emit(as_json: bool, totals: Mapping[str, int], lines: Sequence[str]) -> None:
    if as_json:
        ordered = {category: totals[category] for category in CATEGORIES}
        print(json.dumps(ordered, ensure_ascii=False))
        return
    sys.stdout.write("".join(lines))


def main(argv: list[str] | None = None) -> int:
    """Print the census. Forge is not constructed for help or usage errors."""
    args = _parser().parse_args(argv)
    try:
        totals, lines = _collect(GitHubForge())
    except BlockedReadError as error:
        print(error, file=sys.stderr)
        return 1
    _emit(args.json, totals, lines)
    return 0
