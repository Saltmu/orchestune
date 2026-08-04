from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from orchestune.dag_models import SubTask
from orchestune.issue_parsing import (
    FOOTPRINT_BLOCK_PATTERN,
    PARENT_MARKER,
    is_epic_issue,
)
from orchestune.models import IssueRecord
from orchestune.provisioning import (
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

    def get_issue(self, issue_number: int | str) -> IssueRecord | None:
        entry = self.issues.get(int(issue_number))
        if entry is None:
            return None
        return IssueRecord(
            number=int(issue_number),
            title=entry["title"],
            body=entry["body"],
            labels=tuple(entry["labels"]),
            created_at="",
        )

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

        # Verify parent issue body includes the markdown description
        parent_call = forge.create_issue_calls[0]
        assert parent_call[0] == "[EPIC] Example big rock"
        assert "# Decomposition Plan" in parent_call[1]

    def test_parent_body_extraction_handles_embedded_dashes_in_yaml(
        self, tmp_path: Path, template_path: Path
    ):
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "Example --- big rock with dashes"\n'
            "subtasks:\n"
            '  - id: "task-a"\n'
            '    description: "d"\n'
            "---\n"
            "\n"
            "# Real Description\n"
            "This is the actual markdown content.\n",
            encoding="utf-8",
        )
        forge = FakeForge()
        result = provision_issues(path, forge=forge, template_path=template_path)

        assert result.parent_issue_number is not None
        parent_call = forge.create_issue_calls[0]
        assert parent_call[0] == "[EPIC] Example --- big rock with dashes"
        assert "# Real Description" in parent_call[1]
        assert "This is the actual markdown content." in parent_call[1]
        assert "subtasks:" not in parent_call[1]

    def test_parent_body_extraction_preserves_indented_code_block(
        self, tmp_path: Path, template_path: Path
    ):
        """#352 review (P2): the closing `---` delimiter's trailing `\\s*`
        must not swallow the leading indentation of a Markdown body that
        opens with an indented code block."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "Example big rock"\n'
            "subtasks:\n"
            '  - id: "task-a"\n'
            '    description: "d"\n'
            "---\n"
            "\n"
            '    print("hello")\n'
            "\n"
            "Regular paragraph.\n",
            encoding="utf-8",
        )
        forge = FakeForge()
        result = provision_issues(path, forge=forge, template_path=template_path)

        assert result.parent_issue_number is not None
        parent_call = forge.create_issue_calls[0]
        assert '    print("hello")' in parent_call[1]

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
        assert PARENT_MARKER not in forge.issues[unrelated]["body"]

        result = provision_issues(plan_path, forge=forge, template_path=template_path)

        assert result.parent_issue_number != unrelated
        assert result.parent_issue_number is not None
        assert PARENT_MARKER in forge.issues[result.parent_issue_number]["body"]
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

    def test_does_not_trust_a_stale_persisted_parent_issue_number(
        self, plan_path: Path, template_path: Path
    ):
        """#323 review round 7 (P2): an already-set `parent_issue_number`
        was used with zero verification, unlike a persisted subtask number
        (which is checked against `get_issue` + the marker). A stale parent
        number (e.g. the plan copied to another repo, where that number now
        belongs to an unrelated issue) must not have every subtask attached
        beneath it either."""
        forge = FakeForge()
        unrelated = forge.create_issue(
            "Someone else's real issue", "Nothing to do with this plan."
        )

        from orchestune.plan_writer import write_issue_numbers

        write_issue_numbers(plan_path, parent_issue_number=unrelated)

        result = provision_issues(plan_path, forge=forge, template_path=template_path)

        assert result.parent_issue_number != unrelated
        assert forge.sub_issues.get(unrelated, []) == []
        new_parent = result.parent_issue_number
        assert new_parent is not None
        assert PARENT_MARKER in forge.issues[new_parent]["body"]

    def test_does_not_trust_a_persisted_parent_from_a_different_plan(
        self, plan_path: Path, template_path: Path
    ):
        """#323 review round 8 (P2): `PARENT_MARKER` is a single constant
        shared by every EPIC this module ever creates, so carrying it alone
        can't distinguish this plan's own parent from an EPIC created for a
        *different* plan (e.g. a colliding issue number in another
        Orchestune-managed repo). The persisted-number verification must
        also check the title, the same requirement the title-recovery
        search below it already applies."""
        forge = FakeForge()
        other_plans_parent = forge.create_issue(
            "[EPIC] A completely different plan",
            _parent_body("A completely different plan"),
        )

        from orchestune.plan_writer import write_issue_numbers

        write_issue_numbers(plan_path, parent_issue_number=other_plans_parent)

        result = provision_issues(plan_path, forge=forge, template_path=template_path)

        assert result.parent_issue_number != other_plans_parent
        assert forge.sub_issues.get(other_plans_parent, []) == []
        new_parent = result.parent_issue_number
        assert new_parent is not None
        assert forge.issues[new_parent]["title"] == "[EPIC] Example big rock"

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
        parent = forge.create_issue(
            "[EPIC] Example big rock", _parent_body("Example big rock")
        )
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

    def test_does_not_mutate_an_unrelated_issue_with_a_stale_persisted_number(
        self, plan_path: Path, template_path: Path
    ):
        """#323 review (P2): a plan's persisted issue_number could be stale
        (e.g. the plan was copied to another repo where that number now
        belongs to an unrelated issue). It must not be trusted just because
        it's a valid positive integer — an unrelated issue must not get
        reparented or blocked-by relationships added."""
        forge = FakeForge()
        unrelated = forge.create_issue(
            "Someone else's real issue", "Nothing to do with this plan."
        )

        from orchestune.plan_writer import write_issue_numbers

        write_issue_numbers(plan_path, {"task-a": unrelated})

        result = provision_issues(plan_path, forge=forge, template_path=template_path)

        assert result.reused.get("task-a") != unrelated
        assert result.created.get("task-a") != unrelated
        parent = result.parent_issue_number
        assert parent is not None
        # The unrelated issue must never have been touched: not linked as a
        # sub-issue, and no blocked-by relationship added to/from it.
        assert unrelated not in forge.sub_issues.get(parent, [])
        assert unrelated not in forge.blocked_by

    def test_reuses_persisted_number_when_plan_id_has_surrounding_whitespace(
        self, tmp_path: Path, template_path: Path
    ):
        """#323 review round 7 (P2): `dag_parsing._parse_subtask_id` strips
        the id before it becomes `SubTask.id` (`" task-a "` -> `"task-a"`),
        but `_load_plan`'s `issue_numbers` lookup dict was keyed by the raw,
        unstripped id — so the lookup by the stripped `subtask.id` could
        never hit, and a persisted number would be silently ignored,
        creating a duplicate issue instead of reusing it."""
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            '  - id: " task-a "\n'
            '    description: "d"\n'
            "    issue_number: 555\n"
            "---\n",
            encoding="utf-8",
        )
        forge = FakeForge()
        forge.issues[555] = {
            "title": "[FEAT] task-a: d",
            "body": "```yaml\nsubtask_id: task-a\n```\n",
            "labels": [],
        }
        forge._next_number = 556

        result = provision_issues(path, forge=forge, template_path=template_path)

        assert result.reused.get("task-a") == 555
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


class TestIsEpicIssue:
    """`--parent-issue`検証用: `is_epic_issue`がEPIC構造（タイトル接頭辞+本文
    マーカー）の有無だけを見て判定することを確認する。"""

    def _issue(self, *, title: str, body: str, state: str = "OPEN") -> IssueRecord:
        return IssueRecord(
            number=1,
            title=title,
            body=body,
            labels=(),
            created_at="",
            state=state,
        )

    def test_true_when_title_prefixed_and_marker_present(self):
        issue = self._issue(title="[EPIC] Some plan", body=f"...\n{PARENT_MARKER}")
        assert is_epic_issue(issue) is True

    def test_false_when_marker_missing(self):
        issue = self._issue(title="[EPIC] Some plan", body="no marker here")
        assert is_epic_issue(issue) is False

    def test_false_when_title_missing_prefix(self):
        issue = self._issue(title="[BUG] some bug", body=f"...\n{PARENT_MARKER}")
        assert is_epic_issue(issue) is False

    def test_false_when_neither_present(self):
        issue = self._issue(title="[BUG] some bug", body="no marker here")
        assert is_epic_issue(issue) is False

    def test_true_regardless_of_issue_state(self):
        """完了済み（クローズ済み）EPICへの後続処理を妨げないよう、状態は問わない。"""
        issue = self._issue(
            title="[EPIC] Some plan", body=f"...\n{PARENT_MARKER}", state="CLOSED"
        )
        assert is_epic_issue(issue) is True


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


class TestSymbolVerificationWarning:
    """#359: symbolsが実コードに存在しない場合、Issue本文に注記を追加する。"""

    def _plan(self, symbols: str) -> str:
        return (
            "---\n"
            'title: "Example big rock"\n'
            "parent_issue_number: null\n"
            "subtasks:\n"
            "  - id: task-a\n"
            '    description: "Implement feature XX"\n'
            "    footprint: [src/foo.py]\n"
            f"    symbols: [{symbols}]\n"
            "    depends_on: []\n"
            "    issue_number: null\n"
            "---\n"
            "\n"
            "# Decomposition Plan\n"
        )

    def test_missing_symbol_appends_warning_on_create(
        self, tmp_path: Path, template_path: Path
    ):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").write_text(
            "def actual_function():\n    pass\n", encoding="utf-8"
        )
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(self._plan("renamed_away_function"), encoding="utf-8")

        forge = FakeForge()
        provision_issues(
            plan_path,
            forge=forge,
            template_path=template_path,
            repo_root=tmp_path,
        )

        subtask_body = next(
            body for title, body, _ in forge.create_issue_calls if "task-a" in title
        )
        assert "symbols不整合の可能性" in subtask_body
        assert "renamed_away_function" in subtask_body

    def test_existing_symbol_does_not_append_warning(
        self, tmp_path: Path, template_path: Path
    ):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").write_text(
            "def actual_function():\n    pass\n", encoding="utf-8"
        )
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(self._plan("actual_function"), encoding="utf-8")

        forge = FakeForge()
        provision_issues(
            plan_path,
            forge=forge,
            template_path=template_path,
            repo_root=tmp_path,
        )

        subtask_body = next(
            body for title, body, _ in forge.create_issue_calls if "task-a" in title
        )
        assert "symbols不整合の可能性" not in subtask_body

    def test_missing_symbol_appends_warning_in_no_apply_preview(
        self, tmp_path: Path, template_path: Path
    ):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").write_text(
            "def actual_function():\n    pass\n", encoding="utf-8"
        )
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(self._plan("renamed_away_function"), encoding="utf-8")

        result = provision_issues(
            plan_path,
            apply=False,
            template_path=template_path,
            repo_root=tmp_path,
        )

        assert "symbols不整合の可能性" in result.previews[0].body

    def test_footprint_file_not_yet_existing_skips_warning(
        self, tmp_path: Path, template_path: Path
    ):
        """新規作成予定のfootprintファイルのみのsubtaskでは、検証材料が
        無いためfalse positiveを避けて注記を付けない。"""
        plan_path = tmp_path / "decomposition_plan.md"
        plan_path.write_text(self._plan("brand_new_function"), encoding="utf-8")

        forge = FakeForge()
        provision_issues(
            plan_path,
            forge=forge,
            template_path=template_path,
            repo_root=tmp_path,
        )

        subtask_body = next(
            body for title, body, _ in forge.create_issue_calls if "task-a" in title
        )
        assert "symbols不整合の可能性" not in subtask_body

    def test_repo_root_defaults_to_cwd_without_crashing(
        self, plan_path: Path, template_path: Path
    ):
        forge = FakeForge()
        result = provision_issues(plan_path, forge=forge, template_path=template_path)
        assert result.applied is True


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


