import os
from pathlib import Path

import pytest
import yaml

from orchestune.plan_writer import (
    _decode_segment_key,
    _find_matching_brace,
    _split_flow_top_level,
    write_issue_numbers,
)

_PLAN = """\
---
title: "Example big rock"
parent_issue_number: null
subtasks:
  - id: task-a
    description: "Implement feature XX"
    priority: medium    # high, medium, low (default: medium)
    depends_on: []
    issue_number: null
  - id: task-b
    description: "Implement feature YY"
    depends_on: [task-a]
---

# Decomposition Plan
Free text body.
"""


@pytest.fixture
def plan_path(tmp_path: Path) -> Path:
    path = tmp_path / "decomposition_plan.md"
    path.write_text(_PLAN, encoding="utf-8")
    return path


class TestWriteSubtaskIssueNumber:
    def test_replaces_existing_null_value(self, plan_path: Path):
        write_issue_numbers(plan_path, {"task-a": 101})
        lines = plan_path.read_text(encoding="utf-8").splitlines()
        assert "    issue_number: 101" in lines
        assert "    issue_number: null" not in lines

    def test_inserts_when_field_is_absent(self, plan_path: Path):
        write_issue_numbers(plan_path, {"task-b": 102})
        text = plan_path.read_text(encoding="utf-8")
        lines = text.splitlines()
        id_index = lines.index("  - id: task-b")
        assert lines[id_index + 1].strip() == "issue_number: 102"

    def test_updates_value_on_second_call(self, plan_path: Path):
        write_issue_numbers(plan_path, {"task-a": 101})
        write_issue_numbers(plan_path, {"task-a": 999})
        lines = plan_path.read_text(encoding="utf-8").splitlines()
        assert lines.count("    issue_number: 999") == 1
        assert "    issue_number: 101" not in lines

    def test_idempotent_on_repeated_identical_write(self, plan_path: Path):
        write_issue_numbers(plan_path, {"task-a": 101})
        first = plan_path.read_text(encoding="utf-8")
        write_issue_numbers(plan_path, {"task-a": 101})
        second = plan_path.read_text(encoding="utf-8")
        assert first == second

    def test_preserves_comments_and_unrelated_formatting(self, plan_path: Path):
        write_issue_numbers(plan_path, {"task-a": 101})
        text = plan_path.read_text(encoding="utf-8")
        assert "# high, medium, low (default: medium)" in text
        assert "# Decomposition Plan\nFree text body.\n" in text

    def test_unknown_subtask_id_raises(self, plan_path: Path):
        with pytest.raises(ValueError, match="task-z"):
            write_issue_numbers(plan_path, {"task-z": 1})

    def test_finds_id_line_with_trailing_comment(self, tmp_path: Path):
        """#323 review (P2): `- id: task-a  # comment` is valid YAML (the
        parser ignores the comment) but the old end-anchored regex couldn't
        locate the line at all, so provisioning could never complete."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            "  - id: task-a  # implementation task\n"
            '    description: "d"\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-a": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        assert "    issue_number: 101" in lines
        assert "  - id: task-a  # implementation task" in lines

    def test_finds_id_line_with_escaped_yaml_scalar(self, tmp_path: Path):
        """#323 review (P2): `- id: "task\\x2da"` is valid YAML that decodes
        to `task-a` (matching what `dag_parsing.parse_decomposition_plan`
        produces), but a literal source-text match for the decoded value
        can't find this line since the raw text differs from the decoded
        value."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            '  - id: "task\\x2da"\n'
            '    description: "d"\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-a": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        assert "    issue_number: 101" in lines
        assert '  - id: "task\\x2da"' in lines

    def test_finds_id_line_with_hash_inside_quotes(self, tmp_path: Path):
        """#323 review (P2): `#` inside a quoted YAML scalar (`"task#1"`) is
        not a comment, but a regex that comment-strips before decoding
        can't tell the difference and truncates the value at the `#`."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            '  - id: "task#1"\n'
            '    description: "d"\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task#1": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        assert "    issue_number: 101" in lines
        assert '  - id: "task#1"' in lines

    def test_finds_id_line_with_surrounding_whitespace(self, tmp_path: Path):
        """#323 review (P2): `id: " task-a "` is valid YAML that decodes with
        the surrounding whitespace intact, but `dag_parsing._parse_subtask_id`
        strips it before use, so the matcher must strip too or it can never
        find the entry."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            '  - id: " task-a "\n'
            '    description: "d"\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-a": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        assert "    issue_number: 101" in lines
        assert '  - id: " task-a "' in lines

    def test_finds_id_key_when_not_the_first_key_in_the_mapping(self, tmp_path: Path):
        """#323 review round 7 (P2): the old matcher only recognized `id`
        as the literal first key right after `- `; a subtask whose mapping
        lists another field first (e.g. `description`) could never be
        located at all."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            '  - description: "d"\n'
            "    id: task-a\n"
            "    priority: medium\n"
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-a": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        assert "    issue_number: 101" in lines
        id_index = lines.index("    id: task-a")
        assert lines[id_index + 1].strip() == "issue_number: 101"

    def test_ignores_nested_id_key_that_is_not_the_subtasks_own(self, tmp_path: Path):
        """#323 review round 7 (P2): a coincidental `id` key nested deeper
        inside one of this subtask's own fields (e.g. a `metadata:` mapping)
        must not shadow the subtask's actual `id` — even when its value
        collides with a real subtask id elsewhere, it must not be mistaken
        for that subtask's own `id` field."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            "  - id: task-a\n"
            "    metadata:\n"
            "      id: task-b\n"
            '      note: "x"\n'
            "---\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="task-b"):
            write_issue_numbers(path, {"task-b": 101})

    def test_does_not_overwrite_a_nested_issue_number_key(self, tmp_path: Path):
        """#323 review round 8 (P2): a coincidental `issue_number` key
        nested inside one of this subtask's own fields (e.g. a `metadata:`
        mapping) must not be mistaken for the subtask's own direct
        `issue_number` field and overwritten in place of it."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            "  - id: task-a\n"
            "    metadata:\n"
            "      issue_number: 77\n"
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-a": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        assert "    issue_number: 101" in lines
        assert "      issue_number: 77" in lines  # nested value left untouched

    def test_writes_into_a_flow_style_subtask_mapping(self, tmp_path: Path):
        """#323 review round 8 (P2): `- {id: task-a, description: d}` is
        valid YAML `dag_parsing.parse_decomposition_plan` accepts, but the
        block-style-only matcher couldn't locate (or write into) it at
        all."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            '  - {id: task-a, description: "d"}\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-a": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        flow_line = next(line for line in lines if "task-a" in line)
        decoded = yaml.safe_load(flow_line.strip().removeprefix("- "))
        assert decoded == {"id": "task-a", "description": "d", "issue_number": 101}

    def test_updates_existing_issue_number_in_a_flow_style_subtask_mapping(
        self, tmp_path: Path
    ):
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            "  - {id: task-a, issue_number: 101}\n"
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-a": 999})
        lines = path.read_text(encoding="utf-8").splitlines()
        flow_line = next(line for line in lines if "task-a" in line)
        decoded = yaml.safe_load(flow_line.strip().removeprefix("- "))
        assert decoded == {"id": "task-a", "issue_number": 999}

    def test_writes_into_a_flow_style_mapping_with_a_trailing_comment(
        self, tmp_path: Path
    ):
        """#323 review round 9 (P2): `- {id: task-a, description: d} # note`
        is valid YAML (PyYAML accepts a comment after the closing brace),
        but the old flow-item regex required only whitespace after `}`, so
        this line was silently treated as not flow-style at all."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            '  - {id: task-a, description: "d"} # note\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-a": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        flow_line = next(line for line in lines if "task-a" in line)
        assert flow_line.rstrip().endswith("# note")
        decoded = yaml.safe_load(flow_line.split("#")[0].strip().removeprefix("- "))
        assert decoded == {"id": "task-a", "description": "d", "issue_number": 101}

    def test_does_not_corrupt_a_quoted_value_containing_issue_number_text(
        self, tmp_path: Path
    ):
        """#323 review round 9 (P2): a naive raw-text search for
        `issue_number:` inside a flow mapping could match that literal
        substring sitting inside an unrelated quoted string value,
        corrupting the line into invalid YAML instead of adding a new
        top-level `issue_number` key."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            '  - {id: task-a, description: "foo issue_number: 77 bar"}\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-a": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        flow_line = next(line for line in lines if "task-a" in line)
        decoded = yaml.safe_load(flow_line.strip().removeprefix("- "))
        assert decoded == {
            "id": "task-a",
            "description": "foo issue_number: 77 bar",
            "issue_number": 101,
        }

    def test_does_not_overwrite_a_nested_issue_number_in_a_flow_style_mapping(
        self, tmp_path: Path
    ):
        """#323 review round 9 (P2): a nested flow mapping's own
        `issue_number` key (e.g. inside `metadata: {issue_number: 77}`)
        must not be mistaken for the subtask's own top-level field."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            "  - {id: task-a, metadata: {issue_number: 77}}\n"
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-a": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        flow_line = next(line for line in lines if "task-a" in line)
        decoded = yaml.safe_load(flow_line.strip().removeprefix("- "))
        assert decoded == {
            "id": "task-a",
            "metadata": {"issue_number": 77},
            "issue_number": 101,
        }

    def test_ignores_an_id_in_a_list_that_precedes_the_subtasks_list(
        self, tmp_path: Path
    ):
        """#323 review round 9 (P2): `_write_subtask_issue_number` used to
        scan the whole frontmatter, not just the `subtasks:` list. A same-
        valued `id` key in some other top-level list appearing earlier in
        the frontmatter must not be mistaken for the actual subtask."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "examples:\n"
            "  - id: task-a\n"
            "    note: not a real subtask\n"
            "subtasks:\n"
            "  - id: task-a\n"
            '    description: "d"\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-a": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        examples_id_index = lines.index("  - id: task-a")
        assert lines[examples_id_index + 1].strip() != "issue_number: 101"
        subtasks_id_index = lines.index("  - id: task-a", examples_id_index + 1)
        assert lines[subtasks_id_index + 1].strip() == "issue_number: 101"

    def test_finds_subtasks_across_a_column_zero_comment(self, tmp_path: Path):
        """#323 review round 10 (P2): a comment at column 0 between
        sequence items (`# next task`) is valid YAML and must not be
        mistaken for the next top-level frontmatter key ending the
        `subtasks:` list — a subtask after such a comment must still be
        found."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            "  - id: task-a\n"
            '    description: "a"\n'
            "# next task\n"
            "  - id: task-b\n"
            '    description: "b"\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-b": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        id_index = lines.index("  - id: task-b")
        assert lines[id_index + 1].strip() == "issue_number: 101"

    def test_finds_a_field_across_a_column_zero_comment_inside_the_item(
        self, tmp_path: Path
    ):
        """#323 review round 11 (P2): a column-0 comment interrupting a
        block-style item's own fields (before `id`, in this case) is valid
        YAML that `parse_decomposition_plan` still accepts as one mapping,
        but `_iter_list_item_blocks` mistook the comment for a dedent out
        of the item, cutting the block off before `id` was ever seen."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            '  - description: "d"\n'
            "# identity\n"
            "    id: task-a\n"
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-a": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        id_index = lines.index("    id: task-a")
        assert lines[id_index + 1].strip() == "issue_number: 101"

    def test_finds_the_flow_mapping_close_brace_before_a_trailing_comment(
        self, tmp_path: Path
    ):
        """#323 review round 10 (P2): when the trailing comment on a
        flow-style item itself contains `}` (`- {id: task-a} # } note`), a
        greedy end-anchored search for the *last* `}` in the line picks the
        one inside the comment instead of the mapping's real closing brace,
        corrupting the rewritten line."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n" 'title: "x"\n' "subtasks:\n" "  - {id: task-a} # } note\n" "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-a": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        flow_line = next(line for line in lines if "task-a" in line)
        before_comment = flow_line.split("#", 1)[0].strip().removeprefix("- ")
        decoded = yaml.safe_load(before_comment)
        assert decoded == {"id": "task-a", "issue_number": 101}
        assert flow_line.rstrip().endswith("} note")

    def test_recognizes_a_quoted_existing_issue_number_key_in_flow_style(
        self, tmp_path: Path
    ):
        """#323 review round 10 (P2): an existing `issue_number` key that's
        quoted or oddly spaced (`"issue_number": 77`) is still the same
        field as a plain `issue_number: 77` key once decoded — a raw-text
        regex that only recognizes bare identifiers would append a second,
        duplicate key instead of replacing the existing one."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            '  - {id: task-a, "issue_number": 77}\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-a": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        flow_line = next(line for line in lines if "task-a" in line)
        decoded = yaml.safe_load(flow_line.strip().removeprefix("- "))
        assert decoded == {"id": "task-a", "issue_number": 101}
        # Not just decoded correctly (PyYAML silently resolves duplicate
        # keys by keeping the last one) but genuinely replaced in place:
        # the stale value and the old quoted key must both be gone, or a
        # different YAML parser reading the raw file could pick the first
        # duplicate key instead and see the stale number.
        assert "77" not in flow_line
        assert flow_line.count("issue_number") == 1


