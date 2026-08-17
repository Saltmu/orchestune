"""#485: ネイティブSub-issue関係が使えない環境向けフォールバックの単体テスト。"""

from __future__ import annotations

import pytest

from orchestune.forge import MetadataSearchUnavailableError
from orchestune.issue_parsing import (
    backfill_parent_issue_number,
    find_children_by_parent,
    parent_issue_number_from_body,
)
from orchestune.models import IssueRecord


class TestParentIssueNumberFromBody:
    def test_parses_value_from_footprint_block(self):
        body = "```yaml\nsubtask_id: task-a\nparent_issue_number: 100\n```\n"
        assert parent_issue_number_from_body(body) == 100

    def test_returns_none_when_null(self):
        body = "```yaml\nsubtask_id: task-a\nparent_issue_number: null\n```\n"
        assert parent_issue_number_from_body(body) is None

    def test_returns_none_when_field_absent(self):
        body = "```yaml\nsubtask_id: task-a\n```\n"
        assert parent_issue_number_from_body(body) is None

    def test_returns_none_when_no_yaml_block(self):
        assert parent_issue_number_from_body("no yaml here") is None

    def test_returns_none_on_malformed_yaml(self):
        body = "```yaml\n: not valid: yaml: [\n```\n"
        assert parent_issue_number_from_body(body) is None

    def test_parses_quoted_integer_string(self):
        body = '```yaml\nsubtask_id: task-a\nparent_issue_number: "100"\n```\n'
        assert parent_issue_number_from_body(body) == 100

    def test_rejects_fractional_value_instead_of_truncating(self):
        """#485 review round 8 (P2): `int(100.9)` truncates to 100 rather
        than rejecting a malformed value — this must reject it instead,
        or a metadata-search substring match on '100' could wrongly treat
        the issue as a child of parent #100."""
        body = "```yaml\nsubtask_id: task-a\nparent_issue_number: 100.9\n```\n"
        assert parent_issue_number_from_body(body) is None

    def test_rejects_boolean_value(self):
        """`bool` is a subclass of `int` in Python; `int(True) == 1` would
        otherwise silently accept a YAML boolean as issue #1."""
        body = "```yaml\nsubtask_id: task-a\nparent_issue_number: true\n```\n"
        assert parent_issue_number_from_body(body) is None

    def test_rejects_non_numeric_string(self):
        body = "```yaml\nsubtask_id: task-a\nparent_issue_number: abc\n```\n"
        assert parent_issue_number_from_body(body) is None


class TestBackfillParentIssueNumber:
    def test_adds_missing_field_preserving_rest_of_body(self):
        body = (
            "# [FEAT] task-a: d\n\nSome human-written notes.\n\n"
            "```yaml\nsubtask_id: task-a\nfootprint: [src/foo.py]\n```\n"
        )
        result = backfill_parent_issue_number(body, 100)
        assert result is not None
        assert "Some human-written notes." in result
        assert parent_issue_number_from_body(result) == 100
        assert "subtask_id: task-a" in result

    def test_corrects_a_stale_value(self):
        body = "```yaml\nsubtask_id: task-a\nparent_issue_number: 999\n```\n"
        result = backfill_parent_issue_number(body, 100)
        assert result is not None
        assert parent_issue_number_from_body(result) == 100

    def test_returns_none_when_already_correct(self):
        body = "```yaml\nsubtask_id: task-a\nparent_issue_number: 100\n```\n"
        assert backfill_parent_issue_number(body, 100) is None

    def test_returns_none_when_no_footprint_block(self):
        assert backfill_parent_issue_number("no yaml here", 100) is None


class _NativeOnlyForge:
    """`find_issues_by_parent_metadata`を実装しない旧来のForge。"""

    def __init__(self, native: list[IssueRecord]):
        self._native = native

    def list_sub_issues(self, parent_issue_number):
        return self._native


