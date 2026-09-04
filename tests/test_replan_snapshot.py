from __future__ import annotations

from dataclasses import dataclass

import pytest

from orchestune.models import IssueRecord, PrRecord
from orchestune.replan.snapshot import collect_replan_snapshot


def _issue(number: int, body: str = "", *, state: str = "OPEN") -> IssueRecord:
    return IssueRecord(number, f"Issue {number}", body, (), "2026-01-01", state)


@dataclass
class FakeForge:
    parent: IssueRecord
    children: list[IssueRecord]
    prs: list[PrRecord]

    def get_issue(self, number: int) -> IssueRecord | None:
        return self.parent if number == self.parent.number else None

    def list_sub_issues(self, number: int) -> list[IssueRecord]:
        assert number == self.parent.number
        return self.children

    def list_prs(self, state: str = "open", **_: object) -> list[PrRecord]:
        assert state == "merged"
        return self.prs


def test_collects_only_issues_enumerated_by_parent_plan_and_is_read_only() -> None:
    parent = _issue(
        693,
        "<!-- orchestune:decomposition-plan -->\n```yaml\nsubtasks:\n"
        "- id: old-a\n  issue_number: 10\n```\n",
    )
    forge = FakeForge(
        parent,
        [_issue(10), _issue(11)],
        [PrRecord(99, "x", (), closes_issue_numbers=(10,))],
    )

    snapshot = collect_replan_snapshot(forge, 693)

    assert snapshot.retirement_candidates[0].issue_number == 10
    assert [issue.number for issue in snapshot.old_issues] == [10]
    assert [issue.number for issue in snapshot.child_issues] == [10, 11]
    assert snapshot.merged_closing_issue_numbers == (10,)


def test_collects_merged_subissue_branch_issue_without_closing_reference() -> None:
    parent = _issue(
        693,
        "<!-- orchestune:decomposition-plan -->\n```yaml\nsubtasks:\n"
        "- id: old-a\n  issue_number: 10\n```\n",
    )
    forge = FakeForge(
        parent,
        [_issue(10)],
        [
            PrRecord(
                99,
                "claude/issue-10-old-a",
                (),
                closes_issue_numbers=(),
            )
        ],
    )

    snapshot = collect_replan_snapshot(forge, 693)

    assert snapshot.merged_closing_issue_numbers == (10,)


def test_snapshot_fails_closed_when_parent_plan_number_and_body_id_disagree() -> None:
    parent = _issue(
        693,
        "<!-- orchestune:decomposition-plan -->\n```yaml\nsubtasks:\n"
        "- id: old-a\n  issue_number: 10\n```\n",
    )
    forge = FakeForge(parent, [_issue(10, "```yaml\nsubtask_id: other\n```")], [])

    snapshot = collect_replan_snapshot(forge, 693)

    assert snapshot.conflicts == (
        "old Issue #10 declares subtask_id 'other', expected 'old-a'",
    )


def test_snapshot_rejects_non_string_subtask_id_in_parent_plan() -> None:
    parent = _issue(
        693,
        "<!-- orchestune:decomposition-plan -->\n```yaml\nsubtasks:\n"
        "- id: 12\n  issue_number: 10\n```\n",
    )

    with pytest.raises(ValueError, match="invalid old subtask"):
        collect_replan_snapshot(FakeForge(parent, [_issue(10)], []), 693)
