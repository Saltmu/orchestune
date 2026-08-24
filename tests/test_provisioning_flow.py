from __future__ import annotations

from pathlib import Path

import pytest

from orchestune.dag.models import SubTask
from orchestune.forge import (
    RelationshipUnavailableError,
)
from orchestune.issue_parsing import (
    PARENT_MARKER,
)
from orchestune.provisioning import (
    _link_subtask_relationships,
    _parent_body,
    _provision_subtask,
    provision_issues,
)
from tests.test_provisioning_support import (
    FakeForge,
)


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
        prose_before_marker = parent_call[1].split(PARENT_MARKER)[0]
        assert "subtasks:" not in prose_before_marker

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


class TestProvisionSubtask:
    def test_provisions_new_subtask_and_writes_to_plan(
        self, tmp_path: Path, template_path: Path
    ):
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(
            "---\ntitle: 'T'\nsubtasks:\n  - id: task-a\n    description: 'd'\n    issue_number: null\n---\n",
            encoding="utf-8",
        )
        template = template_path.read_text(encoding="utf-8")
        forge = FakeForge()
        subtask = SubTask(
            id="task-a",
            description="desc a",
            footprint=(),
            symbols=(),
            depends_on=(),
            risk=False,
            risk_reasons=(),
        )
        number, is_reused, is_done, has_parent_metadata = _provision_subtask(
            forge=forge,
            subtask=subtask,
            template=template,
            repo_root=tmp_path,
            plan_path=plan_path,
            existing_by_subtask_id={},
            dependencies_done={},
            parent_issue_number=1,
        )
        assert is_reused is False
        assert is_done is False
        assert has_parent_metadata is True
        assert number in forge.issues
        assert f"issue_number: {number}" in plan_path.read_text(encoding="utf-8")

    def test_reuses_existing_subtask_with_done_status(
        self, tmp_path: Path, template_path: Path
    ):
        plan_path = tmp_path / "plan.md"
        plan_path.write_text("---\ntitle: 'T'\n---\n", encoding="utf-8")
        template = template_path.read_text(encoding="utf-8")
        forge = FakeForge()
        existing_number = forge.create_issue(
            "[FEAT] task-a: d",
            "```yaml\nsubtask_id: task-a\n```\n",
            labels=("status:done",),
        )
        subtask = SubTask(
            id="task-a",
            description="desc a",
            footprint=(),
            symbols=(),
            depends_on=(),
            risk=False,
            risk_reasons=(),
            issue_number=existing_number,
        )
        number, is_reused, is_done, has_parent_metadata = _provision_subtask(
            forge=forge,
            subtask=subtask,
            template=template,
            repo_root=tmp_path,
            plan_path=plan_path,
            existing_by_subtask_id={},
            dependencies_done={},
            parent_issue_number=1,
        )
        assert number == existing_number
        assert is_reused is True
        assert is_done is True
        assert has_parent_metadata is True
        # #485 review (P2): a reused issue created before parent_issue_number
        # existed (or by an older template) gets it backfilled on reuse, so
        # the metadata fallback can still find it if native linking fails.
        assert "parent_issue_number: 1" in forge.issues[existing_number]["body"]

    def test_does_not_rewrite_body_when_parent_metadata_already_correct(
        self, tmp_path: Path, template_path: Path
    ):
        plan_path = tmp_path / "plan.md"
        plan_path.write_text("---\ntitle: 'T'\n---\n", encoding="utf-8")
        template = template_path.read_text(encoding="utf-8")

        class RecordingForge(FakeForge):
            def __init__(self) -> None:
                super().__init__()
                self.update_issue_body_calls: list[int] = []

            def update_issue_body(self, issue_number: int | str, body: str) -> None:
                self.update_issue_body_calls.append(int(issue_number))

        forge = RecordingForge()
        existing_number = forge.create_issue(
            "[FEAT] task-a: d",
            "```yaml\nsubtask_id: task-a\nparent_issue_number: 1\n```\n",
            labels=("status:queued",),
        )
        subtask = SubTask(
            id="task-a",
            description="desc a",
            footprint=(),
            symbols=(),
            depends_on=(),
            risk=False,
            risk_reasons=(),
            issue_number=existing_number,
        )
        _provision_subtask(
            forge=forge,
            subtask=subtask,
            template=template,
            repo_root=tmp_path,
            plan_path=plan_path,
            existing_by_subtask_id={},
            dependencies_done={},
            parent_issue_number=1,
        )
        assert forge.update_issue_body_calls == []

    def test_backfill_failure_from_unsupported_forge_does_not_abort_reuse(
        self, tmp_path: Path, template_path: Path
    ):
        plan_path = tmp_path / "plan.md"
        plan_path.write_text("---\ntitle: 'T'\n---\n", encoding="utf-8")
        template = template_path.read_text(encoding="utf-8")

        class NoBodyUpdateForge(FakeForge):
            def update_issue_body(self, issue_number, body) -> None:
                raise RelationshipUnavailableError("body edit not exposed")

        forge = NoBodyUpdateForge()
        existing_number = forge.create_issue(
            "[FEAT] task-a: d",
            "```yaml\nsubtask_id: task-a\n```\n",
            labels=("status:queued",),
        )
        subtask = SubTask(
            id="task-a",
            description="desc a",
            footprint=(),
            symbols=(),
            depends_on=(),
            risk=False,
            risk_reasons=(),
            issue_number=existing_number,
        )
        number, is_reused, _, has_parent_metadata = _provision_subtask(
            forge=forge,
            subtask=subtask,
            template=template,
            repo_root=tmp_path,
            plan_path=plan_path,
            existing_by_subtask_id={},
            dependencies_done={},
            parent_issue_number=1,
        )
        assert number == existing_number
        assert is_reused is True
        # #485 review round 4 (P2): the caller (provision_issues) must be
        # told the backfill failed, so it can check whether native linking
        # covers this subtask instead — reusing it "successfully" here
        # doesn't mean it's actually discoverable.
        assert has_parent_metadata is False

    def test_rewrites_an_invalid_bodyparent_value_that_coerces_equal_via_python(
        self, tmp_path: Path, template_path: Path
    ):
        """#485 review round 9 (P2): `parent_issue_number: true` in the
        body must actually be rewritten for parent #1, not skipped as
        "already correct" just because `True == 1` in Python — the strict
        parser (`parent_issue_number_from_body`) rejects booleans, so
        skipping the write here would leave the body permanently
        undiscoverable while `has_parent_metadata` wrongly reports True."""
        plan_path = tmp_path / "plan.md"
        plan_path.write_text("---\ntitle: 'T'\n---\n", encoding="utf-8")
        template = template_path.read_text(encoding="utf-8")
        forge = FakeForge()
        existing_number = forge.create_issue(
            "[FEAT] task-a: d",
            "```yaml\nsubtask_id: task-a\nparent_issue_number: true\n```\n",
            labels=("status:queued",),
        )
        subtask = SubTask(
            id="task-a",
            description="desc a",
            footprint=(),
            symbols=(),
            depends_on=(),
            risk=False,
            risk_reasons=(),
            issue_number=existing_number,
        )
        number, is_reused, _, has_parent_metadata = _provision_subtask(
            forge=forge,
            subtask=subtask,
            template=template,
            repo_root=tmp_path,
            plan_path=plan_path,
            existing_by_subtask_id={},
            dependencies_done={},
            parent_issue_number=1,
        )
        assert has_parent_metadata is True
        assert "parent_issue_number: 1\n" in forge.issues[existing_number]["body"]


