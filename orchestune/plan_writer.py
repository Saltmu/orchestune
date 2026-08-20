"""Surgical in-place updates to `decomposition_plan.md`'s YAML frontmatter.

Only the `issue_number` / `parent_issue_number` lines this module owns are
touched; every other line (including comments and formatting) is left
byte-for-byte identical, so re-dumping the frontmatter through a YAML
serializer is deliberately avoided.

Supports standard block-style subtask mappings (`- key: value` across
multiple lines, `id` in any position, unquoted bare-identifier keys,
comments anywhere including interrupting a mapping mid-item) and
single-line flow-style mappings (`- {id: task-a, ...}`). Deliberately not
supported: a flow mapping split across multiple physical lines, and quoted
or oddly-spaced keys in block style (`- "id": task-a`) — these are valid
YAML that `dag_parsing.parse_decomposition_plan` accepts, but are exotic
enough that `decomposition_plan.md` authors are expected to avoid them;
see `docs/{en,ja}/usage.md`.
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
_PARENT_ISSUE_NUMBER_LINE = re.compile(
    r"^(parent_issue_number:\s*)(?:'[^']*'|\"[^\"]*\"|[^ \t\n#]+)?([ \t]*(?:#.*)?)\r?\n?$"
)
_PARENT_ISSUE_SOURCE_LINE = re.compile(
    r"^(parent_issue_source:\s*)(?:'[^']*'|\"[^\"]*\"|[^ \t\n#]+)?([ \t]*(?:#.*)?)\r?\n?$"
)
_ISSUE_NUMBER_LINE = re.compile(
    r"^(\s*)(issue_number:\s*)(?:'[^']*'|\"[^\"]*\"|[^ \t\n#]+)?([ \t]*(?:#.*)?)\r?\n?$"
)
_LIST_ITEM_START = re.compile(r"^(\s*)-(\s*)(.*)$")
_MAPPING_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
_FLOW_ITEM_OPEN = re.compile(r"^(\s*)-(\s*)\{")
_SUBTASKS_KEY_LINE = re.compile(r"^subtasks:\s*(#.*)?$")


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
            stripped = line.strip()
            # A column-0 (or otherwise shallow) comment interrupting a
            # block-style item's own fields is valid YAML and must not be
            # mistaken for a sibling item or a dedent out of the list —
            # only a real (non-comment) line at or below `item_indent`
            # does that.
            if (
                stripped
                and not stripped.startswith("#")
                and len(line) - len(line.lstrip(" ")) <= item_indent
            ):
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


def _find_matching_brace(text: str, open_index: int) -> int | None:
    """Return the index of the `}` that closes the `{` at `open_index`,
    tracking quotes and nesting depth so a brace character sitting inside
    a quoted value or a trailing comment is never mistaken for it — unlike
    a greedy `\\{(.*)\\}` regex, which always prefers the *last* `}` in the
    line, wherever it is."""
    depth = 0
    quote: str | None = None
    index = open_index
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\" and quote == '"' and index + 1 < len(text):
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _decode_segment_key(segment: str) -> str | None:
    """Decode a flow mapping's top-level `key: value` segment and return
    its real (parsed) key — not a source-text regex match, which can't
    recognize a quoted or oddly-spaced key (`"issue_number": 77`) as the
    same field a plain `issue_number: 77` key names."""
    try:
        decoded = yaml.safe_load("{" + segment + "}")
    except yaml.YAMLError:
        return None
    if isinstance(decoded, dict) and len(decoded) == 1:
        return str(next(iter(decoded)))
    return None


def _split_flow_top_level(inner: str) -> list[str]:
    """Split a flow mapping's inner text (between `{` and `}`) into its
    top-level `key: value` segments on commas, the way a real YAML parser
    would — not on every literal `,`, which would also cut through commas
    sitting inside a quoted string value or a nested `{...}`/`[...]`."""
    segments = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    index = 0
    while index < len(inner):
        char = inner[index]
        if quote:
            current.append(char)
            if char == "\\" and quote == '"' and index + 1 < len(inner):
                index += 1
                current.append(inner[index])
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
            current.append(char)
        elif char in "{[":
            depth += 1
            current.append(char)
        elif char in "}]":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            segments.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    if current:
        segments.append("".join(current))
    return segments


def _write_flow_style_issue_number(
    lines: list[str], block_start: int, subtask_id: str, number: int
) -> bool:
    """Handle a single-line flow-style list item (`- {id: task-a, ...}`):
    a valid form `dag_parsing.parse_decomposition_plan` accepts via plain
    `yaml.safe_load`, but that `_LIST_ITEM_START`/`_MAPPING_KEY` (built for
    block-style `key: value` lines) can't parse — the `{` doesn't match a
    mapping key, so a flow-style subtask's `id` would otherwise never be
    found at all.

    Returns `True` if this line was flow-style and its `id` matched (the
    line has been rewritten in place), `False` otherwise so the caller can
    fall through to the block-style search.
    """
    raw_line = lines[block_start]
    newline = "\n" if raw_line.endswith("\n") else ""
    body = raw_line[: len(raw_line) - len(newline)]
    open_match = _FLOW_ITEM_OPEN.match(body)
    if not open_match:
        return False
    indent, dash_gap = open_match.groups()
    open_index = open_match.end() - 1  # position of the `{` itself
    close_index = _find_matching_brace(body, open_index)
    if close_index is None:
        return False
    inner = body[open_index + 1 : close_index]
    trailing = body[close_index + 1 :]
    try:
        decoded = yaml.safe_load("{" + inner + "}")
    except yaml.YAMLError:
        return False
    if not isinstance(decoded, dict) or "id" not in decoded:
        return False
    decoded_id = decoded["id"]
    if isinstance(decoded_id, str):
        decoded_id = decoded_id.strip()
    if decoded_id != subtask_id:
        return False

    # Rewrite only the top-level `issue_number` segment, matched by its
    # decoded key (so a quoted or oddly-spaced existing key is still
    # recognized) rather than a raw-text search for the substring
    # `issue_number:` — that would also match inside a quoted string value
    # or a nested flow mapping's own `issue_number` key, corrupting the
    # line or silently updating the wrong field.
    segments = _split_flow_top_level(inner)
    updated = False
    new_segments = []
    for segment in segments:
        if _decode_segment_key(segment) == "issue_number":
            prefix = " " if segment.startswith(" ") else ""
            new_segments.append(f"{prefix}issue_number: {number}")
            updated = True
        else:
            new_segments.append(segment)
    if not updated:
        new_segments.append(f" issue_number: {number}")
    new_inner = ",".join(new_segments)
    lines[block_start] = f"{indent}-{dash_gap}{{{new_inner}}}{trailing}{newline}"
    return True


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


def _find_subtasks_bounds(lines: list[str], start: int, end: int) -> tuple[int, int]:
    """Scope `[start, end)` down to just the `subtasks:` list's own lines.

    Without this, a subtask lookup would scan the *entire* frontmatter: if
    some other top-level list happens to precede `subtasks:` and one of its
    entries happens to carry an `id` (or `issue_number`) key with the same
    value, it would be found and mutated instead of the actual subtask.
    """
    for index in range(start, end):
        if _SUBTASKS_KEY_LINE.match(lines[index]):
            list_start = index + 1
            list_end = list_start
            while list_end < end:
                line = lines[list_end]
                stripped = line.strip()
                # A column-0 comment (`# ...`) between sequence items is
                # valid YAML and must not be mistaken for the next
                # top-level frontmatter key ending the list — only a real
                # (non-comment) line at indent 0 does that.
                if (
                    stripped
                    and not stripped.startswith("#")
                    and len(line) - len(line.lstrip(" ")) == 0
                ):
                    break
                list_end += 1
            return list_start, list_end
    raise ValueError("decomposition_plan.md に 'subtasks:' フィールドが見つかりません")


def _write_parent_issue_number(
    lines: list[str], start: int, end: int, number: int
) -> int:
    for index in range(start, end):
        match = _PARENT_ISSUE_NUMBER_LINE.match(lines[index])
        if match:
            lines[index] = f"{match.group(1)}{number}{match.group(2)}\n"
            return end
    insert_at = start
    for index in range(start, end):
        if _TITLE_LINE.match(lines[index]):
            insert_at = index + 1
            break
    lines.insert(insert_at, f"parent_issue_number: {number}\n")
    return end + 1


def _write_parent_issue_source(
    lines: list[str], start: int, end: int, source: str
) -> int:
    for index in range(start, end):
        match = _PARENT_ISSUE_SOURCE_LINE.match(lines[index])
        if match:
            lines[index] = f"{match.group(1)}{source}{match.group(2)}\n"
            return end
    insert_at = start
    for index in range(start, end):
        if _PARENT_ISSUE_NUMBER_LINE.match(lines[index]):
            insert_at = index + 1
            break
    lines.insert(insert_at, f"parent_issue_source: {source}\n")
    return end + 1


def _write_subtask_issue_number(
    lines: list[str], start: int, end: int, subtask_id: str, number: int
) -> int:
    for block_start, block_end in _iter_list_item_blocks(lines, start, end):
        if _write_flow_style_issue_number(lines, block_start, subtask_id, number):
            return end
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
        # Only a direct field of this item's own mapping may be treated as
        # its `issue_number` — the same restriction the `id` lookup above
        # applies — so a coincidental `issue_number` key nested inside one
        # of this item's own fields (e.g. a `metadata:` sub-mapping) can't
        # be mistaken for, and overwritten in place of, the subtask's own.
        existing_index = next(
            (line_index for line_index, _, key, _ in fields if key == "issue_number"),
            None,
        )
        if existing_index is not None:
            existing = _ISSUE_NUMBER_LINE.match(lines[existing_index])
            assert existing is not None
            lines[existing_index] = (
                f"{existing.group(1)}{existing.group(2)}{number}{existing.group(3)}\n"
            )
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
    parent_issue_source: str | None = None,
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

    if parent_issue_source is not None:
        end = _write_parent_issue_source(lines, start, end, parent_issue_source)

    for subtask_id, number in (subtask_issue_numbers or {}).items():
        subtasks_start, subtasks_end = _find_subtasks_bounds(lines, start, end)
        new_subtasks_end = _write_subtask_issue_number(
            lines, subtasks_start, subtasks_end, subtask_id, number
        )
        end += new_subtasks_end - subtasks_end

    _atomic_write_text(target, "".join(lines))