class TestWriteParentIssueNumber:
    def test_replaces_existing_null_value(self, plan_path: Path):
        write_issue_numbers(plan_path, parent_issue_number=42)
        text = plan_path.read_text(encoding="utf-8")
        assert "parent_issue_number: 42\n" in text
        assert "parent_issue_number: null" not in text

    def test_idempotent_on_repeated_identical_write(self, plan_path: Path):
        write_issue_numbers(plan_path, parent_issue_number=42)
        first = plan_path.read_text(encoding="utf-8")
        write_issue_numbers(plan_path, parent_issue_number=42)
        second = plan_path.read_text(encoding="utf-8")
        assert first == second

    def test_inserts_after_title_when_field_absent(self, tmp_path: Path):
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            '---\ntitle: "No parent field yet"\nsubtasks:\n  - id: task-a\n---\n',
            encoding="utf-8",
        )
        write_issue_numbers(path, parent_issue_number=7)
        lines = path.read_text(encoding="utf-8").splitlines()
        title_index = lines.index('title: "No parent field yet"')
        assert lines[title_index + 1] == "parent_issue_number: 7"


def test_missing_frontmatter_raises(tmp_path: Path):
    path = tmp_path / "decomposition_plan.md"
    path.write_text("# No frontmatter here\n", encoding="utf-8")
    with pytest.raises(ValueError, match="フロントマター"):
        write_issue_numbers(path, {"task-a": 1})


