from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from orchestune.forge import RelationshipUnavailableError
from orchestune.issue_parsing import (
    decomposition_plan_from_parent_body,
    embed_decomposition_plan_in_parent_body,
    parent_issue_number_from_body,
)
from orchestune.models import IssueRecord, PrRecord
from orchestune.replan.apply import apply_replan
from orchestune.replan.audit import replan_audit_marker, retirement_marker
from orchestune.replan.plan import load_replan_plan
from orchestune.replan.preview import build_replan_preview
from orchestune.replan.snapshot import collect_replan_snapshot

PARENT = 693
OLD_PLAN = {
    "title": "Old plan",
    "parent_issue_number": PARENT,
    "parent_issue_source": "adopted",
    "subtasks": [
        {"id": "old-a", "description": "Old A", "issue_number": 10},
    ],
}
PLAN_TEXT = """\
---
title: New plan
parent_issue_number: 693
parent_issue_source: adopted
plan_revision: keep-this-local-value
subtasks:
  - id: task-a
    description: Prepare A
    priority: high
    footprint: [orchestune/a.py]
    symbols: [a.prepare]
    depends_on: []
    issue_number: null
  - id: task-b
    description: Prepare B
    priority: medium
    footprint: [orchestune/b.py]
    symbols: [b.prepare]
    depends_on: [task-a]
    issue_number: null
---

# New plan prose
"""
TEMPLATE = """\
# [FEAT] {{subtask_id}}: {{description}}

## Overview
{{overview}}

## Proposed Changes
{{proposed_changes}}

## Acceptance Criteria
{{acceptance_criteria}}

## Verification Plan
{{verification_plan}}

```yaml
subtask_id: {{subtask_id_yaml}}
footprint: {{footprint}}
symbols: {{symbols}}
depends_on: {{depends_on}}
shared_contract: {{shared_contract}}
writes_shared_contract: {{writes_shared_contract}}
parent_issue_number: {{parent_issue_number}}
execution_profile: {{execution_profile}}
model_tier: {{model_tier}}
```
"""


