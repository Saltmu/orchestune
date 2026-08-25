"""Tests for decomposition_plan.md persistence in parent issue body (Issue #532)."""

from __future__ import annotations

from pathlib import Path

from orchestune.issue_parsing import (
    DECOMPOSITION_PLAN_MARKER,
    PARENT_MARKER,
    decomposition_plan_from_parent_body,
    embed_decomposition_plan_in_parent_body,
    restore_plan_markdown_from_parent_body,
)
from orchestune.provisioning.flow import provision_issues
from orchestune.provisioning.parent import _resolve_parent_issue
from orchestune.provisioning.plan import PlanMetadata
from tests.test_provisioning_support import FakeForge


def test_embed_and_extract_decomposition_plan():
    initial_body = f"EPIC Description\n\n{PARENT_MARKER}"
    plan_dict = {
        "title": "My Big Rock",
        "parent_issue_number": 100,
        "subtasks": [
            {
                "id": "task-a",
                "description": "Task A description",
                "issue_number": 101,
            }
        ],
    }

    embedded = embed_decomposition_plan_in_parent_body(initial_body, plan_dict)
    assert DECOMPOSITION_PLAN_MARKER in embedded
    assert PARENT_MARKER in embedded
    assert "EPIC Description" in embedded

    extracted = decomposition_plan_from_parent_body(embedded)
    assert extracted is not None
    assert extracted["title"] == "My Big Rock"
    assert extracted["parent_issue_number"] == 100
    assert len(extracted["subtasks"]) == 1
    assert extracted["subtasks"][0]["id"] == "task-a"
    assert extracted["subtasks"][0]["issue_number"] == 101


def test_embed_replaces_existing_plan_block():
    initial_body = (
        f"Initial Body\n\n{PARENT_MARKER}\n\n{DECOMPOSITION_PLAN_MARKER}\n"
        "```yaml\ntitle: Old Title\n```"
    )
    new_plan_dict = {
        "title": "New Title",
        "parent_issue_number": 200,
        "subtasks": [{"id": "task-b", "issue_number": 201}],
    }

    updated = embed_decomposition_plan_in_parent_body(initial_body, new_plan_dict)
    # Should only contain one marker
    assert updated.count(DECOMPOSITION_PLAN_MARKER) == 1
    extracted = decomposition_plan_from_parent_body(updated)
    assert extracted is not None
    assert extracted["title"] == "New Title"
    assert extracted["parent_issue_number"] == 200
    assert extracted["subtasks"][0]["id"] == "task-b"


def test_restore_plan_markdown_from_parent_body():
    plan_dict = {
        "title": "Restored Rock",
        "parent_issue_number": 300,
        "subtasks": [
            {
                "id": "task-1",
                "description": "Task 1",
                "footprint": ["src/foo.py"],
                "symbols": ["foo.bar"],
                "depends_on": [],
                "issue_number": 301,
            }
        ],
    }
    initial_parent_body = (
        f"# Restored Rock\n\nOriginal human rationale prose.\n\n"
        f"配下のサブタスクはこのIssueのSub-issueとして紐付けられます。\n\n{PARENT_MARKER}"
    )
    body = embed_decomposition_plan_in_parent_body(initial_parent_body, plan_dict)
    restored_markdown = restore_plan_markdown_from_parent_body(body)
    assert restored_markdown is not None
    assert restored_markdown.startswith("---\n")
    assert "title: Restored Rock" in restored_markdown
    assert "parent_issue_number: 300" in restored_markdown
    assert "id: task-1" in restored_markdown
    assert "issue_number: 301" in restored_markdown
    # Prose should be preserved, but boilerplate and parent marker should be cleanly stripped
    assert "Original human rationale prose." in restored_markdown
    assert (
        "配下のサブタスクはこのIssueのSub-issueとして紐付けられます。"
        not in restored_markdown
    )
    assert PARENT_MARKER not in restored_markdown


def test_embed_and_parse_yaml_with_embedded_code_fence():
    # Review finding #2: Subtask description containing code fences
    plan_dict = {
        "title": "Plan with code snippet",
        "parent_issue_number": 400,
        "subtasks": [
            {
                "id": "code-task",
                "description": "Run ```python\nprint('hello')\n``` to test.",
                "issue_number": 401,
            }
        ],
    }
    body = embed_decomposition_plan_in_parent_body(
        f"Parent Body\n\n{PARENT_MARKER}", plan_dict
    )
    extracted = decomposition_plan_from_parent_body(body)
    assert extracted is not None
    assert extracted["title"] == "Plan with code snippet"
    assert "print('hello')" in extracted["subtasks"][0]["description"]