class TestAtomicWrite:
    """#323 review (P2): the write must never leave the plan truncated or
    partially written if the process dies mid-write."""

    def test_no_leftover_temp_file_after_a_normal_write(self, plan_path: Path):
        write_issue_numbers(plan_path, {"task-a": 101})
        siblings = {p.name for p in plan_path.parent.iterdir()}
        assert siblings == {plan_path.name}

    def test_original_file_is_untouched_if_the_write_fails_partway(
        self, plan_path: Path, monkeypatch
    ):
        original = plan_path.read_text(encoding="utf-8")

        def _failing_fsync(fd):
            raise OSError("simulated disk failure")

        monkeypatch.setattr(os, "fsync", _failing_fsync)

        with pytest.raises(OSError):
            write_issue_numbers(plan_path, {"task-a": 101})

        assert plan_path.read_text(encoding="utf-8") == original
        siblings = {p.name for p in plan_path.parent.iterdir()}
        assert siblings == {plan_path.name}  # no leftover temp file either

    def test_preserves_original_file_permissions(self, plan_path: Path):
        """#323 review (P2): `mkstemp` creates the replacement at mode 0600;
        without copying the original's mode across, a typical 0644 plan
        would silently become owner-only on every write."""
        os.chmod(plan_path, 0o644)

        write_issue_numbers(plan_path, {"task-a": 101})

        assert oct(plan_path.stat().st_mode)[-3:] == "644"

    def test_swallows_missing_temp_file_during_cleanup(
        self, plan_path: Path, monkeypatch
    ):
        """When the write itself fails, cleanup removes the leftover temp
        file; if that temp file is already gone by the time cleanup runs
        (e.g. a race with an external cleaner), the resulting
        `FileNotFoundError` must be swallowed so the *original* write
        failure is still what propagates to the caller."""

        def _failing_fsync(fd):
            raise OSError("simulated disk failure")

        def _missing_remove(path):
            raise FileNotFoundError(path)

        monkeypatch.setattr(os, "fsync", _failing_fsync)
        monkeypatch.setattr(os, "remove", _missing_remove)

        with pytest.raises(OSError, match="simulated disk failure"):
            write_issue_numbers(plan_path, {"task-a": 101})


