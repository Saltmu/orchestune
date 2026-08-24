from __future__ import annotations

from pathlib import Path

import pytest

from orchestune.forge import (
    MetadataSearchUnavailableError,
    RelationshipUnavailableError,
)
from orchestune.issue_parsing import (
    PARENT_MARKER,
)
from orchestune.models import IssueRecord
from orchestune.provisioning.cli import (
    ProvisionResult,
    _print_result,
)
from orchestune.provisioning.flow import (
    provision_issues,
)
from tests.test_provisioning_support import (
    FakeForge,
)


class TestProvisionIssuesDegradedMode:
    """#485: forgeがネイティブSub-issue/blocked_by関係を提供しない場合でも
    provisioningは中断せず、本文metadataフォールバックで完走する。"""

    def test_raises_when_neither_relationships_nor_metadata_search_are_supported(
        self, plan_path: Path, template_path: Path
    ):
        """#485 review round 7 (P1): a newly created subtask's body always
        gets the correct parent_issue_number field, so `has_parent_metadata`
        is trivially True for it. But that's worthless for discovery if
        this forge can never *search* for it either — `find_children_by_parent`
        degrades straight to native-only results, which will never include
        this subtask. Provisioning must not report "degraded" (implying
        the fallback works) in this case; it must fail loudly instead."""

        class FullyUnsupportedForge(FakeForge):
            def add_sub_issue(self, parent_issue_number, child_issue_number) -> None:
                raise RelationshipUnavailableError("sub_issue_write not exposed")

            def set_blocked_by(self, issue_number, blocking_issue_number) -> None:
                raise RelationshipUnavailableError("issue_dependency_write not exposed")

            def find_issues_by_parent_metadata(self, parent_issue_number):
                raise MetadataSearchUnavailableError("issue search not exposed")

        forge = FullyUnsupportedForge()
        with pytest.raises(RelationshipUnavailableError):
            provision_issues(plan_path, forge=forge, template_path=template_path)

    def test_completes_and_reports_degraded_subtask_ids_when_relationships_unavailable(
        self, plan_path: Path, template_path: Path
    ):
        class DegradedForge(FakeForge):
            def add_sub_issue(self, parent_issue_number, child_issue_number) -> None:
                raise RelationshipUnavailableError("sub_issue_write not exposed")

            def set_blocked_by(self, issue_number, blocking_issue_number) -> None:
                raise RelationshipUnavailableError("issue_dependency_write not exposed")

        forge = DegradedForge()
        result = provision_issues(plan_path, forge=forge, template_path=template_path)

        assert result.applied is True
        assert set(result.created) == {"task-a", "task-b"}
        assert set(result.degraded_subtask_ids) == {"task-a", "task-b"}
        # Relationships were never established natively...
        assert forge.sub_issues == {}
        assert forge.blocked_by == {}
        # ...but the parent number and dependency are still in the body.
        task_b_number = result.created["task-b"]
        assert (
            f"parent_issue_number: {result.parent_issue_number}"
            in forge.issues[task_b_number]["body"]
        )
        assert "depends_on: [task-a]" in forge.issues[task_b_number]["body"]

    def test_resumes_via_body_metadata_when_forge_only_supports_metadata_search(
        self, plan_path: Path, template_path: Path
    ):
        """A forge that never linked native sub-issues (prior run was fully
        degraded) but *does* support `find_issues_by_parent_metadata` must
        still find those already-created issues on resume, instead of
        creating duplicates."""

        class MetadataSearchForge(FakeForge):
            def add_sub_issue(self, parent_issue_number, child_issue_number) -> None:
                raise RelationshipUnavailableError("unavailable")

            def set_blocked_by(self, issue_number, blocking_issue_number) -> None:
                raise RelationshipUnavailableError("unavailable")

            def find_issues_by_parent_metadata(self, parent_issue_number):
                # No native sub_issues dict entries exist (never linked), so
                # simulate a body-metadata index scan over every issue.
                return [
                    IssueRecord(
                        number=number,
                        title=entry["title"],
                        body=entry["body"],
                        labels=tuple(entry["labels"]),
                        created_at="",
                    )
                    for number, entry in self.issues.items()
                ]

        forge = MetadataSearchForge()
        first = provision_issues(plan_path, forge=forge, template_path=template_path)
        assert set(first.created) == {"task-a", "task-b"}

        second = provision_issues(plan_path, forge=forge, template_path=template_path)
        assert second.created == {}
        assert set(second.reused) == {"task-a", "task-b"}
        assert second.reused["task-a"] == first.created["task-a"]
        assert second.reused["task-b"] == first.created["task-b"]

    def test_raises_when_reused_issue_has_neither_native_link_nor_metadata_fallback(
        self, tmp_path: Path, template_path: Path
    ):
        """#485 review round 4 (P2): a legacy issue (body predates
        parent_issue_number) reused on a forge that can't write native
        `add_sub_issue` *or* `update_issue_body` has no way for the
        parent-scoped Dispatcher to ever find it. Reporting this as
        "degraded" (as if the metadata fallback covers it) would be
        actively misleading, so provisioning must fail loudly instead."""
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(
            "---\ntitle: 'T'\nsubtasks:\n"
            "  - id: task-a\n    description: 'd'\n    issue_number: 555\n---\n",
            encoding="utf-8",
        )

        class FullyDegradedForge(FakeForge):
            def add_sub_issue(self, parent_issue_number, child_issue_number) -> None:
                raise RelationshipUnavailableError("sub_issue_write not exposed")

            def set_blocked_by(self, issue_number, blocking_issue_number) -> None:
                raise RelationshipUnavailableError("issue_dependency_write not exposed")

            def update_issue_body(self, issue_number, body) -> None:
                raise RelationshipUnavailableError("body edit not exposed")

        forge = FullyDegradedForge()
        forge.issues[555] = {
            "title": "[FEAT] task-a: d",
            # No `parent_issue_number` field: this predates the field.
            "body": "```yaml\nsubtask_id: task-a\n```\n",
            "labels": ["status:queued"],
        }
        forge._next_number = 556

        with pytest.raises(RelationshipUnavailableError):
            provision_issues(plan_path, forge=forge, template_path=template_path)

    def test_does_not_raise_when_reused_issue_already_has_a_native_parent_link(
        self, tmp_path: Path, template_path: Path
    ):
        """#485 review round 5 (P2): a legacy issue already natively linked
        to its parent from a prior run (discovered here via `list_sub_issues`)
        remains discoverable even if the *current* forge can't re-write
        `add_sub_issue`/`update_issue_body` — the existing native relationship
        on GitHub doesn't disappear just because this run can't re-assert it.
        Provisioning must not raise for it."""
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(
            "---\ntitle: 'T'\nsubtasks:\n"
            "  - id: task-a\n    description: 'd'\n    issue_number: null\n---\n",
            encoding="utf-8",
        )

        class ReadOnlyForge(FakeForge):
            def add_sub_issue(self, parent_issue_number, child_issue_number) -> None:
                raise RelationshipUnavailableError("sub_issue_write not exposed")

            def set_blocked_by(self, issue_number, blocking_issue_number) -> None:
                raise RelationshipUnavailableError("issue_dependency_write not exposed")

            def update_issue_body(self, issue_number, body) -> None:
                raise RelationshipUnavailableError("body edit not exposed")

        forge = ReadOnlyForge()
        parent_number = forge.create_issue(
            "[EPIC] T", f"...\n{PARENT_MARKER}", labels=()
        )
        # Simulate a legacy issue already natively linked from a prior run
        # (predates `parent_issue_number` in the body).
        child_number = forge.create_issue(
            "[FEAT] task-a: d",
            "```yaml\nsubtask_id: task-a\n```\n",
            labels=("status:queued",),
        )
        forge.sub_issues[parent_number] = [child_number]

        result = provision_issues(plan_path, forge=forge, template_path=template_path)

        assert result.applied is True
        assert result.reused == {"task-a": child_number}
        assert result.created == {}
        # No body write was ever attempted: the native link already made
        # it discoverable, so there was nothing to backfill.
        assert "parent_issue_number" not in forge.issues[child_number]["body"]

    def test_raises_when_reused_issue_natively_belongs_to_a_different_parent(
        self, tmp_path: Path, template_path: Path
    ):
        """#485 review round 6 (P2): a *present* native parent always wins
        over body metadata (that's the whole point of treating native as
        authoritative). So an issue natively attached to a *different*
        parent must not be reported "discoverable" just because a body
        backfill write would succeed — that write would be silently
        ignored by `effective_parent_number`. Only a successful native
        re-link fixes this; if that also fails, provisioning must raise
        rather than report misleading degraded success."""
        plan_path = tmp_path / "plan.md"
        plan_path.write_text(
            "---\ntitle: 'T'\nsubtasks:\n"
            "  - id: task-a\n    description: 'd'\n    issue_number: 555\n---\n",
            encoding="utf-8",
        )

        class NoRelinkForge(FakeForge):
            def add_sub_issue(self, parent_issue_number, child_issue_number) -> None:
                raise RelationshipUnavailableError("sub_issue_write not exposed")

            def set_blocked_by(self, issue_number, blocking_issue_number) -> None:
                raise RelationshipUnavailableError("issue_dependency_write not exposed")

            # `update_issue_body` is intentionally NOT overridden here: it
            # would succeed if called, which is exactly what must not
            # matter — this issue is natively attached elsewhere, so a
            # body write can never make it discoverable under `100`.

        forge = NoRelinkForge()
        # `parent_issue_number` isn't persisted in the plan, so
        # `_resolve_parent_issue` creates a fresh EPIC and gets whatever
        # number is next — pin it to #1000 so it's unambiguously distinct
        # from the stale native parent (#999) seeded below.
        forge._next_number = 1000
        forge.issues[555] = {
            "title": "[FEAT] task-a: d",
            "body": "```yaml\nsubtask_id: task-a\n```\n",
            "labels": ["status:queued"],
        }
        # Natively attached to a *different* parent (#999), not the #1000
        # this plan will resolve its own parent to.
        forge.sub_issues[999] = [555]

        with pytest.raises(RelationshipUnavailableError):
            provision_issues(plan_path, forge=forge, template_path=template_path)

    def test_print_result_reports_degraded_operating_mode(self, capsys):
        result = ProvisionResult(
            parent_issue_number=100,
            applied=True,
            created={"task-a": 101},
            reused={},
            degraded_subtask_ids=("task-a",),
        )
        _print_result(result)
        captured = capsys.readouterr()
        assert "Operating mode: degraded/parent-metadata" in captured.out
        assert "task-a" in captured.out

    def test_print_result_reports_full_operating_mode_when_nothing_degraded(
        self, capsys
    ):
        result = ProvisionResult(
            parent_issue_number=100,
            applied=True,
            created={"task-a": 101},
            reused={},
        )
        _print_result(result)
        captured = capsys.readouterr()
        assert "Operating mode: full" in captured.out