class FakeReplanForge:
    def __init__(self, *, old_status: str = "status:queued") -> None:
        parent_body = embed_decomposition_plan_in_parent_body(
            "Requirements stay here.\n\n## Human Notes\nDo not rewrite me.\n", OLD_PLAN
        )
        self.issues: dict[int, dict[str, Any]] = {
            PARENT: {
                "title": "[EPIC] Parent",
                "body": parent_body,
                "labels": [],
                "state": "OPEN",
            },
            10: {
                "title": "Old A",
                "body": "```yaml\nsubtask_id: old-a\nparent_issue_number: 693\n```\n",
                "labels": [old_status],
                "state": "OPEN",
            },
        }
        self.parents = {10: PARENT}
        self.blockers: dict[int, set[int]] = {}
        self.comments: dict[int, list[str]] = {}
        self.mutations: list[str] = []
        self.next_number = 100
        self.fail_after: str | None = None

    def _mutated(self, operation: str) -> None:
        self.mutations.append(operation)
        if self.fail_after == operation:
            self.fail_after = None
            raise RuntimeError(f"simulated crash after {operation}")

    def _record(self, number: int) -> IssueRecord:
        issue = self.issues[number]
        return IssueRecord(
            number,
            str(issue["title"]),
            str(issue["body"]),
            tuple(issue["labels"]),
            "",
            state=str(issue["state"]),
            parent=(
                {"number": self.parents[number]} if number in self.parents else None
            ),
            blocked_by=tuple(sorted(self.blockers.get(number, set()))),
        )

    def get_issue(self, issue_number: int | str) -> IssueRecord | None:
        number = int(issue_number)
        return self._record(number) if number in self.issues else None

    def list_sub_issues(self, parent_issue_number: int | str) -> list[IssueRecord]:
        parent = int(parent_issue_number)
        return [
            self._record(number)
            for number in sorted(self.issues)
            if self.parents.get(number) == parent
        ]

    def find_issues_by_parent_metadata(
        self, parent_issue_number: int | str
    ) -> list[IssueRecord]:
        parent = int(parent_issue_number)
        return [
            self._record(number)
            for number in sorted(self.issues)
            if number != parent
            and parent_issue_number_from_body(str(self.issues[number]["body"]))
            == parent
        ]

    def list_prs(self, state: str = "open", **_: object) -> list[PrRecord]:
        assert state == "merged"
        return []

    def create_issue(self, title: str, body: str, labels: Sequence[str] = ()) -> int:
        number = self.next_number
        self.next_number += 1
        self.issues[number] = {
            "title": title,
            "body": body,
            "labels": list(labels),
            "state": "OPEN",
        }
        self._mutated(f"create:{number}")
        return number

    def update_issue_body(self, issue_number: int | str, body: str) -> None:
        number = int(issue_number)
        self.issues[number]["body"] = body
        self._mutated("parent-body" if number == PARENT else f"body:{number}")

    def add_sub_issue(
        self, parent_issue_number: int | str, child_issue_number: int | str
    ) -> None:
        parent, child = int(parent_issue_number), int(child_issue_number)
        if self.parents.get(child) == parent:
            return
        self.parents[child] = parent
        self._mutated(f"parent:{child}")

    def remove_sub_issue(
        self, parent_issue_number: int | str, child_issue_number: int | str
    ) -> None:
        parent, child = int(parent_issue_number), int(child_issue_number)
        if self.parents.get(child) != parent:
            return
        del self.parents[child]
        self._mutated(f"detach:{child}")

    def set_blocked_by(
        self, issue_number: int | str, blocking_issue_number: int | str
    ) -> None:
        number, blocker = int(issue_number), int(blocking_issue_number)
        if blocker in self.blockers.setdefault(number, set()):
            return
        self.blockers[number].add(blocker)
        self._mutated(f"blocked:{number}:{blocker}")

    def add_label(self, issue_number: int | str, label: str) -> None:
        number = int(issue_number)
        labels = self.issues[number]["labels"]
        if label in labels:
            return
        labels.append(label)
        self._mutated(f"label+:{number}:{label}")

    def remove_label(self, issue_number: int | str, label: str) -> None:
        number = int(issue_number)
        labels = self.issues[number]["labels"]
        if label not in labels:
            return
        labels.remove(label)
        self._mutated(f"label-:{number}:{label}")

    def get_issue_labels(self, issue_number: int | str) -> tuple[str, ...]:
        return tuple(self.issues[int(issue_number)]["labels"])

    def get_issue_state(self, issue_number: int | str) -> str:
        return str(self.issues[int(issue_number)]["state"])

    def close_issue(
        self, issue_number: int | str, reason: str, comment: str | None = None
    ) -> None:
        assert reason == "not planned"
        assert comment is None
        number = int(issue_number)
        if self.issues[number]["state"] == "CLOSED":
            return
        self.issues[number]["state"] = "CLOSED"
        self._mutated(f"close:{number}")

    def add_comment(self, issue_number: int | str, body: str) -> None:
        number = int(issue_number)
        self.comments.setdefault(number, []).append(body)
        marker = "audit" if number == PARENT else "retirement"
        self._mutated(f"comment:{marker}:{number}")

    def list_comments(self, issue_number: int | str) -> list[dict[str, Any]]:
        return [
            {"body": body, "author": "test", "created_at": ""}
            for body in self.comments.get(int(issue_number), [])
        ]


class DegradedForge(FakeReplanForge):
    def add_sub_issue(
        self, parent_issue_number: int | str, child_issue_number: int | str
    ) -> None:
        if int(child_issue_number) == 10:
            return super().add_sub_issue(parent_issue_number, child_issue_number)
        raise RelationshipUnavailableError("native parents unavailable")

    def remove_sub_issue(
        self, parent_issue_number: int | str, child_issue_number: int | str
    ) -> None:
        raise RelationshipUnavailableError("native parents unavailable")

    def set_blocked_by(
        self, issue_number: int | str, blocking_issue_number: int | str
    ) -> None:
        raise RelationshipUnavailableError("native dependencies unavailable")


