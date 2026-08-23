from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from orchestune.models import IssueRecord

_TEMPLATE = (
    "# [FEAT] {{subtask_id}}: {{description}}\n\n"
    "## Overview\n{{overview}}\n\n"
    "## Proposed Changes\n{{proposed_changes}}\n\n"
    "## Acceptance Criteria\n{{acceptance_criteria}}\n\n"
    "## Verification Plan\n{{verification_plan}}\n\n"
    "```yaml\n"
    "subtask_id: {{subtask_id_yaml}}\n"
    "footprint: {{footprint}}\n"
    "symbols: {{symbols}}\n"
    "depends_on: {{depends_on}}\n"
    "parent_issue_number: {{parent_issue_number}}\n"
    "```\n"
)

_PLAN = """\
---
title: "Example big rock"
parent_issue_number: null
subtasks:
  - id: task-a
    description: "Implement feature XX"
    priority: high
    footprint: [src/foo.py]
    symbols: [foo.Foo]
    depends_on: []
    issue_number: null
  - id: task-b
    description: "Implement feature YY"
    priority: medium
    depends_on: [task-a]
    issue_number: null
---

# Decomposition Plan
"""


class FakeForge:
    """Minimal in-memory IssueForge double; no mock.patch.

    Only the methods `provisioning.py` actually calls have real behaviour;
    the rest are stubs present solely to satisfy the `IssueForge` protocol.
    """

    def __init__(self) -> None:
        self._next_number = 100
        self.issues: dict[int, dict] = {}
        self.sub_issues: dict[int, list[int]] = {}
        self.blocked_by: dict[int, list[int]] = {}
        self.create_issue_calls: list[tuple[str, str, tuple[str, ...]]] = []

    def create_issue(self, title: str, body: str, labels: Sequence[str] = ()) -> int:
        number = self._next_number
        self._next_number += 1
        self.issues[number] = {"title": title, "body": body, "labels": list(labels)}
        self.create_issue_calls.append((title, body, tuple(labels)))
        return number

    def update_issue_body(self, issue_number: int | str, body: str) -> None:
        self.issues[int(issue_number)]["body"] = body

    def update_issue_title(self, issue_number: int | str, title: str) -> None:
        self.issues[int(issue_number)]["title"] = title

    def add_sub_issue(
        self, parent_issue_number: int | str, child_issue_number: int | str
    ) -> None:
        self.sub_issues.setdefault(int(parent_issue_number), []).append(
            int(child_issue_number)
        )

    def set_blocked_by(
        self, issue_number: int | str, blocking_issue_number: int | str
    ) -> None:
        self.blocked_by.setdefault(int(issue_number), []).append(
            int(blocking_issue_number)
        )

    def list_sub_issues(self, parent_issue_number: int | str) -> list[IssueRecord]:
        numbers = self.sub_issues.get(int(parent_issue_number), [])
        return [
            IssueRecord(
                number=number,
                title=self.issues[number]["title"],
                body=self.issues[number]["body"],
                labels=tuple(self.issues[number]["labels"]),
                created_at="",
                # A record only reaches here via a real native sub-issue
                # relationship, so it has a native parent by definition.
                parent={"number": int(parent_issue_number)},
            )
            for number in numbers
        ]

    def get_issue_labels(self, issue_number: int | str) -> tuple[str, ...]:
        return tuple(self.issues[int(issue_number)]["labels"])

    def find_open_issues_by_exact_title(self, title: str) -> list[IssueRecord]:
        return [
            IssueRecord(
                number=number,
                title=entry["title"],
                body=entry["body"],
                labels=tuple(entry["labels"]),
                created_at="",
            )
            for number, entry in self.issues.items()
            if entry["title"] == title
        ]

    def get_issue(self, issue_number: int | str) -> IssueRecord | None:
        entry = self.issues.get(int(issue_number))
        if entry is None:
            return None
        number = int(issue_number)
        native_parent = next(
            (
                {"number": parent}
                for parent, children in self.sub_issues.items()
                if number in children
            ),
            None,
        )
        return IssueRecord(
            number=number,
            title=entry["title"],
            body=entry["body"],
            labels=tuple(entry["labels"]),
            created_at="",
            state=entry.get("state", "OPEN"),
            parent=native_parent,
        )

    def find_issues_by_parent_metadata(
        self, parent_issue_number: int | str
    ) -> list[IssueRecord]:
        """No metadata-search backend by default (mirrors a plain `gh`-based
        forge); subclasses override this to exercise the #485 fallback."""
        return []

    # --- Unused by provisioning.py; present only for IssueForge conformance. ---

    def list_issues_by_label(
        self, label: str, state: str = "open", limit: int = 1000
    ) -> list[IssueRecord]:
        raise NotImplementedError

    def add_label(self, issue_number: int | str, label: str) -> None:
        raise NotImplementedError

    def remove_label(self, issue_number: int | str, label: str) -> None:
        raise NotImplementedError

    def close_issue(
        self, issue_number: int | str, reason: str, comment: str | None = None
    ) -> None:
        raise NotImplementedError

    def add_comment(self, issue_number: int | str, body: str) -> None:
        raise NotImplementedError

    def list_comments(self, issue_number: int | str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_issue_state(self, issue_number: int | str) -> str:
        raise NotImplementedError

    def get_label_actor(self, issue_number: int | str, label: str) -> str:
        raise NotImplementedError

    def get_actor_permission(self, username: str) -> str:
        raise NotImplementedError

    def get_issue_last_reopened_at(self, issue_number: int | str) -> str | None:
        raise NotImplementedError


@pytest.fixture
def plan_path(tmp_path: Path) -> Path:
    path = tmp_path / "decomposition_plan.md"
    path.write_text(_PLAN, encoding="utf-8")
    return path


@pytest.fixture
def template_path(tmp_path: Path) -> Path:
    path = tmp_path / "issue_template.md"
    path.write_text(_TEMPLATE, encoding="utf-8")
    return path