class TestListItemBoundaryValues:
    """`_iter_list_item_blocks`/`_direct_fields`の走査境界値。"""

    def test_finds_item_after_a_blank_line_before_the_first_entry(self, tmp_path: Path):
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            "\n"
            "  - id: task-a\n"
            '    description: "d"\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-a": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        assert "    issue_number: 101" in lines

    def test_handles_a_bare_dash_with_mapping_starting_on_the_next_line(
        self, tmp_path: Path
    ):
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            "  -\n"
            "    id: task-a\n"
            '    description: "d"\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-a": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        id_index = lines.index("    id: task-a")
        assert lines[id_index + 1].strip() == "issue_number: 101"

    def test_bare_dash_skips_a_blank_line_before_the_first_field(self, tmp_path: Path):
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            "  -\n"
            "\n"
            "    id: task-a\n"
            '    description: "d"\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-a": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        id_index = lines.index("    id: task-a")
        assert lines[id_index + 1].strip() == "issue_number: 101"

    def test_skips_a_blank_line_between_an_items_own_fields(self, tmp_path: Path):
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            "  - id: task-a\n"
            "\n"
            '    description: "d"\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-a": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        assert "  - id: task-a" in lines
        id_index = lines.index("  - id: task-a")
        assert lines[id_index + 1].strip() == "issue_number: 101"

    def test_ignores_a_same_indent_line_that_is_not_a_mapping_key(self, tmp_path: Path):
        """A nested list value (`- urgent`) sitting at the same column as
        this item's own `key: value` fields is not itself a `key: value`
        line and must be skipped rather than mistaken for one."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            "  - id: task-a\n"
            "    tags:\n"
            "    - urgent\n"
            '    description: "d"\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-a": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        id_index = lines.index("  - id: task-a")
        assert lines[id_index + 1].strip() == "issue_number: 101"
        assert "    - urgent" in lines


class TestDecodedIdBoundaryValues:
    """`_decoded_id_matches`の異常値・型不一致に対する安全なスキップ。"""

    def test_skips_a_candidate_with_invalid_yaml_in_its_id_value(self, tmp_path: Path):
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            '  - id: "unterminated\n'
            '    description: "decoy"\n'
            "  - id: task-b\n"
            '    description: "real"\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-b": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        id_index = lines.index("  - id: task-b")
        assert lines[id_index + 1].strip() == "issue_number: 101"

    def test_skips_a_candidate_whose_id_decodes_to_a_non_string(self, tmp_path: Path):
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            "  - id: 123\n"
            '    description: "decoy"\n'
            "  - id: task-b\n"
            '    description: "real"\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-b": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        id_index = lines.index("  - id: task-b")
        assert lines[id_index + 1].strip() == "issue_number: 101"


class TestFlowStyleBoundaryValues:
    def test_unbalanced_brace_falls_through_to_the_next_item(self, tmp_path: Path):
        """A flow-style item whose `{` is never closed on its own line
        can't be a valid flow mapping; `_write_flow_style_issue_number`
        must decline it (rather than crash) so the search continues to the
        next, well-formed item."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            "  - {id: task-a\n"
            "  - id: task-b\n"
            '    description: "d"\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-b": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        id_index = lines.index("  - id: task-b")
        assert lines[id_index + 1].strip() == "issue_number: 101"

    def test_falls_through_when_flow_mapping_yaml_is_invalid(self, tmp_path: Path):
        """A flow item whose braces balance but whose inner text is not
        valid YAML on its own (a bare `1: 2` mapping value with no braces
        of its own) must be declined rather than crash, falling through to
        the next item."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            "  - {id: task-a, x: 1: 2}\n"
            "  - id: task-b\n"
            '    description: "d"\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-b": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        id_index = lines.index("  - id: task-b")
        assert lines[id_index + 1].strip() == "issue_number: 101"

    def test_falls_through_when_flow_mapping_has_no_id_key(self, tmp_path: Path):
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            '  - {description: "decoy", issue_number: 5}\n'
            "  - id: task-b\n"
            '    description: "real"\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-b": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        id_index = lines.index("  - id: task-b")
        assert lines[id_index + 1].strip() == "issue_number: 101"

    def test_skips_a_flow_candidate_whose_id_decodes_to_a_non_string(
        self, tmp_path: Path
    ):
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            "  - {id: 123}\n"
            "  - id: task-b\n"
            '    description: "real"\n'
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-b": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        id_index = lines.index("  - id: task-b")
        assert lines[id_index + 1].strip() == "issue_number: 101"


class TestFindMatchingBrace:
    """`_find_matching_brace`の境界値: エスケープ済み引用符と閉じ括弧欠如。"""

    def test_skips_an_escaped_quote_inside_a_double_quoted_value(self):
        text = '{id: task-a, description: "a\\"b"}'
        open_index = text.index("{")
        close_index = _find_matching_brace(text, open_index)
        assert close_index == len(text) - 1

    def test_returns_none_when_no_closing_brace_exists(self):
        text = "{id: task-a"
        open_index = text.index("{")
        assert _find_matching_brace(text, open_index) is None


class TestDecodeSegmentKey:
    """`_decode_segment_key`の境界値: 不正YAMLと単一キー以外の結果。"""

    def test_returns_none_for_invalid_yaml(self):
        assert _decode_segment_key(" x: [1, 2") is None

    def test_returns_none_when_decoded_value_is_not_a_single_key_mapping(self):
        # An empty segment (e.g. from a trailing comma) decodes to `{}`,
        # a dict of length 0 rather than exactly one key.
        assert _decode_segment_key("") is None


class TestSplitFlowTopLevel:
    """`_split_flow_top_level`の境界値: エスケープ済みカンマと空入力。"""

    def test_keeps_an_escaped_quote_inside_a_segment_intact(self):
        inner = 'id: task-a, description: "a\\"b"'
        segments = _split_flow_top_level(inner)
        assert segments == ["id: task-a", ' description: "a\\"b"']

    def test_returns_no_segments_for_empty_input(self):
        assert _split_flow_top_level("") == []


class TestFrontmatterAndSubtasksBoundsErrors:
    def test_missing_closing_delimiter_raises(self, tmp_path: Path):
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            '---\ntitle: "x"\nsubtasks:\n  - id: task-a\n', encoding="utf-8"
        )
        with pytest.raises(ValueError, match="終端"):
            write_issue_numbers(path, {"task-a": 1})

    def test_subtasks_list_ends_at_a_following_top_level_key(self, tmp_path: Path):
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            "  - id: task-a\n"
            '    description: "d"\n'
            "notes: done\n"
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, {"task-a": 101})
        lines = path.read_text(encoding="utf-8").splitlines()
        id_index = lines.index("  - id: task-a")
        assert lines[id_index + 1].strip() == "issue_number: 101"
        assert "notes: done" in lines

    def test_missing_subtasks_key_raises(self, tmp_path: Path):
        path = tmp_path / "decomposition_plan.md"
        path.write_text('---\ntitle: "x"\n---\n', encoding="utf-8")
        with pytest.raises(ValueError, match="subtasks"):
            write_issue_numbers(path, {"task-a": 1})


class TestWriteParentIssueNumberInsertionBoundaryValues:
    def test_inserts_at_the_top_when_no_title_line_exists(self, tmp_path: Path):
        path = tmp_path / "decomposition_plan.md"
        path.write_text("---\nsubtasks:\n  - id: task-a\n---\n", encoding="utf-8")
        write_issue_numbers(path, parent_issue_number=7)
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[1] == "parent_issue_number: 7"
        assert lines[2] == "subtasks:"

    def test_inserts_after_title_when_a_field_precedes_it(self, tmp_path: Path):
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            "some_other_field: value\n"
            'title: "x"\n'
            "subtasks:\n"
            "  - id: task-a\n"
            "---\n",
            encoding="utf-8",
        )
        write_issue_numbers(path, parent_issue_number=7)
        lines = path.read_text(encoding="utf-8").splitlines()
        title_index = lines.index('title: "x"')
        assert lines[title_index + 1] == "parent_issue_number: 7"
