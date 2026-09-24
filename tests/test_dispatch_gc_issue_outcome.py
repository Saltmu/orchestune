"""Issue-canonical Outcome lookup regressions for GC (#1002)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.gc import _rule_completed
from orchestune.dispatch.gc.completion import (
    _fetch_outcome_for_active,
    _local_pr_completion_status,
)
from orchestune.dispatch.state import ActiveWorktree
from orchestune.models import PrRecord
from orchestune.outcome_record import OutcomeLookupState, OutcomeRecord
from tests.dispatch_gc_test_support import _active, _rule_ctx, _task


def _config(tmp_path, forge):
    return DispatcherConfig(
        parent_issue_number=100,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "run_state.json",
        worktree_root=tmp_path / "worktrees",
        forge=forge,
    )


def test_issue_outcome_is_found_without_listing_pull_requests(tmp_path):
    active = ActiveWorktree(
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
    active = ActiveWorktree(
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
            head_ref=active.branch,
            changed_files=(),
            state="OPEN",
        )
    ]
    forge.list_comments.side_effect = lambda number: (
        []
        if number == active.issue_number
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
            head_ref=active.branch,
            changed_files=(),
            closes_issue_numbers=(active.issue_number,),
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
    assert outcome.completion_event["action"] == "completion_skipped_forge_error"
    assert outcome.completion_event["operation"] == "list_comments"
    fake_forge.list_prs.assert_not_called()
    fake_forge.add_label.assert_not_called()