@pytest.fixture
def replan_files(tmp_path: Path) -> tuple[Path, Path]:
    plan = tmp_path / "decomposition_plan.md"
    plan.write_text(PLAN_TEXT, encoding="utf-8")
    template = tmp_path / "issue_template.md"
    template.write_text(TEMPLATE, encoding="utf-8")
    return plan, template


def preview_token(forge: FakeReplanForge, plan: Path) -> str:
    return build_replan_preview(
        load_replan_plan(plan), collect_replan_snapshot(forge, PARENT)
    ).preview_token


def test_apply_rejects_stale_token_before_the_first_write(
    replan_files: tuple[Path, Path],
) -> None:
    plan, template = replan_files
    forge = FakeReplanForge()

    with pytest.raises(ValueError, match="preview token"):
        apply_replan(
            plan,
            "replan-preview-v1:sha256:" + "0" * 64,
            forge=forge,
            template_path=template,
            repo_root=plan.parent,
        )

    assert forge.mutations == []
    assert "issue_number: null" in plan.read_text(encoding="utf-8")


def test_apply_rejects_an_unsafe_old_generation_before_writing(
    replan_files: tuple[Path, Path],
) -> None:
    plan, template = replan_files
    forge = FakeReplanForge(old_status="status:in-progress")

    with pytest.raises(ValueError, match="cannot be applied automatically"):
        apply_replan(
            plan,
            preview_token(forge, plan),
            forge=forge,
            template_path=template,
            repo_root=plan.parent,
        )

    assert forge.mutations == []


def test_apply_prepares_new_generation_then_retires_and_switches_parent_plan(
    replan_files: tuple[Path, Path],
) -> None:
    plan, template = replan_files
    forge = FakeReplanForge()

    result = apply_replan(
        plan,
        preview_token(forge, plan),
        forge=forge,
        template_path=template,
        repo_root=plan.parent,
    )

    assert result.created_issue_numbers == (100, 101)
    assert result.reused_issue_numbers == ()
    assert result.retired_issue_numbers == (10,)
    assert result.degraded is False
    assert forge.parents == {100: PARENT, 101: PARENT}
    assert forge.blockers == {101: {100}}
    assert forge.issues[10]["labels"] == ["status:not-needed"]
    assert forge.issues[10]["state"] == "CLOSED"
    assert retirement_marker(result.plan_revision) in forge.comments[10][0]
    assert replan_audit_marker(result.plan_revision) in forge.comments[PARENT][0]

    operations = forge.mutations
    assert max(
        operations.index("parent:100"), operations.index("parent:101")
    ) < operations.index("comment:retirement:10")
    assert operations.index("detach:10") < operations.index("parent-body")
    assert operations.index("parent-body") < operations.index(f"comment:audit:{PARENT}")

    parent_body = str(forge.issues[PARENT]["body"])
    assert "Requirements stay here." in parent_body
    assert "## Human Notes\nDo not rewrite me." in parent_body
    stored = decomposition_plan_from_parent_body(parent_body)
    assert stored is not None
    assert [entry["issue_number"] for entry in stored["subtasks"]] == [100, 101]
    local = plan.read_text(encoding="utf-8")
    assert "plan_revision: keep-this-local-value" in local
    assert "issue_number: 100" in local
    assert "issue_number: 101" in local
    assert "body:10" not in operations


def test_same_subtask_id_still_creates_a_new_revision_issue(
    replan_files: tuple[Path, Path],
) -> None:
    plan, template = replan_files
    forge = FakeReplanForge()
    old_plan = dict(OLD_PLAN)
    old_plan["subtasks"] = [
        {"id": "task-a", "description": "Old A", "issue_number": 10}
    ]
    forge.issues[PARENT]["body"] = embed_decomposition_plan_in_parent_body(
        str(forge.issues[PARENT]["body"]), old_plan
    )
    forge.issues[10]["body"] = str(forge.issues[10]["body"]).replace(
        "subtask_id: old-a", "subtask_id: task-a"
    )

    result = apply_replan(
        plan,
        preview_token(forge, plan),
        forge=forge,
        template_path=template,
        repo_root=plan.parent,
    )

    assert result.created_issue_numbers == (100, 101)
    assert result.reused_issue_numbers == ()
    assert result.retired_issue_numbers == (10,)


