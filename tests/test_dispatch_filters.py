"""#485: `_filter_by_parent`のネイティブ/本文metadataフォールバック挙動。"""

from __future__ import annotations

from orchestune.dispatch.filters import _filter_by_parent
from orchestune.models import IssueRecord


def _issue(number, parent=None, body=""):
    return IssueRecord(
        number=number,
        title="t",
        body=body,
        labels=(),
        created_at="2026-01-01T00:00:00Z",
        parent=parent,
    )


class TestFilterByParent:
    def test_returns_all_issues_when_parent_number_is_none(self):
        issues = [_issue(1), _issue(2)]
        assert _filter_by_parent(issues, None) == issues

    def test_matches_on_native_parent_relationship(self):
        issues = [_issue(1, parent={"number": 100}), _issue(2, parent={"number": 200})]
        assert [i.number for i in _filter_by_parent(issues, 100)] == [1]

    def test_falls_back_to_body_metadata_when_native_parent_absent(self):
        body = "```yaml\nparent_issue_number: 100\n```\n"
        issues = [
            _issue(1, parent=None, body=body),
            _issue(2, parent=None, body="```yaml\nparent_issue_number: 200\n```\n"),
        ]
        assert [i.number for i in _filter_by_parent(issues, 100)] == [1]

    def test_native_parent_takes_precedence_over_body_metadata(self):
        body = "```yaml\nparent_issue_number: 999\n```\n"
        issues = [_issue(1, parent={"number": 100}, body=body)]
        assert [i.number for i in _filter_by_parent(issues, 100)] == [1]
        assert _filter_by_parent(issues, 999) == []

    def test_excludes_issues_with_no_parent_information_at_all(self):
        issues = [_issue(1, parent=None, body="no metadata")]
        assert _filter_by_parent(issues, 100) == []
