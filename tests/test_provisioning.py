from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from orchestune.dag_models import SubTask
from orchestune.issue_parsing import FOOTPRINT_BLOCK_PATTERN
from orchestune.models import IssueRecord
from orchestune.provisioning import (
    _PARENT_MARKER,
    _derive_labels,
    _parent_body,
    _render_issue_body,
    _subtask_id_from_body,
    main,
    provision_issues,
)

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


class TestDeriveLabels:
    def _subtask(
        self,
        *,
        depends_on: tuple[str, ...] = (),
        risk: bool = False,
        priority: str = "medium",
    ) -> SubTask:
        return SubTask(
            id="x",
            description="d",
            footprint=(),
            symbols=(),
            depends_on=depends_on,
            risk=risk,
            risk_reasons=(),
            priority=priority,
        )

    def test_no_dependencies_is_queued(self):
        labels = _derive_labels(self._subtask(depends_on=()), dependencies_done=False)
        assert labels == ("status:queued", "priority:medium")

    def test_unresolved_dependency_is_blocked(self):
        labels = _derive_labels(
            self._subtask(depends_on=("y",)), dependencies_done=False
        )
        assert labels[0] == "status:blocked"

    def test_resolved_dependencies_is_queued(self):
        labels = _derive_labels(
            self._subtask(depends_on=("y",)), dependencies_done=True
        )
        assert labels[0] == "status:queued"

    def test_risk_flag_appends_label(self):
        labels = _derive_labels(self._subtask(risk=True), dependencies_done=False)
        assert "risk:flagged" in labels

    def test_priority_label_reflects_field(self):
        labels = _derive_labels(self._subtask(priority="low"), dependencies_done=False)
        assert "priority:low" in labels


class TestRenderIssueBodySubtaskIdSafety:
    """#323 review (P2): a subtask id containing `:` or `#` must still
    round-trip through the rendered Footprint YAML block."""

    def _subtask(self, subtask_id: str) -> SubTask:
        return SubTask(
            id=subtask_id,
            description="d",
            footprint=(),
            symbols=(),
            depends_on=(),
            risk=False,
            risk_reasons=(),
        )

    @pytest.mark.parametrize(
        "subtask_id", ["auth: login", "task#1", "plain-id", "setup-database"]
    )
    def test_id_round_trips_through_rendered_yaml_block(self, subtask_id):
        # Use the real (multi-key) template: a bug that only manifests when
        # more YAML follows `subtask_id:` in the fence (see the `...`
        # document-terminator regression below) wouldn't show up in a
        # single-key fence.
        body = _render_issue_body(self._subtask(subtask_id), _TEMPLATE)

        # The heading keeps the raw (human-readable) id.
        assert body.startswith(f"# [FEAT] {subtask_id}: d")

        # The Footprint block is valid YAML and yields the exact id back,
        # including the fields declared after `subtask_id:` in the fence.
        match = FOOTPRINT_BLOCK_PATTERN.search(body)
        assert match
        data = yaml.safe_load(match.group(1))
        assert data["subtask_id"] == subtask_id
        assert "footprint" in data  # would be silently dropped by a `...` marker
        assert _subtask_id_from_body(body) == subtask_id

    def test_plain_id_scalar_has_no_yaml_document_terminator(self):
        """#323 review (P1): `yaml.dump("task-a")` emits a trailing `...`
        document-end marker for bare scalars; embedding that verbatim turns
        the rest of the Footprint block into an unparseable second
        document."""
        body = _render_issue_body(self._subtask("task-a"), _TEMPLATE)
        match = FOOTPRINT_BLOCK_PATTERN.search(body)
        assert match
        assert "..." not in match.group(1)

    def test_a_fields_own_value_is_not_reprocessed_as_a_template_token(self):
        """#323 review (P2): substituting one field at a time (as opposed to
        a single pass over the original template) means an earlier field's
        *value* can itself contain a literal `{{token}}`, which then gets
        corrupted by a later placeholder's substitution — even though that
        text was never part of the template."""
        subtask = SubTask(
            id="x",
            description="Preserve {{overview}} in the template",
            footprint=(),
            symbols=(),
            depends_on=(),
            risk=False,
            risk_reasons=(),
            overview="THE REAL OVERVIEW",
        )

        body = _render_issue_body(subtask, _TEMPLATE)

        assert "Preserve {{overview}} in the template" in body
        assert "Preserve THE REAL OVERVIEW in the template" not in body