def test_restore_plan_file_from_parent_helper(tmp_path: Path):
    # Review finding #6: CLI helper for restoring plan file
    from orchestune.provisioning.plan import restore_plan_file_from_parent

    forge = FakeForge()
    plan_dict = {
        "title": "CLI Restored Rock",
        "parent_issue_number": 500,
        "subtasks": [{"id": "sub-1", "issue_number": 501}],
    }
    body = embed_decomposition_plan_in_parent_body(
        f"# CLI Restored Rock\n\nSome overview\n\n{PARENT_MARKER}", plan_dict
    )
    parent_num = forge.create_issue("[EPIC] CLI Restored Rock", body)

    out_file = tmp_path / "restored_plan.md"
    result_path = restore_plan_file_from_parent(forge, parent_num, output_path=out_file)
    assert result_path == out_file
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "title: CLI Restored Rock" in content
    assert "parent_issue_number: 500" in content
    assert "Some overview" in content


def test_provision_persists_plan_into_parent_issue_body(tmp_path: Path):
    plan_file = tmp_path / "decomposition_plan.md"
    template_file = tmp_path / "issue_template.md"
    template_file.write_text(
        "### Task {{subtask_id}}\n\n"
        "```yaml\n"
        "subtask_id: {{subtask_id_yaml}}\n"
        "depends_on: {{depends_on}}\n"
        "parent_issue_number: {{parent_issue_number}}\n"
        "execution_profile: {{execution_profile}}\n"
        "```\n",
        encoding="utf-8",
    )

    plan_file.write_text(
        "---\n"
        "title: Big Rock Persistence Test\n"
        "parent_issue_number: null\n"
        "subtasks:\n"
        "  - id: step-1\n"
        "    description: First step\n"
        "    footprint: []\n"
        "    symbols: []\n"
        "    depends_on: []\n"
        "    issue_number: null\n"
        "  - id: step-2\n"
        "    description: Second step\n"
        "    footprint: []\n"
        "    symbols: []\n"
        "    depends_on: [step-1]\n"
        "    issue_number: null\n"
        "---\n"
        "# Plan Overview\n"
        "Details here.\n",
        encoding="utf-8",
    )

    forge = FakeForge()
    res = provision_issues(
        plan_path=plan_file,
        forge=forge,
        template_path=template_file,
        repo_root=tmp_path,
    )

    assert res.parent_issue_number is not None
    parent_record = forge.get_issue(res.parent_issue_number)
    assert parent_record is not None

    # Parent body should contain DECOMPOSITION_PLAN_MARKER
    assert DECOMPOSITION_PLAN_MARKER in parent_record.body

    # Extracted plan should contain updated issue numbers
    extracted = decomposition_plan_from_parent_body(parent_record.body)
    assert extracted is not None
    assert extracted["title"] == "Big Rock Persistence Test"
    assert extracted["parent_issue_number"] == res.parent_issue_number
    assert len(extracted["subtasks"]) == 2
    subtask_1 = next(s for s in extracted["subtasks"] if s["id"] == "step-1")
    subtask_2 = next(s for s in extracted["subtasks"] if s["id"] == "step-2")
    assert subtask_1["issue_number"] == res.created["step-1"]
    assert subtask_2["issue_number"] == res.created["step-2"]


def test_decomposition_plan_from_parent_body_edge_cases():
    # Missing marker
    assert decomposition_plan_from_parent_body("Just plain text") is None

    # Broken YAML
    broken = f"{DECOMPOSITION_PLAN_MARKER}\n```yaml\n: invalid: yaml: [\n```"
    assert decomposition_plan_from_parent_body(broken) is None

    # YAML is not a dict (e.g. list)
    not_dict = f"{DECOMPOSITION_PLAN_MARKER}\n```yaml\n- item 1\n- item 2\n```"
    assert decomposition_plan_from_parent_body(not_dict) is None


def test_embed_decomposition_plan_with_string_and_formatting():
    # plan_data as raw string
    raw_str = "title: String Plan\nsubtasks: []\n"
    body = embed_decomposition_plan_in_parent_body("Initial text", raw_str)
    assert DECOMPOSITION_PLAN_MARKER in body
    extracted = decomposition_plan_from_parent_body(body)
    assert extracted is not None
    assert extracted["title"] == "String Plan"

    # body without trailing newline
    body_no_nl = embed_decomposition_plan_in_parent_body("No newline", {"k": "v"})
    assert "No newline\n\n<!-- orchestune:decomposition-plan -->" in body_no_nl


