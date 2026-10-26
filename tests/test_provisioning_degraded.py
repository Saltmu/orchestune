from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

from orchestune.forge import (
    MetadataSearchUnavailableError,
    RelationshipUnavailableError,
)
from orchestune.infra.cli_output import make_output
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

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class _TtyStream(io.StringIO):
    """A stream that reports itself as a UTF-8 terminal (decorated output)."""

    def isatty(self) -> bool:
        return True

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"


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

    def test_print_result_warns_about_degraded_mode_on_tty(self):
        result = ProvisionResult(
            parent_issue_number=100,
            applied=True,
            created={"task-a": 101},
            reused={},
            degraded_subtask_ids=("task-a",),
        )
        stream = _TtyStream()
        _print_result(result, make_output(stream, env={}, width=100))
        text = _ANSI.sub("", stream.getvalue())
        assert "Operating mode: degraded/parent-metadata for 1 subtask(s)" in text
        assert "task-a" in text
        assert "\x1b" in stream.getvalue()


_HOSTILE = "red\x1b[31m!\x1b[0m\x1b]0;pwned\x07\u202eevil\u2066\x01\x80end"
_HOSTILE_CLEAN = "red!evilend"
_FORBIDDEN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def _preview(**overrides: object):
    from orchestune.provisioning.flow import IssuePreview

    fields: dict[str, object] = {
        "subtask_id": "task-a",
        "title": "Do the thing",
        "body": "line one\n\nline three\n",
        "labels": ("status:queued", "priority:medium"),
        "already_has_issue": False,
    }
    fields.update(overrides)
    return IssuePreview(**fields)  # type: ignore[arg-type]


def _plain_text(result: ProvisionResult, capsys: pytest.CaptureFixture[str]) -> str:
    _print_result(result)
    captured = capsys.readouterr()
    assert captured.err == ""
    return captured.out