class TestProvisionIssuesApply:
    def test_creates_parent_and_subtasks_in_topological_order(
        self, plan_path: Path, template_path: Path
    ):
        forge = FakeForge()
        result = provision_issues(plan_path, forge=forge, template_path=template_path)

        assert result.applied is True
        assert result.parent_issue_number is not None
        assert set(result.created) == {"task-a", "task-b"}
        # task-a must be created before task-b (topological order).
        titles_in_order = [call[0] for call in forge.create_issue_calls]
        assert any("task-a" in title for title in titles_in_order[:-1])

    def test_recovers_orphaned_parent_by_title_before_creating_duplicate(
        self, plan_path: Path, template_path: Path
    ):
        """#323 review (P1): if a prior run created the parent issue but
        crashed (or failed to write) before persisting parent_issue_number,
        the next run must find it by exact title rather than creating a
        second EPIC and splitting the sub-issue hierarchy."""
        forge = FakeForge()
        orphaned_parent = forge.create_issue(
            "[EPIC] Example big rock", _parent_body("Example big rock")
        )

        result = provision_issues(plan_path, forge=forge, template_path=template_path)

        assert result.parent_issue_number == orphaned_parent
        epic_issues = [
            entry
            for entry in forge.issues.values()
            if entry["title"] == "[EPIC] Example big rock"
        ]
        assert len(epic_issues) == 1  # no duplicate EPIC

    def test_does_not_adopt_an_unrelated_issue_with_the_same_title(
        self, plan_path: Path, template_path: Path
    ):
        """#323 review (P2): an exact title match alone isn't proof that an
        issue came from a prior run of this command — a coincidentally
        same-titled, unrelated issue must not have every generated subtask
        attached beneath it. Only adopt a match that also carries our
        marker in its body."""
        forge = FakeForge()
        unrelated = forge.create_issue(
            "[EPIC] Example big rock", "Some unrelated human-written issue."
        )
        assert _PARENT_MARKER not in forge.issues[unrelated]["body"]

        result = provision_issues(plan_path, forge=forge, template_path=template_path)

        assert result.parent_issue_number != unrelated
        assert result.parent_issue_number is not None
        assert _PARENT_MARKER in forge.issues[result.parent_issue_number]["body"]
        epic_issues = [
            entry
            for entry in forge.issues.values()
            if entry["title"] == "[EPIC] Example big rock"
        ]
        assert len(epic_issues) == 2  # the unrelated one, plus our own new EPIC

    def test_finds_marked_parent_among_multiple_same_titled_candidates(
        self, plan_path: Path, template_path: Path
    ):
        """#323 review (P2): if an unrelated same-titled issue AND our own
        orphaned marked parent both exist, the marked one (wherever it
        appears in the search results) must be selected — checking only
        the first exact-title match would miss it and create a duplicate."""
        forge = FakeForge()
        unrelated = forge.create_issue(
            "[EPIC] Example big rock", "Some unrelated human-written issue."
        )
        our_orphan = forge.create_issue(
            "[EPIC] Example big rock", _parent_body("Example big rock")
        )
        assert unrelated < our_orphan  # unrelated appears first in FakeForge order

        result = provision_issues(plan_path, forge=forge, template_path=template_path)

        assert result.parent_issue_number == our_orphan
        epic_issues = [
            entry
            for entry in forge.issues.values()
            if entry["title"] == "[EPIC] Example big rock"
        ]
        assert len(epic_issues) == 2  # no third (duplicate) EPIC created

    def test_links_sub_issue_and_blocked_by(self, plan_path: Path, template_path: Path):
        forge = FakeForge()
        result = provision_issues(plan_path, forge=forge, template_path=template_path)

        parent = result.parent_issue_number
        assert parent is not None
        task_a_number = result.created["task-a"]
        task_b_number = result.created["task-b"]
        assert task_a_number in forge.sub_issues[parent]
        assert task_b_number in forge.sub_issues[parent]
        assert forge.blocked_by[task_b_number] == [task_a_number]

    def test_writes_issue_numbers_back_to_plan(
        self, plan_path: Path, template_path: Path
    ):
        forge = FakeForge()
        result = provision_issues(plan_path, forge=forge, template_path=template_path)

        text = plan_path.read_text(encoding="utf-8")
        assert f"parent_issue_number: {result.parent_issue_number}" in text
        assert f"issue_number: {result.created['task-a']}" in text
        assert f"issue_number: {result.created['task-b']}" in text

    def test_rerun_reuses_issue_numbers_without_creating_duplicates(
        self, plan_path: Path, template_path: Path
    ):
        forge = FakeForge()
        first = provision_issues(plan_path, forge=forge, template_path=template_path)
        second = provision_issues(plan_path, forge=forge, template_path=template_path)

        assert second.created == {}
        assert second.reused == {
            "task-a": first.created["task-a"],
            "task-b": first.created["task-b"],
        }
        assert len(forge.issues) == 3  # parent + 2 subtasks, no duplicates

    def test_partial_failure_then_resume_does_not_duplicate_completed_subtasks(
        self, plan_path: Path, template_path: Path
    ):
        class FlakyForge(FakeForge):
            def __init__(self):
                super().__init__()
                self.fail_next_blocked_by = True

            def set_blocked_by(self, issue_number, blocking_issue_number) -> None:
                if self.fail_next_blocked_by:
                    self.fail_next_blocked_by = False
                    raise RuntimeError("simulated transient failure")
                super().set_blocked_by(issue_number, blocking_issue_number)

        flaky = FlakyForge()
        with pytest.raises(RuntimeError):
            provision_issues(plan_path, forge=flaky, template_path=template_path)

        # task-a (and the parent) must have been persisted before the crash.
        assert (
            "task-a"
            in [entry["title"].split(": ", 1)[0] for entry in flaky.issues.values()]
            or len(flaky.issues) >= 2
        )

        resumed = provision_issues(plan_path, forge=flaky, template_path=template_path)
        assert "task-a" in resumed.reused
        assert len(flaky.issues) == 3  # no duplicate of task-a created on resume

    def test_finds_existing_sub_issue_by_subtask_id_when_issue_number_unset(
        self, plan_path: Path, template_path: Path
    ):
        forge = FakeForge()
        parent = forge.create_issue("[EPIC] Example big rock", "body")
        existing_task_a = forge.create_issue(
            "[FEAT] task-a: pre-filed",
            "```yaml\nsubtask_id: task-a\n```\n",
        )
        forge.add_sub_issue(parent, existing_task_a)

        from orchestune.plan_writer import write_issue_numbers

        write_issue_numbers(plan_path, parent_issue_number=parent)

        result = provision_issues(plan_path, forge=forge, template_path=template_path)
        assert result.reused["task-a"] == existing_task_a
        assert "task-a" not in result.created

    def test_orphaned_issue_is_persisted_and_not_duplicated_on_resume(
        self, plan_path: Path, template_path: Path
    ):
        """#323 review (P1): if create_issue succeeds but add_sub_issue then
        fails, the issue isn't linked as a sub-issue yet, so the subtask_id
        search over the parent's children can't find it either. Without an
        immediate write-back, a naive retry would create a duplicate."""

        class FlakyForge(FakeForge):
            def __init__(self):
                super().__init__()
                self.fail_next_add_sub_issue = True

            def add_sub_issue(self, parent_issue_number, child_issue_number) -> None:
                if self.fail_next_add_sub_issue:
                    self.fail_next_add_sub_issue = False
                    raise RuntimeError("simulated transient failure")
                super().add_sub_issue(parent_issue_number, child_issue_number)

        flaky = FlakyForge()
        with pytest.raises(RuntimeError):
            provision_issues(plan_path, forge=flaky, template_path=template_path)

        # task-a's issue was created (orphaned: not yet linked as a sub-issue)
        # but its number must already be durably written back.
        assert len(flaky.issues) == 2  # parent + task-a
        assert "issue_number: 101" in plan_path.read_text(encoding="utf-8")

        resumed = provision_issues(plan_path, forge=flaky, template_path=template_path)
        assert resumed.reused.get("task-a") == 101
        assert len(flaky.issues) == 3  # no duplicate of task-a
        parent = resumed.parent_issue_number
        assert parent is not None
        assert 101 in flaky.sub_issues[parent]  # orphan got linked on retry

    def test_reused_issue_missing_a_blocker_gets_it_reconciled_on_resume(
        self, tmp_path: Path, template_path: Path
    ):
        """#323 review (P1): a subtask with 2+ dependencies where the first
        set_blocked_by succeeds and a later one fails must have the missing
        blocker restored on retry, even though its own issue is reused."""
        plan = tmp_path / "decomposition_plan.md"
        plan.write_text(
            "---\n"
            'title: "Example big rock"\n'
            "parent_issue_number: null\n"
            "subtasks:\n"
            "  - id: task-a\n"
            '    description: "A"\n'
            "    depends_on: []\n"
            "    issue_number: null\n"
            "  - id: task-b\n"
            '    description: "B"\n'
            "    depends_on: []\n"
            "    issue_number: null\n"
            "  - id: task-c\n"
            '    description: "C"\n'
            "    depends_on: [task-a, task-b]\n"
            "    issue_number: null\n"
            "---\n",
            encoding="utf-8",
        )

        class FlakyForge(FakeForge):
            def __init__(self):
                super().__init__()
                self.blocked_by_call_count = 0

            def set_blocked_by(self, issue_number, blocking_issue_number) -> None:
                self.blocked_by_call_count += 1
                if self.blocked_by_call_count == 2:
                    raise RuntimeError("simulated transient failure")
                super().set_blocked_by(issue_number, blocking_issue_number)

        flaky = FlakyForge()
        with pytest.raises(RuntimeError):
            provision_issues(plan, forge=flaky, template_path=template_path)

        task_c_number = next(
            number
            for number, entry in flaky.issues.items()
            if entry["title"].startswith("[FEAT] task-c")
        )
        # Only the first dependency's blocked-by call landed before the crash.
        assert len(flaky.blocked_by.get(task_c_number, [])) == 1

        provision_issues(plan, forge=flaky, template_path=template_path)
        task_a_number = next(
            number
            for number, entry in flaky.issues.items()
            if entry["title"].startswith("[FEAT] task-a")
        )
        task_b_number = next(
            number
            for number, entry in flaky.issues.items()
            if entry["title"].startswith("[FEAT] task-b")
        )
        assert set(flaky.blocked_by[task_c_number]) == {task_a_number, task_b_number}


