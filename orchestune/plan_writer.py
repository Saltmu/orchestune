"""Surgical in-place updates to `decomposition_plan.md`'s YAML frontmatter.

Only the `issue_number` / `parent_issue_number` lines this module owns are
touched; every other line (including comments and formatting) is left
byte-for-byte identical, so re-dumping the frontmatter through a YAML
serializer is deliberately avoided.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

_FRONTMATTER_DELIMITER = re.compile(r"^---\s*$")
_TITLE_LINE = re.compile(r"^title:\s*.*$")
_PARENT_ISSUE_NUMBER_LINE = re.compile(r"^(parent_issue_number:\s*).*$")
_ISSUE_NUMBER_LINE = re.compile(r"^(\s*)(issue_number:\s*).*$")


def _subtask_id_line(subtask_id: str) -> re.Pattern[str]:
    escaped = re.escape(subtask_id)
    return re.compile(rf"^(\s*)-\s*id:\s*(['\"]?){escaped}\2\s*(#.*)?$")


def _find_frontmatter_bounds(lines: list[str]) -> tuple[int, int]:
    if not lines or not _FRONTMATTER_DELIMITER.match(lines[0]):
        raise ValueError(
            "decomposition_plan.md にYAMLフロントマター（--- ... ---）が見つかりません"
        )
    for index in range(1, len(lines)):
        if _FRONTMATTER_DELIMITER.match(lines[index]):
            return 1, index
    raise ValueError(
        "decomposition_plan.md にYAMLフロントマターの終端(---)が見つかりません"
    )


def _write_parent_issue_number(
    lines: list[str], start: int, end: int, number: int
) -> int:
    for index in range(start, end):
        match = _PARENT_ISSUE_NUMBER_LINE.match(lines[index])
        if match:
            lines[index] = f"{match.group(1)}{number}\n"
            return end
    insert_at = start
    for index in range(start, end):
        if _TITLE_LINE.match(lines[index]):
            insert_at = index + 1
            break
    lines.insert(insert_at, f"parent_issue_number: {number}\n")
    return end + 1


def _write_subtask_issue_number(
    lines: list[str], start: int, end: int, subtask_id: str, number: int
) -> int:
    pattern = _subtask_id_line(subtask_id)
    for index in range(start, end):
        id_match = pattern.match(lines[index])
        if not id_match:
            continue
        block_end = index + 1
        while block_end < end and not re.match(r"^\s*-\s*id:\s*", lines[block_end]):
            existing = _ISSUE_NUMBER_LINE.match(lines[block_end])
            if existing:
                lines[block_end] = f"{existing.group(1)}{existing.group(2)}{number}\n"
                return end
            block_end += 1
        child_indent = id_match.group(1) + "  "
        lines.insert(index + 1, f"{child_indent}issue_number: {number}\n")
        return end + 1
    raise ValueError(
        f"decomposition_plan.md に該当するサブタスクが見つかりません: {subtask_id}"
    )


def write_issue_numbers(
    path: str | Path,
    subtask_issue_numbers: Mapping[str, int] | None = None,
    *,
    parent_issue_number: int | None = None,
) -> None:
    """Write resolved issue numbers back into `decomposition_plan.md` in place.

    Called once per resolved number (parent, then each subtask as it's
    provisioned) so a crash mid-run leaves already-resolved numbers durably
    persisted for the next run to reuse.
    """
    target = Path(path)
    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    start, end = _find_frontmatter_bounds(lines)

    if parent_issue_number is not None:
        end = _write_parent_issue_number(lines, start, end, parent_issue_number)

    for subtask_id, number in (subtask_issue_numbers or {}).items():
        end = _write_subtask_issue_number(lines, start, end, subtask_id, number)

    target.write_text("".join(lines), encoding="utf-8")