def test_restore_plan_markdown_missing_marker():
    assert restore_plan_markdown_from_parent_body("No plan marker here") is None


def test_provision_with_explicit_parent_persists_plan(tmp_path: Path):
    plan_file = tmp_path / "decomposition_plan.md"
    template_file = tmp_path / "issue_template.md"
    template_file.write_text(
        "### Task {{subtask_id}}\n\n"
        "```yaml\n"
        "subtask_id: {{subtask_id_yaml}}\n"
        "depends_on: {{depends_on}}\n"
        "parent_issue_number: {{parent_issue_number}}\n"
        "execution_profile: {{execution_profile}}\n"
        "```\n",
        encoding="utf-8",
    )

    plan_file.write_text(
        "---\n"
        "title: Explicit Parent Test\n"
        "parent_issue_number: null\n"
        "subtasks:\n"
        "  - id: sub-1\n"
        "    description: Subtask 1\n"
        "    footprint: []\n"
        "    symbols: []\n"
        "    depends_on: []\n"
        "    issue_number: null\n"
        "---\n",
        encoding="utf-8",
    )

    forge = FakeForge()
    # Create pre-existing parent issue
    parent_num = forge.create_issue(
        title="Pre-existing Parent Issue",
        body="Manual parent description",
    )

    res = provision_issues(
        plan_path=plan_file,
        forge=forge,
        template_path=template_file,
        repo_root=tmp_path,
        parent_issue=parent_num,
    )

    assert res.parent_issue_number == parent_num
    parent_record = forge.get_issue(parent_num)
    assert parent_record is not None
    assert parent_record.title == "[EPIC] Pre-existing Parent Issue"
    assert DECOMPOSITION_PLAN_MARKER in parent_record.body

    extracted = decomposition_plan_from_parent_body(parent_record.body)
    assert extracted is not None
    assert extracted["parent_issue_number"] == parent_num
    assert extracted["subtasks"][0]["id"] == "sub-1"
    assert extracted["subtasks"][0]["issue_number"] == res.created["sub-1"]


def test_incremental_parent_sync_on_partial_failure(tmp_path: Path):
    # Review finding #1: Incremental sync during subtask creation loop
    plan_file = tmp_path / "decomposition_plan.md"
    template_file = tmp_path / "issue_template.md"
    template_file.write_text(
        "### Task {{subtask_id}}\n\n"
        "```yaml\n"
        "subtask_id: {{subtask_id_yaml}}\n"
        "depends_on: {{depends_on}}\n"
        "parent_issue_number: {{parent_issue_number}}\n"
        "execution_profile: {{execution_profile}}\n"
        "```\n",
        encoding="utf-8",
    )

    plan_file.write_text(
        "---\n"
        "title: Incremental Test\n"
        "parent_issue_number: null\n"
        "subtasks:\n"
        "  - id: step-1\n"
        "    description: Step 1\n"
        "    footprint: []\n"
        "    symbols: []\n"
        "    depends_on: []\n"
        "    issue_number: null\n"
        "  - id: step-2\n"
        "    description: Step 2\n"
        "    footprint: []\n"
        "    symbols: []\n"
        "    depends_on: [step-1]\n"
        "    issue_number: null\n"
        "---\n",
        encoding="utf-8",
    )

    class FailingOnSecondIssueForge(FakeForge):
        def __init__(self):
            super().__init__()
            self._call_count = 0

        def create_issue(self, title: str, body: str, labels=()):
            # First call is parent, second is step-1, third is step-2
            self._call_count += 1
            if self._call_count == 3:
                raise RuntimeError("Simulated crash on step-2 creation")
            return super().create_issue(title, body, labels)

    forge = FailingOnSecondIssueForge()

    import pytest

    with pytest.raises(RuntimeError, match="Simulated crash on step-2 creation"):
        provision_issues(
            plan_path=plan_file,
            forge=forge,
            template_path=template_file,
            repo_root=tmp_path,
        )

    # Parent issue should already exist and contain step-1's issue number
    parent_issue = forge.issues[100]
    extracted = decomposition_plan_from_parent_body(parent_issue["body"])
    assert extracted is not None
    step_1 = next(s for s in extracted["subtasks"] if s["id"] == "step-1")
    step_2 = next(s for s in extracted["subtasks"] if s["id"] == "step-2")
    assert step_1["issue_number"] == 101
    assert step_2["issue_number"] is None