def test_invalid_template_is_rejected_before_writes(
    replan_files: tuple[Path, Path],
) -> None:
    plan, template = replan_files
    template.write_text("missing identity fields", encoding="utf-8")
    forge = FakeReplanForge()

    with pytest.raises(ValueError, match="subtask_id"):
        apply_replan(
            plan,
            preview_token(forge, plan),
            forge=forge,
            template_path=template,
            repo_root=plan.parent,
        )

    assert forge.mutations == []


def test_relationship_capability_absence_returns_a_degraded_result(
    replan_files: tuple[Path, Path],
) -> None:
    plan, template = replan_files
    forge = DegradedForge()

    result = apply_replan(
        plan,
        preview_token(forge, plan),
        forge=forge,
        template_path=template,
        repo_root=plan.parent,
    )

    assert result.degraded is True
    assert parent_issue_number_from_body(str(forge.issues[100]["body"])) == PARENT
    assert "depends_on: [task-a]" in str(forge.issues[101]["body"])


def test_completed_generation_is_a_write_free_no_op(
    replan_files: tuple[Path, Path],
) -> None:
    plan, template = replan_files
    forge = FakeReplanForge()
    apply_replan(
        plan,
        preview_token(forge, plan),
        forge=forge,
        template_path=template,
        repo_root=plan.parent,
    )
    forge.mutations.clear()

    result = apply_replan(
        plan,
        preview_token(forge, plan),
        forge=forge,
        template_path=template,
        repo_root=plan.parent,
    )

    assert result.created_issue_numbers == ()
    assert result.reused_issue_numbers == (100, 101)
    assert result.retired_issue_numbers == ()
    assert forge.mutations == []


def test_unrelated_parent_comments_do_not_invalidate_preview_token(
    replan_files: tuple[Path, Path],
) -> None:
    plan, template = replan_files
    forge = FakeReplanForge()
    token = preview_token(forge, plan)

    # An unrelated bot comment or notification is posted to the parent issue
    forge.comments.setdefault(PARENT, []).append(
        "<!-- orchestune:issue-notice --> Automated notification: branch rebased."
    )

    # apply_replan should succeed with the original token without raising stale token error!
    result = apply_replan(
        plan,
        token,
        forge=forge,
        template_path=template,
        repo_root=plan.parent,
    )
    assert result.created_issue_numbers == (100, 101)


def test_switch_parent_plan_oversized_body_returns_degraded_without_raising(
    replan_files: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan, template = replan_files
    forge = FakeReplanForge()
    token = preview_token(forge, plan)

    plan_data = {
        "title": "Old plan",
        "parent_issue_number": PARENT,
        "parent_issue_source": "adopted",
        "subtasks": [{"id": "old-a", "description": "Old A", "issue_number": 10}],
    }
    oversized_prose = "x" * 65400 + "\n"
    forge.issues[PARENT]["body"] = embed_decomposition_plan_in_parent_body(
        oversized_prose, plan_data
    )

    result = apply_replan(
        plan,
        token,
        forge=forge,
        template_path=template,
        repo_root=plan.parent,
    )

    assert result.degraded is True
    assert "over GitHub's 65536 character limit" in capsys.readouterr().err
    assert "Native relationship result: degraded" in forge.comments[PARENT][0]

    # Subsequent retry does not crash and completes with degraded status
    retry_token = preview_token(forge, plan)
    retry_result = apply_replan(
        plan,
        retry_token,
        forge=forge,
        template_path=template,
        repo_root=plan.parent,
    )
    assert retry_result.degraded is True
