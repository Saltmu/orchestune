"""Tests for plan restoration from parent and child issues."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orchestune.dag.parsing import extract_frontmatter_and_body
from orchestune.issue_parsing import embed_decomposition_plan_in_parent_body
from orchestune.models import IssueRecord
from orchestune.provisioning.plan import restore_plan_file_from_parent
from orchestune.provisioning.rendering import _render_issue_body
from tests.test_provisioning_support import FakeForge

DEFAULT_TEMPLATE = Path(".github/issue_template.md").read_text(encoding="utf-8")


class InMemoryForge(FakeForge):
    def __init__(self, issues: dict[int, IssueRecord]) -> None:
        super().__init__()
        for num, rec in issues.items():
            self.issues[num] = {
                "title": rec.title,
                "body": rec.body,
                "labels": list(rec.labels),
                "state": rec.state,
            }
        self.call_count: dict[int, int] = {}

    def get_issue(self, issue_number: int | str) -> IssueRecord | None:
        num = int(issue_number)
        self.call_count[num] = self.call_count.get(num, 0) + 1
        return super().get_issue(num)


def _make_child_body(
    subtask_id: str,
    desc: str,
    overview: str = "Child overview",
    changes: tuple[str, ...] = ("Child change 1",),
    criteria: tuple[str, ...] = ("Child criterion 1",),
    verification: tuple[str, ...] = ("Child verification",),
    footprint: tuple[str, ...] = ("orchestune/child.py",),
    symbols: tuple[str, ...] = ("child_sym",),
    depends_on: tuple[str, ...] = (),
    parent_issue_number: int = 100,
    **extra_subtask_fields: Any,
) -> str:
    from orchestune.dag.models import SubTask

    task = SubTask(
        id=subtask_id,
        description=desc,
        overview=overview,
        proposed_changes=changes,
        acceptance_criteria=criteria,
        verification_plan=verification,
        footprint=footprint,
        symbols=symbols,
        depends_on=depends_on,
        risk=False,
        risk_reasons=(),
        **extra_subtask_fields,
    )
    return _render_issue_body(task, DEFAULT_TEMPLATE, parent_issue_number)


def test_restore_from_legacy_parent_with_children(tmp_path: Path) -> None:
    parent_plan = {
        "title": "EPIC Plan",
        "subtasks": [
            {
                "id": "task-1",
                "description": "First task",
                "issue_number": 101,
                "depends_on": [],
                "overview": "Old parent overview",
                "proposed_changes": ["Old change"],
                "acceptance_criteria": ["Old crit"],
                "verification_plan": ["Old verify"],
                "footprint": ["orchestune/old.py"],
                "symbols": ["old_sym"],
                "priority": "high",
                "risk": True,
                "risk_reasons": ["high risk task"],
            },
            {
                "id": "task-2",
                "description": "Second task (unprovisioned)",
                "issue_number": None,
                "depends_on": ["task-1"],
                "overview": "Unprovisioned overview",
                "proposed_changes": ["Unprovisioned change"],
                "acceptance_criteria": ["Unprovisioned crit"],
                "verification_plan": ["Unprovisioned verify"],
                "footprint": ["orchestune/task2.py"],
                "symbols": ["sym2"],
                "priority": "low",
                "risk": False,
                "risk_reasons": [],
            },
        ],
    }
    parent_body = embed_decomposition_plan_in_parent_body(
        "# EPIC Plan\nParent prose", parent_plan
    )

    child_body = _make_child_body(
        subtask_id="task-1",
        desc="First task",
        overview="New updated child overview",
        changes=("New updated child change",),
        criteria=("New updated criterion",),
        verification=("New verification",),
        footprint=("orchestune/child.py",),
        symbols=("child_sym",),
        depends_on=(),
        parent_issue_number=100,
    )

    forge = InMemoryForge(
        {
            100: IssueRecord(100, "EPIC Plan", parent_body, ("epic",), "open"),
            101: IssueRecord(101, "[FEAT] task-1: First task", child_body, (), "open"),
        }
    )

    out_file = tmp_path / "restored.md"
    result_path = restore_plan_file_from_parent(forge, 100, out_file)
    assert result_path == out_file

    restored_dict, _ = extract_frontmatter_and_body(
        out_file.read_text(encoding="utf-8")
    )
    assert restored_dict is not None
    assert "plan_format" not in restored_dict

    subtasks = restored_dict["subtasks"]
    assert len(subtasks) == 2

    # task-1 was provisioned: child values took precedence
    t1 = subtasks[0]
    assert t1["id"] == "task-1"
    assert t1["issue_number"] == 101
    assert t1["overview"] == "New updated child overview"
    assert t1["proposed_changes"] == ["New updated child change"]
    assert t1["acceptance_criteria"] == ["New updated criterion"]
    assert t1["verification_plan"] == ["New verification"]
    assert t1["footprint"] == ["orchestune/child.py"]
    assert t1["symbols"] == ["child_sym"]
    # Preserved parent-only metadata
    assert t1["priority"] == "high"
    assert t1["risk"] is True
    assert t1["risk_reasons"] == ["high risk task"]

    # task-2 was not provisioned: kept from parent
    t2 = subtasks[1]
    assert t2["id"] == "task-2"
    assert t2["issue_number"] is None
    assert t2["overview"] == "Unprovisioned overview"


def test_restore_from_manifest_parent(tmp_path: Path) -> None:
    manifest_plan = {
        "plan_format": "manifest-v1",
        "title": "Manifest EPIC",
        "subtasks": [
            {
                "id": "task-1",
                "description": "First task",
                "issue_number": 101,
                "depends_on": [],
                "priority": "medium",
                "risk": False,
                "risk_reasons": [],
            }
        ],
    }
    parent_body = embed_decomposition_plan_in_parent_body(
        "# Manifest EPIC\nParent prose", manifest_plan
    )

    child_body = _make_child_body(
        subtask_id="task-1",
        desc="First task",
        overview="Child overview",
        changes=("Child change",),
        criteria=("Child crit",),
        verification=("Child verify",),
        footprint=("orchestune/task1.py",),
        symbols=("task1_sym",),
        depends_on=(),
        parent_issue_number=100,
    )

    forge = InMemoryForge(
        {
            100: IssueRecord(100, "Manifest EPIC", parent_body, ("epic",), "open"),
            101: IssueRecord(101, "[FEAT] task-1: First task", child_body, (), "open"),
        }
    )

    out_file = tmp_path / "restored.md"
    result_path = restore_plan_file_from_parent(forge, 100, out_file)
    assert result_path == out_file

    restored_dict, _ = extract_frontmatter_and_body(
        out_file.read_text(encoding="utf-8")
    )
    assert restored_dict is not None
    assert "plan_format" not in restored_dict
    assert restored_dict["subtasks"][0]["overview"] == "Child overview"
    assert restored_dict["subtasks"][0]["footprint"] == ["orchestune/task1.py"]


def test_restore_rejects_structural_mismatch(tmp_path: Path) -> None:
    parent_plan = {
        "title": "EPIC Plan",
        "subtasks": [
            {
                "id": "task-1",
                "description": "Parent task description",
                "issue_number": 101,
                "depends_on": ["dep-a"],
            }
        ],
    }
    parent_body = embed_decomposition_plan_in_parent_body("Body", parent_plan)

    # Child has different description or depends_on
    child_body = _make_child_body(
        subtask_id="task-1",
        desc="Different task description",
        depends_on=(),
        parent_issue_number=100,
    )

    forge = InMemoryForge(
        {
            100: IssueRecord(100, "EPIC Plan", parent_body, (), "open"),
            101: IssueRecord(
                101, "[FEAT] task-1: Different task description", child_body, (), "open"
            ),
        }
    )

    out_file = tmp_path / "restored.md"
    with pytest.raises(ValueError, match="mismatch|description"):
        restore_plan_file_from_parent(forge, 100, out_file)


def test_restore_detects_concurrent_modification(tmp_path: Path) -> None:
    parent_plan = {
        "title": "EPIC Plan",
        "subtasks": [
            {
                "id": "task-1",
                "description": "Task 1",
                "issue_number": 101,
                "depends_on": [],
            }
        ],
    }
    parent_body = embed_decomposition_plan_in_parent_body("Body", parent_plan)
    child_body = _make_child_body("task-1", "Task 1", parent_issue_number=100)

    class ConcurrentlyModifyingForge(InMemoryForge):
        def get_issue(self, issue_number: int | str) -> IssueRecord | None:
            res = super().get_issue(issue_number)
            # When re-fetching parent on second read, modify it
            if int(issue_number) == 100 and self.call_count.get(100, 0) == 2 and res:
                return IssueRecord(
                    100,
                    res.title,
                    res.body + "\nconcurrent change",
                    res.labels,
                    res.state,
                )
            return res

    forge = ConcurrentlyModifyingForge(
        {
            100: IssueRecord(100, "EPIC Plan", parent_body, (), "open"),
            101: IssueRecord(101, "[FEAT] task-1: Task 1", child_body, (), "open"),
        }
    )

    out_file = tmp_path / "restored.md"
    out_file.write_text("existing content", encoding="utf-8")

    with pytest.raises(ValueError, match="concurrently"):
        restore_plan_file_from_parent(forge, 100, out_file)

    # Existing content should NOT be damaged
    assert out_file.read_text(encoding="utf-8") == "existing content"