class TestProvisionIssuesNoApply:
    def test_no_apply_makes_no_forge_calls_and_returns_preview(
        self, plan_path: Path, template_path: Path
    ):
        class ExplodingForge:
            def __getattr__(self, name):
                raise AssertionError(f"forge.{name} must not be called in --no-apply")

        result = provision_issues(
            plan_path, forge=ExplodingForge(), apply=False, template_path=template_path
        )

        assert result.applied is False
        assert result.created == {}
        assert result.reused == {}
        subtask_ids = [p.subtask_id for p in result.previews]
        assert subtask_ids == ["task-a", "task-b"]
        assert "[FEAT] task-a: Implement feature XX" == result.previews[0].title
        assert "status:queued" in result.previews[0].labels
        assert "status:blocked" in result.previews[1].labels


class TestMain:
    def test_no_apply_prints_preview_and_exits_0(
        self, plan_path: Path, template_path: Path, capsys
    ):
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "--plan",
                    str(plan_path),
                    "--template",
                    str(template_path),
                    "--no-apply",
                ]
            )
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "Dry run" in captured.out
        assert "task-a" in captured.out

    def test_apply_mode_prints_summary_and_exits_0(
        self, plan_path: Path, template_path: Path, capsys, monkeypatch
    ):
        forge = FakeForge()
        monkeypatch.setattr("orchestune.provisioning.GitHubForge", lambda: forge)
        with pytest.raises(SystemExit) as exc_info:
            main(["--plan", str(plan_path), "--template", str(template_path)])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "Parent issue:" in captured.out
        assert "task-a" in captured.out

    def test_missing_plan_file_exits_1_with_error(self, tmp_path: Path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(["--plan", str(tmp_path / "nonexistent.md")])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Error:" in captured.err


def test_missing_title_raises(tmp_path: Path, template_path: Path):
    path = tmp_path / "decomposition_plan.md"
    path.write_text(
        "---\nsubtasks:\n  - id: task-a\n    description: x\n---\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="title"):
        provision_issues(path, forge=FakeForge(), template_path=template_path)
