"""Read-only census of open blocked-human-review issue comments."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from orchestune.blocked_report import (
    CATEGORIES,
    BlockedIssueTriage,
    build_triage_entry,
    classify_comments,
)
from orchestune.forge import GitHubForge
from orchestune.infra.cli_guidance import quote_command
from orchestune.infra.cli_output import CliOutput, make_output
from orchestune.issue_parsing import find_children_by_parent
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord

_LIST_LIMIT = 1000
_LABEL = StatusLabel.BLOCKED_HUMAN_REVIEW
_NO_ISSUES = "対象の Issue はありません"
_NO_COMMENTS = "コメントなし"
_TABLE_HEADERS = ("Issue", "Title", "Categories", "Next Command")


class BlockedReadError(Exception):
    """A census read failed. The message names the target and the reason."""


@dataclass(frozen=True)
class _Census:
    totals: Mapping[str, int]
    lines: tuple[str, ...]
    rows: tuple[tuple[str, str, str, str], ...]


def _positive_issue(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from error
    if number < 1:
        raise argparse.ArgumentTypeError(f"{value!r} must be a positive integer")
    return number


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
    parser.add_argument(
        "-d",
        "--detail",
        action="store_true",
        help="Print a triage table with titles and next commands.",
    )
    parser.add_argument(
        "-p",
        "--parent-issue",
        type=_positive_issue,
        default=None,
        help="Limit the census to children of this parent issue.",
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


def _child_numbers(forge: GitHubForge, parent: int) -> set[int]:
    try:
        found = find_children_by_parent(forge, parent)
        return {issue.number for issue in found.issues}
    except Exception as error:  # noqa: BLE001 - boundary: parent lookup failure stops the census
        raise BlockedReadError(
            f"blocked: failed to list child issues for parent {parent}: {error}"
        ) from error


def _matching_issues(forge: GitHubForge, parent: int | None) -> list[IssueRecord]:
    records = _issues(forge)
    if parent is None:
        return records
    children = _child_numbers(forge, parent)
    return [record for record in records if record.number in children]


def _category_text(entry: BlockedIssueTriage) -> str:
    if not entry.has_comments:
        return _NO_COMMENTS
    return ", ".join(
        f"{name} {entry.counts[name]}"
        for name in CATEGORIES
        if entry.counts.get(name, 0)
    )


def _triage_row(
    issue: IssueRecord,
    counts: Mapping[str, int],
    *,
    has_comments: bool,
) -> tuple[str, str, str, str]:
    entry = build_triage_entry(
        issue.number,
        issue.title,
        counts,
        has_comments=has_comments,
    )
    return (
        str(entry.issue_number),
        entry.title,
        _category_text(entry),
        quote_command(entry.recommended_command),
    )


def _issue_lines(
    issue: IssueRecord,
    bodies: Sequence[str],
    totals: dict[str, int],
) -> tuple[list[str], tuple[str, str, str, str]]:
    if not bodies:
        row = _triage_row(issue, {}, has_comments=False)
        return [f"{issue.number} {_NO_COMMENTS}\n"], row
    counts = classify_comments(bodies)
    lines: list[str] = []
    for name in CATEGORIES:
        count = counts[name]
        totals[name] += count
        if count:
            lines.append(f"{issue.number} {name} {count}\n")
    return lines, _triage_row(issue, counts, has_comments=True)


def _collect(forge: GitHubForge, parent: int | None) -> _Census:
    totals = {name: 0 for name in CATEGORIES}
    issues = _matching_issues(forge, parent)
    if not issues:
        return _Census(totals, (f"{_NO_ISSUES}\n",), ())
    lines: list[str] = []
    rows: list[tuple[str, str, str, str]] = []
    for issue in issues:
        extra, row = _issue_lines(issue, _bodies(forge, issue.number), totals)
        lines.extend(extra)
        rows.append(row)
    return _Census(totals, tuple(lines), tuple(rows))


def _emit_json(out: CliOutput, totals: Mapping[str, int]) -> None:
    ordered = {name: totals[name] for name in CATEGORIES}
    out.payload(json.dumps(ordered, ensure_ascii=False))


def _emit_triage(out: CliOutput, census: _Census) -> None:
    if not census.rows:
        out.emit(out.text_block(_NO_ISSUES, multiline=False))
        return
    out.emit(out.table_block(_TABLE_HEADERS, census.rows))


def _emit(
    out: CliOutput,
    census: _Census,
    *,
    as_json: bool,
    detail: bool,
) -> None:
    if as_json:
        _emit_json(out, census.totals)
        return
    if detail or out.policy.rich:
        _emit_triage(out, census)
        return
    out.policy.stream.write("".join(census.lines))


def main(argv: list[str] | None = None, *, output: CliOutput | None = None) -> int:
    """Print the census. Forge is not constructed for help or usage errors."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.detail and args.json:
        parser.error("--detail cannot be combined with --json")
    out = output if output is not None else make_output(machine=args.json)
    try:
        census = _collect(GitHubForge(), args.parent_issue)
    except BlockedReadError as error:
        print(error, file=sys.stderr)
        return 1
    _emit(out, census, as_json=args.json, detail=args.detail)
    return 0