def test_provision_reports_plan_synced_false_on_sync_failure(tmp_path: Path):
    # Test that when parent issue sync fails, provision_issues surfaces plan_synced=False
    template_file = tmp_path / "issue_template.md"
    template_file.write_text(
        "### Task {{subtask_id}}\n\n"
        "```yaml\n"
        "subtask_id: {{subtask_id_yaml}}\n"
        "depends_on: {{depends_on}}\n"
        "parent_issue_number: {{parent_issue_number}}\n"
        "execution_profile: {{execution_profile}}\n"
        "```\n",
        encoding="utf-8",
    )
    plan_file = tmp_path / "decomposition_plan.md"
    plan_file.write_text(
        "---\n"
        "title: Failing Sync Rock\n"
        "parent_issue_number: null\n"
        "subtasks:\n"
        "  - id: step-1\n"
        "    description: Step 1\n"
        "    footprint: []\n"
        "    symbols: []\n"
        "    depends_on: []\n"
        "    issue_number: null\n"
        "---\n",
        encoding="utf-8",
    )

    class FailingUpdateBodyForge(FakeForge):
        def update_issue_body(self, issue_number: int | str, body: str) -> None:
            raise RuntimeError("Network error on update_issue_body")

    forge = FailingUpdateBodyForge()
    result = provision_issues(
        plan_path=plan_file,
        forge=forge,
        template_path=template_file,
        repo_root=tmp_path,
    )

    assert result.applied is True
    assert result.plan_synced is False
    assert len(result.created) == 1


def test_print_result_warns_on_plan_sync_failure(capsys):
    from orchestune.provisioning.cli import ProvisionResult, _print_result

    res = ProvisionResult(
        parent_issue_number=100,
        applied=True,
        created={"step-1": 101},
        reused={},
        plan_synced=False,
    )
    _print_result(res)
    captured = capsys.readouterr()
    assert (
        "Warning: could not sync decomposition plan into #100's body." in captured.out
    )


def test_resolve_parent_issue_reports_sync_status(tmp_path: Path):
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(
        "---\ntitle: 'My Big Rock'\nparent_issue_number: null\nsubtasks: []\n---\n",
        encoding="utf-8",
    )

    class FailingUpdateBodyForge(FakeForge):
        def update_issue_body(self, issue_number: int | str, body: str) -> None:
            raise RuntimeError("Network error on update_issue_body")

    forge = FailingUpdateBodyForge()
    metadata = PlanMetadata(
        title="My Big Rock",
        parent_issue_number=None,
        description="Epic details",
    )
    number, sync_ok = _resolve_parent_issue(forge, metadata, plan_path)
    assert number in forge.issues
    assert sync_ok is False


def test_provision_initial_sync_failure_sticks_even_when_subtask_sync_succeeds(
    tmp_path: Path,
):
    template_file = tmp_path / "issue_template.md"
    template_file.write_text(
        "### Task {{subtask_id}}\n\n"
        "```yaml\n"
        "subtask_id: {{subtask_id_yaml}}\n"
        "depends_on: {{depends_on}}\n"
        "parent_issue_number: {{parent_issue_number}}\n"
        "execution_profile: {{execution_profile}}\n"
        "```\n",
        encoding="utf-8",
    )
    plan_file = tmp_path / "decomposition_plan.md"
    plan_file.write_text(
        "---\n"
        "title: Sticky Plan Test\n"
        "parent_issue_number: null\n"
        "subtasks:\n"
        "  - id: step-1\n"
        "    description: Step 1\n"
        "    footprint: []\n"
        "    symbols: []\n"
        "    depends_on: []\n"
        "    issue_number: null\n"
        "---\n",
        encoding="utf-8",
    )

    class FailingFirstUpdateBodyForge(FakeForge):
        def __init__(self):
            super().__init__()
            self._update_count = 0

        def update_issue_body(self, issue_number: int | str, body: str) -> None:
            self._update_count += 1
            if self._update_count == 1:
                # First update (initial parent sync) fails
                raise RuntimeError("Network error on initial parent sync")
            super().update_issue_body(issue_number, body)

    forge = FailingFirstUpdateBodyForge()
    result = provision_issues(
        plan_path=plan_file,
        forge=forge,
        template_path=template_file,
        repo_root=tmp_path,
    )

    assert result.applied is True
    # plan_synced must stay False because initial sync failed, even though subtask sync succeeded
    assert result.plan_synced is False
    assert len(result.created) == 1