def test_missing_subtask_id_yaml_placeholder_raises(tmp_path: Path, plan_path: Path):
    """#323 review round 7 (P2): without `{{subtask_id_yaml}}`, no rendered
    issue body can ever contain a `subtask_id:` line, so
    `_subtask_id_from_body` could never succeed and idempotency would break
    silently — every future run would create a duplicate issue for every
    subtask instead of failing loudly up front."""
    bad_template = tmp_path / "bad_template.md"
    bad_template.write_text(
        "# {{subtask_id}}: {{description}}\n{{overview}}\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="subtask_id_yaml"):
        provision_issues(plan_path, forge=FakeForge(), template_path=bad_template)


def test_subtask_id_yaml_placeholder_outside_the_footprint_fence_raises(
    tmp_path: Path, plan_path: Path
):
    """#323 review round 8 (P2): the raw token `{{subtask_id_yaml}}` can be
    present in the template text (satisfying a naive substring check) while
    sitting outside any Footprint YAML fence, so no rendered body can ever
    yield an extractable `subtask_id` via `_subtask_id_from_body`. The
    validation must render a probe and check extraction actually succeeds,
    not just that the token string appears somewhere."""
    bad_template = tmp_path / "bad_template.md"
    bad_template.write_text(
        "# {{subtask_id}}: {{description}}\n"
        "Not inside a fence: {{subtask_id_yaml}}\n"
        "{{overview}}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="subtask_id"):
        provision_issues(plan_path, forge=FakeForge(), template_path=bad_template)


def test_template_that_redundantly_quotes_the_placeholder_raises(
    tmp_path: Path, plan_path: Path
):
    """#323 review round 9 (P2): a custom template that wraps
    `{{subtask_id_yaml}}` in its own literal quotes (`subtask_id:
    "{{subtask_id_yaml}}"`) double-quotes any id that already needed
    quoting (e.g. one containing `:`), corrupting the fence for real
    subtask ids. A plain-word probe id round-trips fine either way and
    can't detect this, so the probe must use an id that forces
    `_yaml_scalar` to quote it."""
    bad_template = tmp_path / "bad_template.md"
    bad_template.write_text(
        "# {{subtask_id}}: {{description}}\n"
        "```yaml\n"
        'subtask_id: "{{subtask_id_yaml}}"\n'
        "footprint: {{footprint}}\n"
        "symbols: {{symbols}}\n"
        "depends_on: {{depends_on}}\n"
        "```\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="subtask_id"):
        provision_issues(plan_path, forge=FakeForge(), template_path=bad_template)


def test_missing_title_raises(tmp_path: Path, template_path: Path):
    path = tmp_path / "decomposition_plan.md"
    path.write_text(
        "---\nsubtasks:\n  - id: task-a\n    description: x\n---\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="title"):
        provision_issues(path, forge=FakeForge(), template_path=template_path)


class TestRejectsInvalidIssueNumbers:
    """#323 review (P2): a malformed issue_number must never be silently
    coerced (e.g. `int(1.5) == 1`, `int(True) == 1`) into treating an
    unrelated real issue as the subtask/parent."""

    @pytest.mark.parametrize("bad_value", ["1.5", "true", "-1", "0", '"abc"'])
    def test_rejects_bad_subtask_issue_number(
        self, tmp_path: Path, template_path: Path, bad_value
    ):
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            "subtasks:\n"
            "  - id: task-a\n"
            '    description: "d"\n'
            f"    issue_number: {bad_value}\n"
            "---\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            provision_issues(path, forge=FakeForge(), template_path=template_path)

    @pytest.mark.parametrize("bad_value", ["1.5", "true", "-1", "0"])
    def test_rejects_bad_parent_issue_number(
        self, tmp_path: Path, template_path: Path, bad_value
    ):
        path = tmp_path / "decomposition_plan.md"
        path.write_text(
            "---\n"
            'title: "x"\n'
            f"parent_issue_number: {bad_value}\n"
            "subtasks:\n"
            "  - id: task-a\n"
            '    description: "d"\n'
            "---\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            provision_issues(path, forge=FakeForge(), template_path=template_path)