class TestLinkSubtaskRelationships:
    def test_links_sub_issue_and_blockers(self):
        forge = FakeForge()
        result = _link_subtask_relationships(
            forge=forge,
            parent_issue_number=100,
            issue_number=102,
            depends_on=("dep-1",),
            resolved_numbers={"dep-1": 101},
        )
        assert forge.sub_issues[100] == [102]
        assert forge.blocked_by[102] == [101]
        assert result.degraded is False

    def test_degrades_gracefully_when_forge_signals_relationship_unavailable(self):
        """#485: a forge (e.g. an MCP without sub_issue_write/
        issue_dependency_write) that raises `RelationshipUnavailableError`
        must not abort provisioning — it degrades to the body-metadata
        fallback and reports which relationships were not linked natively."""

        class DegradedForge(FakeForge):
            def add_sub_issue(self, parent_issue_number, child_issue_number) -> None:
                raise RelationshipUnavailableError("sub_issue_write not exposed")

            def set_blocked_by(self, issue_number, blocking_issue_number) -> None:
                raise RelationshipUnavailableError("issue_dependency_write not exposed")

        forge = DegradedForge()
        result = _link_subtask_relationships(
            forge=forge,
            parent_issue_number=100,
            issue_number=102,
            depends_on=("dep-1",),
            resolved_numbers={"dep-1": 101},
        )
        assert result.parent_linked is False
        assert result.unresolved_dependencies == ("dep-1",)
        assert result.degraded is True
        assert forge.sub_issues == {}
        assert forge.blocked_by == {}

    def test_other_exceptions_still_propagate_for_retry(self):
        """#323: a transient failure (not a structural capability gap) must
        still abort the run so a rerun can pick up where it left off —
        swallowing it unconditionally would silently corrupt the DAG's
        dependency ordering instead of retrying."""

        class FlakyForge(FakeForge):
            def add_sub_issue(self, parent_issue_number, child_issue_number) -> None:
                raise RuntimeError("simulated transient failure")

        forge = FlakyForge()
        with pytest.raises(RuntimeError):
            _link_subtask_relationships(
                forge=forge,
                parent_issue_number=100,
                issue_number=102,
                depends_on=(),
                resolved_numbers={},
            )

    def test_partial_dependency_failure_reports_only_the_failed_ones(self):
        class PartiallyDegradedForge(FakeForge):
            def set_blocked_by(self, issue_number, blocking_issue_number) -> None:
                if blocking_issue_number == 101:
                    raise RelationshipUnavailableError("unavailable")
                super().set_blocked_by(issue_number, blocking_issue_number)

        forge = PartiallyDegradedForge()
        result = _link_subtask_relationships(
            forge=forge,
            parent_issue_number=100,
            issue_number=103,
            depends_on=("dep-1", "dep-2"),
            resolved_numbers={"dep-1": 101, "dep-2": 102},
        )
        assert result.parent_linked is True
        assert result.unresolved_dependencies == ("dep-1",)
        assert forge.blocked_by[103] == [102]