class _MetadataAwareForge(_NativeOnlyForge):
    def __init__(self, native, metadata_candidates):
        super().__init__(native)
        self._candidates = metadata_candidates
        self.metadata_calls = 0

    def find_issues_by_parent_metadata(self, parent_issue_number):
        self.metadata_calls += 1
        return self._candidates


class _UnsupportedMetadataForge(_NativeOnlyForge):
    def find_issues_by_parent_metadata(self, parent_issue_number):
        raise MetadataSearchUnavailableError("MCP does not expose issue search")


class _FlakyMetadataForge(_NativeOnlyForge):
    def find_issues_by_parent_metadata(self, parent_issue_number):
        raise RuntimeError("gh: rate limit exceeded")


def _issue(number, parent_issue_number, subtask_id="task-a"):
    return IssueRecord(
        number=number,
        title=f"[FEAT] {subtask_id}",
        body=(
            f"```yaml\nsubtask_id: {subtask_id}\n"
            f"parent_issue_number: {parent_issue_number}\n```\n"
        ),
        labels=(),
        created_at="2026-01-01T00:00:00Z",
    )


class TestFindChildrenByParent:
    def test_returns_native_only_when_forge_lacks_metadata_search(self):
        native = [_issue(1, 100)]
        forge = _NativeOnlyForge(native)

        result = find_children_by_parent(forge, 100)

        assert result.issues == native
        assert result.metadata_search_supported is False

    def test_merges_metadata_fallback_candidates_not_in_native_list(self):
        native = [_issue(1, 100)]
        # #485: issue 2 was created without a native sub-issue link (e.g. an
        # MCP without sub_issue_write) but carries the metadata field.
        candidates = [_issue(1, 100), _issue(2, 100, subtask_id="task-b")]
        forge = _MetadataAwareForge(native, candidates)

        result = find_children_by_parent(forge, 100)

        assert sorted(r.number for r in result.issues) == [1, 2]
        assert result.metadata_search_supported is True
        assert forge.metadata_calls == 1

    def test_rejects_candidates_whose_body_metadata_does_not_match(self):
        """gh --search is a substring match ('100' can hit '1000'); the
        result must be re-verified against the exact parsed value."""
        native = []
        candidates = [_issue(2, 1000, subtask_id="task-b")]
        forge = _MetadataAwareForge(native, candidates)

        assert find_children_by_parent(forge, 100).issues == []

    def test_rejects_candidates_whose_native_parent_disagrees_with_stale_body(self):
        """#485 review (P2): an issue natively reparented elsewhere, whose
        body still carries the old parent_issue_number (never rewritten),
        must not match the old parent via the metadata fallback — or it
        would indefinitely block that old parent's completion under the
        wrong scope."""
        native: list[IssueRecord] = []
        reparented = IssueRecord(
            number=2,
            title="[FEAT] task-b",
            body="```yaml\nsubtask_id: task-b\nparent_issue_number: 100\n```\n",
            labels=(),
            created_at="2026-01-01T00:00:00Z",
            parent={"number": 200},
        )
        forge = _MetadataAwareForge(native, [reparented])

        assert find_children_by_parent(forge, 100).issues == []

    def test_falls_back_to_native_only_when_metadata_search_is_unsupported(self):
        native = [_issue(1, 100)]
        forge = _UnsupportedMetadataForge(native)

        result = find_children_by_parent(forge, 100)
        assert result.issues == native
        assert result.metadata_search_supported is False

    def test_propagates_operational_metadata_search_failures(self):
        """#485 review (P1): a transient/operational failure (auth expired,
        rate limit, network) must not be silently downgraded to
        native-only — that can make a metadata-only child temporarily
        disappear from a parent-scoped cycle, and provisioning's own
        dedup could then recreate it as a duplicate."""
        native = [_issue(1, 100)]
        forge = _FlakyMetadataForge(native)

        with pytest.raises(RuntimeError):
            find_children_by_parent(forge, 100)
