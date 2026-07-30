import os
from pathlib import Path

import pytest
import yaml

from orchestune.plan_writer import write_issue_numbers

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
