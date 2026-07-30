"""Surgical in-place updates to `decomposition_plan.md`'s YAML frontmatter.

Only the `issue_number` / `parent_issue_number` lines this module owns are
touched; every other line (including comments and formatting) is left
byte-for-byte identical, so re-dumping the frontmatter through a YAML
serializer is deliberately avoided.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path

import yaml

_FRONTMATTER_DELIMITER = re.compile(r"^---\s*$")
_TITLE_LINE = re.compile(r"^title:\s*.*$")
_PARENT_ISSUE_NUMBER_LINE = re.compile(r"^(parent_issue_number:\s*).*$")
_ISSUE_NUMBER_LINE = re.compile(r"^(\s*)(issue_number:\s*).*$")
_LIST_ITEM_START = re.compile(r"^(\s*)-(\s*)(.*)$")
_MAPPING_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")


def _iter_list_item_blocks(
    lines: list[str], start: int, end: int
) -> Iterator[tuple[int, int]]:
    """Yield `(block_start, block_end)` for each top-level `- ` list item
    within `[start, end)`. A block spans every following line more deeply
    indented than the `-` itself (its mapping's own fields, plus any
    further-nested lists/mappings inside them), up to the next line at or
    below that indentation — a sibling item, or a dedent out of the list.
    """
    index = start
    while index < end:
        match = _LIST_ITEM_START.match(lines[index])
        if not match:
            index += 1
            continue
        item_indent = len(match.group(1))
        block_end = index + 1
        while block_end < end:
            line = lines[block_end]
            if line.strip() and len(line) - len(line.lstrip(" ")) <= item_indent:
                break
            block_end += 1
        yield index, block_end
        index = block_end


def _direct_fields(
    lines: list[str], block_start: int, block_end: int
) -> list[tuple[int, int, str, str]]:
    """Return `(line_index, field_indent, key, value_text)` for each key
    that belongs directly to this list item's own mapping — not to a
    further-nested mapping/list inside one of its values (e.g. a
    `footprint:` entry). Restricting to a single, consistent indentation
    column keeps a coincidental `id` key several levels deeper from
    shadowing this item's actual `id`.
    """
    start_match = _LIST_ITEM_START.match(lines[block_start])
    assert start_match is not None
    indent, dash_gap, inline = start_match.groups()
    field_indent = len(indent) + 1 + len(dash_gap)
    fields: list[tuple[int, int, str, str]] = []
    inline_key_match = _MAPPING_KEY.match(inline)
    if inline_key_match:
        fields.append(
            (
                block_start,
                field_indent,
                inline_key_match.group(1),
                inline_key_match.group(2),
            )
        )
    else:
        # A bare `-` with the mapping starting on the next line: that
        # line's own indentation defines this item's direct-field column.
        for index in range(block_start + 1, block_end):
            if lines[index].strip():
                field_indent = len(lines[index]) - len(lines[index].lstrip(" "))
                break
    for index in range(block_start + 1, block_end):
        line = lines[index]
        if not line.strip():
            continue
        line_indent = len(line) - len(line.lstrip(" "))
        if line_indent != field_indent:
            continue
        key_match = _MAPPING_KEY.match(line[line_indent:])
        if key_match:
            fields.append((index, field_indent, key_match.group(1), key_match.group(2)))
    return fields


def _decoded_id_matches(value_text: str, subtask_id: str) -> bool:
    """Compare a YAML-decoded `id` value against `subtask_id`, not the raw
    source text: `id` may be quoted, escaped, or followed by a comment, and
    `dag_parsing.parse_decomposition_plan` (the source of truth for
    `subtask_id`) always compares decoded, stripped values.

    The whole remainder of the line (comment included) is handed to
    `yaml.safe_load` as-is rather than comment-stripped first: only a real
    YAML parser reliably knows whether a given `#` starts a comment or sits
    inside a quoted scalar (`"task#1"`), a distinction a regex can't make.
    """
    try:
        decoded = yaml.safe_load(value_text)
    except yaml.YAMLError:
        return False
    # `dag_parsing._parse_subtask_id` strips the decoded id before using it
    # as `SubTask.id`; mirror that here or `id: " task-a "` (valid YAML,
    # decodes with the surrounding whitespace intact) would never match.
    if isinstance(decoded, str):
        decoded = decoded.strip()
    return bool(decoded == subtask_id)


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
    for block_start, block_end in _iter_list_item_blocks(lines, start, end):
        fields = _direct_fields(lines, block_start, block_end)
        id_field = next(
            (
                (line_index, field_indent)
                for line_index, field_indent, key, value_text in fields
                if key == "id" and _decoded_id_matches(value_text, subtask_id)
            ),
            None,
        )
        if id_field is None:
            continue
        id_index, field_indent = id_field
        for index in range(block_start, block_end):
            existing = _ISSUE_NUMBER_LINE.match(lines[index])
            if existing:
                lines[index] = f"{existing.group(1)}{existing.group(2)}{number}\n"
                return end
        indent_str = " " * field_indent
        lines.insert(id_index + 1, f"{indent_str}issue_number: {number}\n")
        return end + 1
    raise ValueError(
        f"decomposition_plan.md に該当するサブタスクが見つかりません: {subtask_id}"
    )


def _atomic_write_text(target: Path, content: str) -> None:
    """Replace `target`'s contents without ever leaving it truncated or
    partially written if the process dies or the disk fills mid-write."""
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        # `mkstemp` always creates the file at mode 0600, and `os.replace`
        # carries that mode onto the target — without this, every write
        # would silently strip the plan's original permissions (e.g.
        # 0644 -> 0600), which could stop other automation from reading it.
        os.chmod(tmp_name, target.stat().st_mode)
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.remove(tmp_name)
        except FileNotFoundError:
            pass
        raise


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

    _atomic_write_text(target, "".join(lines))
