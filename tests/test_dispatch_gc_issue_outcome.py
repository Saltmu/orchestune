"""Issue-canonical Outcome lookup regressions for GC (#1002)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.gc import _rule_completed, _rule_not_needed
from orchestune.dispatch.gc.completion import (
    _fetch_outcome_for_active,
    _local_pr_completion_status,
)
from orchestune.models import IssueRecord, PrRecord
from orchestune.outcome_record import OutcomeLookupState, OutcomeRecord
from tests.dispatch_gc_test_support import _active, _rule_ctx, _task
from tests.dispatch_test_support import flat_active_worktree


def _config(tmp_path, forge):
    return DispatcherConfig(
        parent_issue_number=100,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "run_state.json",
        worktree_root=tmp_path / "worktrees",
        forge=forge,
    )


def test_issue_outcome_is_found_without_listing_pull_requests(tmp_path):
    active = flat_active_worktree(
        issue_number=702,
        branch="claude/issue-702-task-a",
        worktree_path=str(tmp_path / "worktree"),
        pid=123,
        started_at=None,
        declared_footprint=(),
    )
    forge = MagicMock()
    forge.list_comments.return_value = [
        {
            "body": OutcomeRecord(result="done", issue=702).render(),
            "created_at": "2026-09-24T00:00:00Z",
        }
    ]
    forge.list_prs.side_effect = RuntimeError("504 Gateway Timeout")

    lookup = _fetch_outcome_for_active(active, forge)

    assert lookup.state is OutcomeLookupState.FOUND
    assert lookup.record is not None
    assert lookup.record.result == "done"
    forge.list_prs.assert_not_called()
    forge.list_comments.assert_called_once_with(702)


def test_pr_only_outcome_is_ignored_by_local_completion(tmp_path):
    active = flat_active_worktree(
        issue_number=702,
        branch="claude/issue-702-task-a",
        worktree_path=str(tmp_path / "worktree"),
        pid=123,
        started_at=None,
        declared_footprint=(),
    )
    forge = MagicMock()
    forge.list_prs.return_value = [
        PrRecord(
            number=703,
            head_ref=active.core.branch,
            changed_files=(),
            state="OPEN",
        )
    ]
    forge.list_comments.side_effect = lambda number: (
        []
        if number == active.core.issue_number
        else [
            {
                "body": OutcomeRecord(result="done", issue=702).render(),
                "created_at": "2026-09-24T00:00:00Z",
            }
        ]
    )

    assert _local_pr_completion_status(active, _config(tmp_path, forge)) == "pending"
    assert [call.args[0] for call in forge.list_comments.call_args_list] == [702]


def test_issue_comment_lookup_failure_holds_before_closed_pr_reclaim(
    fake_forge,
):
    active = _active(pid=123)
    task = _task(status_labels=("status:in-progress",))
    ctx = _rule_ctx(forge=fake_forge)
    fake_forge.list_comments.side_effect = RuntimeError("page two failed")
    fake_forge.list_prs.return_value = [
        PrRecord(
            number=210,
            head_ref=active.core.branch,
            changed_files=(),
            closes_issue_numbers=(active.core.issue_number,),
            state="CLOSED",
        )
    ]

    with patch(
        "orchestune.dispatch.gc.completion.is_process_alive",
        autospec=True,
        return_value=False,
    ):
        outcome = _rule_completed(ctx, "280", active, task)

    assert outcome is not None
    assert (
        outcome.completion_event.to_dict()["action"] == "completion_skipped_forge_error"
    )
    assert outcome.completion_event.to_dict()["operation"] == "list_comments"
    fake_forge.list_prs.assert_not_called()
    fake_forge.add_label.assert_not_called()


@pytest.mark.parametrize("failure", ["worktree", "close", "verification"])
def test_not_needed_finalization_failure_keeps_active_entry_for_retry(
    tmp_path, fake_forge, failure
):
    active = _active(pid=None)
    task = _task(status_labels=("status:in-progress",))
    ctx = _rule_ctx(
        forge=fake_forge,
        tasks_by_issue={active.core.issue_number: task},
    )
    ctx.config.apply = True
    ctx.run_state.active_worktrees["280"] = active
    fake_forge.get_issue.return_value = IssueRecord(
        number=280,
        title="task",
        body="",
        labels=("status:in-progress",),
        created_at="",
        state="OPEN",
    )
    if failure == "close":
        fake_forge.close_issue.side_effect = RuntimeError("close response lost")
    if failure == "worktree":
        remove_worktree = patch(
            "orchestune.dispatch.gc.completion.remove_worktree",
            side_effect=RuntimeError("worktree removal failed"),
        )
    else:
        remove_worktree = patch("orchestune.dispatch.gc.completion.remove_worktree")

    with (
        patch("orchestune.dispatch.gc._has_not_needed_outcome", return_value=True),
        patch(
            "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
            return_value=False,
        ),
        remove_worktree,
        pytest.raises(RuntimeError),
    ):
        _rule_not_needed(ctx, "280", active, task)

    assert ctx.run_state.active_worktrees["280"] is active
    assert ctx.run_state.completed_worktrees == []
    assert ctx.is_completion_confirmed(280) is False
    if failure == "worktree":
        fake_forge.close_issue.assert_not_called()
    else:
        fake_forge.close_issue.assert_called_once()
