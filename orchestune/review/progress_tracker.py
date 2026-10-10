"""Parse the status row in Codex's pull request review tracker comment."""

from __future__ import annotations

import html
import re
from enum import StrEnum

_CODEX_TRACKER_MARKER = "<!-- codex-pull-request-review-summary -->"
_TAG_RE = re.compile(r"<[^>]*>")
_STATUS_RE = re.compile(r"^(?:running|queued|pending|in[ -]+progress)\b")


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
