"""Parse the status row in Codex's pull request review tracker comment."""

from __future__ import annotations

import html
import re
from enum import StrEnum

_CODEX_TRACKER_MARKER = "<!-- codex-pull-request-review-summary -->"
_TAG_RE = re.compile(r"<[^>]*>")
_STATUS_RE = re.compile(r"^(?:running|queued|pending|in[ -]+progress)\b")
_COMMIT_RE = re.compile(r"[0-9a-fA-F]{7,40}")
_COMMIT_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


class CodexTrackerStatus(StrEnum):
    """The review status reported by a Codex tracker, or an unknown state."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


def _plain_cell_text(value: str) -> str:
    text = html.unescape(_TAG_RE.sub(" ", value))
    text = re.sub(r"[*_`~]", "", text)
    return " ".join(text.split())


def _row_key(value: str) -> str:
    return "".join(
        char for char in _plain_cell_text(value).casefold() if char.isalnum()
    )


def _table_cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or "|" not in stripped[1:]:
        return None
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def _status_from_cell(value: str) -> CodexTrackerStatus:
    text = _plain_cell_text(value).casefold()
    text = re.sub(r"^\W+", "", text)
    if _STATUS_RE.match(text):
        return CodexTrackerStatus.IN_PROGRESS
    if re.match(r"^completed\b", text):
        return CodexTrackerStatus.COMPLETED
    return CodexTrackerStatus.UNKNOWN


def parse_codex_tracker_status(body: str) -> CodexTrackerStatus | None:
    """Return the Code Review row's status, or ``None`` for a non-tracker body.

    A present marker with a missing, ambiguous, or unsupported row is an
    ``UNKNOWN`` tracker. Only a recognized status in the Code Review status
    cell can report in-progress or completed; unrelated prose and rows do not
    affect the result.
    """
    if _CODEX_TRACKER_MARKER not in body:
        return None

    rows = [
        cells
        for line in body.splitlines()
        if (cells := _table_cells(line)) is not None
        and cells
        and _row_key(cells[0]) == "codereview"
    ]
    if len(rows) != 1 or len(rows[0]) < 2:
        return CodexTrackerStatus.UNKNOWN
    return _status_from_cell(rows[0][1])


def parse_codex_tracker_commit(body: str) -> str | None:
    """Return the Code Review row's lowercase commit prefix, else ``None``.

    The value is a 7-40 digit hexadecimal prefix read from the table's
    ``Commit`` column. A non-tracker body, a missing or duplicated column, or
    an invalid cell is ``None``: the commit is an activity-correlation hint,
    never proof of which commit was reviewed.
    """
    if _CODEX_TRACKER_MARKER not in body:
        return None

    tables = [
        cells for line in body.splitlines() if (cells := _table_cells(line)) is not None
    ]
    headers = [
        cells for cells in tables if any(_row_key(cell) == "commit" for cell in cells)
    ]
    rows = [cells for cells in tables if cells and _row_key(cells[0]) == "codereview"]
    if len(headers) != 1 or len(rows) != 1:
        return None
    columns = [i for i, cell in enumerate(headers[0]) if _row_key(cell) == "commit"]
    if len(columns) != 1 or columns[0] >= len(rows[0]):
        return None
    cell = _COMMIT_LINK_RE.sub(r"\1", rows[0][columns[0]])
    text = _plain_cell_text(cell)
    return text.lower() if _COMMIT_RE.fullmatch(text) else None
