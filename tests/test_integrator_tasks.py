"""#291: `mock.patch`によるグローバルなクラスメソッド差し替えではなく、
`forge`引数への注入だけでテストが書けることを示す。"""

from __future__ import annotations

from unittest.mock import MagicMock

from orchestune.integrator_tasks import get_sorted_done_tasks
from orchestune.models import IssueRecord


def _done_issue(number: int, subtask_id: str) -> IssueRecord:
    return IssueRecord(
        number=number,
        title=f"Issue {number}",
        body=("```yaml\n" f"subtask_id: {subtask_id}\n" "footprint: []\n" "```\n"),
        labels=("status:done",),
        created_at="2026-07-13T00:00:00Z",
    )


def test_returns_empty_when_no_done_issues_using_injected_fake_forge():
    fake_forge = MagicMock()
    fake_forge.list_issues_by_label.return_value = []

    sorted_done, unparsable = get_sorted_done_tasks(None, forge=fake_forge)

    assert sorted_done == []
    assert unparsable == []
    fake_forge.list_issues_by_label.assert_called_once_with("status:done", state="all")


def test_sorts_done_tasks_using_injected_fake_forge():
    done_issue = _done_issue(1, "task-1")
    fake_forge = MagicMock()
    fake_forge.list_issues_by_label.side_effect = lambda label, state="open": (
        [done_issue] if label == "status:done" else []
    )

    sorted_done, unparsable = get_sorted_done_tasks(None, forge=fake_forge)

    assert unparsable == []
    assert [task.subtask_id for task in sorted_done] == ["task-1"]