class TestPrintResultPlain:
    """#1441: plain output goes through the shared layer; wording is unchanged."""

    def test_dry_run_text_is_unchanged(self, capsys):
        result = ProvisionResult(
            parent_issue_number=100,
            applied=False,
            created={},
            reused={},
            previews=(
                _preview(),
                _preview(subtask_id="task-b", already_has_issue=True),
            ),
        )
        assert _plain_text(result, capsys) == (
            "Dry run (--no-apply): no Issues were created.\n"
            "Target parent issue: #100\n"
            "\n"
            "=== task-a (would create) ===\n"
            "Title: Do the thing\n"
            "Labels: status:queued, priority:medium\n"
            "line one\n\nline three\n\n"
            "\n"
            "=== task-b (reuse expected) ===\n"
            "Title: Do the thing\n"
            "Labels: status:queued, priority:medium\n"
            "line one\n\nline three\n\n"
        )

    def test_dry_run_without_parent_omits_target_line(self, capsys):
        result = ProvisionResult(
            parent_issue_number=None, applied=False, created={}, reused={}
        )
        assert _plain_text(result, capsys) == (
            "Dry run (--no-apply): no Issues were created.\n"
        )

    def test_applied_text_is_unchanged(self, capsys):
        result = ProvisionResult(
            parent_issue_number=100,
            applied=True,
            created={"task-a": 101},
            reused={"task-b": 102},
        )
        assert _plain_text(result, capsys) == (
            "Parent issue: #100\n"
            "Created: 1\n"
            "  + task-a -> #101\n"
            "Reused: 1\n"
            "  = task-b -> #102\n"
            "\n"
            "Operating mode: full (native sub-issue/blocked_by relationships linked)\n"
        )

    def test_degraded_text_is_unchanged(self, capsys):
        result = ProvisionResult(
            parent_issue_number=100,
            applied=True,
            created={"task-a": 101},
            reused={},
            degraded_subtask_ids=("task-a",),
        )
        assert _plain_text(result, capsys) == (
            "Parent issue: #100\n"
            "Created: 1\n"
            "  + task-a -> #101\n"
            "Reused: 0\n"
            "\n"
            "Operating mode: degraded/parent-metadata for 1 subtask(s)\n"
            "  ! task-a\n"
        )

    def test_parent_only_error_text_is_unchanged(self, capsys):
        result = ProvisionResult(
            parent_issue_number=100,
            applied=True,
            created={},
            reused={},
            parent_only_subtask_ids=("ghost-a", "ghost-b"),
        )
        assert _plain_text(result, capsys) == (
            "Parent issue: #100\n"
            "Created: 0\n"
            "Reused: 0\n"
            "\n"
            "Operating mode: full (native sub-issue/blocked_by relationships linked)\n"
            "\n"
            "Error: the plan of #100 has registered subtasks missing from the "
            "local plan: ghost-a, ghost-b\n"
            "No Issues were created. Restore the plan with "
            "`orchestune provision --restore-plan 100` and rerun provision.\n"
        )

    def test_plan_sync_failure_text_is_unchanged(self, capsys):
        result = ProvisionResult(
            parent_issue_number=100,
            applied=True,
            created={},
            reused={},
            plan_synced=False,
        )
        assert _plain_text(result, capsys) == (
            "Parent issue: #100\n"
            "Created: 0\n"
            "Reused: 0\n"
            "\n"
            "Operating mode: full (native sub-issue/blocked_by relationships linked)\n"
            "\n"
            "Warning: could not sync decomposition plan into #100's body.\n"
            "The local plan file was updated, but the parent Issue body does not "
            "reflect the latest state.\n"
        )

    def test_dry_run_strips_controls_and_keeps_body_newlines(self, capsys):
        result = ProvisionResult(
            parent_issue_number=100,
            applied=False,
            created={},
            reused={},
            previews=(
                _preview(
                    subtask_id=f"id-{_HOSTILE}",
                    title=f"T-{_HOSTILE}\nsecond",
                    body=f"head {_HOSTILE}\n\tindented\nlast",
                    labels=(f"l-{_HOSTILE}", "ok"),
                ),
            ),
        )
        out = _plain_text(result, capsys)
        assert not _FORBIDDEN.search(out.replace("\n", "").replace("\t", ""))
        assert f"=== id-{_HOSTILE_CLEAN} (would create) ===\n" in out
        assert f"Title: T-{_HOSTILE_CLEAN} second\n" in out
        assert f"Labels: l-{_HOSTILE_CLEAN}, ok\n" in out
        assert f"head {_HOSTILE_CLEAN}\n\tindented\nlast\n" in out

    def test_applied_strips_controls_from_subtask_ids(self, capsys):
        result = ProvisionResult(
            parent_issue_number=100,
            applied=True,
            created={f"c-{_HOSTILE}": 101},
            reused={f"r-{_HOSTILE}": 102},
            degraded_subtask_ids=(f"d-{_HOSTILE}",),
            parent_only_subtask_ids=(f"p-{_HOSTILE}",),
        )
        out = _plain_text(result, capsys)
        assert not _FORBIDDEN.search(out.replace("\n", ""))
        assert f"  + c-{_HOSTILE_CLEAN} -> #101\n" in out
        assert f"  = r-{_HOSTILE_CLEAN} -> #102\n" in out
        assert f"  ! d-{_HOSTILE_CLEAN}\n" in out
        assert f"local plan: p-{_HOSTILE_CLEAN}\n" in out

    def test_plain_env_on_a_tty_sanitizes_without_decoration(self):
        stream = _TtyStream()
        result = ProvisionResult(
            parent_issue_number=100,
            applied=False,
            created={},
            reused={},
            previews=(_preview(title=_HOSTILE, body=f"b {_HOSTILE}\nz"),),
        )
        output = make_output(stream, env={"ORCHESTUNE_CLI_PLAIN": "1"})
        _print_result(result, output)
        text = stream.getvalue()
        assert "\x1b" not in text
        assert not _FORBIDDEN.search(text.replace("\n", ""))
        assert f"Title: {_HOSTILE_CLEAN}\n" in text
        assert f"b {_HOSTILE_CLEAN}\nz\n" in text
