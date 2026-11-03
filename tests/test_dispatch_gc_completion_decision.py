"""Completion outcome decisions for an active worktree.

The decision functions stay in ``orchestune.dispatch.gc.completion``. These
tests patch that module, which is unchanged by moving the tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.gc.cloud_completion import _cloud_worktree_completion_status
from orchestune.dispatch.gc.completion import (
    _decide_completed_worktree_outcome,
    _decide_not_needed_dirty_worktree,
    _is_worktree_complete,
)
from orchestune.dispatch.targets import CodexCloudDispatchTarget
from orchestune.models import IssueRecord, PrRecord
from orchestune.outcome_record import OutcomeRecord
from tests.dispatch_gc_test_support import _active, _task


class TestDecideCompletedWorktreeOutcome:
    """decide層: worktree_has_uncommitted_changes/worktree_has_new_commitsの
    読み取りのみで方針を判定し、github/worktreeへの書き込みは行わない。"""

    def test_dirty_worktree_is_skipped(self):
        active = _active()
        task = _task()
        with patch(
            "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
            autospec=True,
            return_value=True,
        ):
            decision = _decide_completed_worktree_outcome(active, task)
        assert decision.action == "completion_skipped_dirty_worktree"

    def test_no_new_commits_is_completed_no_commits(self):
        active = _active()
        task = _task()
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                autospec=True,
                return_value=False,
            ),
        ):
            decision = _decide_completed_worktree_outcome(active, task)
        assert decision.action == "completed_no_commits"
        assert decision.subtask_id == "task-a"

    def test_verified_prior_parent_merge_beats_zero_commit_escalation(self):
        active = _active()
        task = _task(parent_number=100, status_labels=("status:in-progress",))
        issue = IssueRecord(
            number=280,
            title="child",
            body="",
            labels=("status:in-progress",),
            created_at="2026-09-01T00:00:00Z",
            parent={"number": 100},
        )
        forge = MagicMock()
        forge.list_merged_prs_for_base.return_value = [
            PrRecord(
                number=301,
                head_ref="claude/issue-280-task-a",
                changed_files=(),
                state="MERGED",
                base_ref="parent/issue-100",
                closes_issue_numbers=(280,),
                is_cross_repository=False,
                merged_at="2026-09-02T00:00:00Z",
                merge_commit_oid="a" * 40,
            )
        ]
        forge.get_issue_last_reopened_at.return_value = None
        forge.is_merge_commit_reachable_from.return_value = True
        forge.get_issue.return_value = issue
        with patch(
            "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
            autospec=True,
            return_value=False,
        ):
            decision = _decide_completed_worktree_outcome(
                active, task, forge=forge, issue=issue
            )

        assert decision.action == "already_merged"

    def test_indeterminate_prior_merge_holds_without_zero_commit_escalation(self):
        active = _active()
        task = _task(parent_number=100, status_labels=("status:in-progress",))
        issue = IssueRecord(
            number=280,
            title="child",
            body="",
            labels=("status:in-progress",),
            created_at="2026-09-01T00:00:00Z",
            parent={"number": 100},
        )
        forge = MagicMock()
        forge.list_merged_prs_for_base.side_effect = RuntimeError("API unavailable")
        with patch(
            "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
            autospec=True,
            return_value=False,
        ):
            decision = _decide_completed_worktree_outcome(
                active, task, forge=forge, issue=issue
            )

        assert decision.action == "completion_skipped_prior_merge_indeterminate"

    def test_no_new_commits_with_forge_error_holds_for_retry(
        self,
    ):
        active = _active()
        task = _task()
        fake_forge = MagicMock()
        fake_forge.list_comments.side_effect = RuntimeError("network error")
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                autospec=True,
                return_value=False,
            ),
        ):
            decision = _decide_completed_worktree_outcome(
                active, task, forge=fake_forge
            )
        assert decision.action == "completion_skipped_forge_error"
        assert decision.operation == "list_comments"
        assert decision.subtask_id == "task-a"

    def test_clean_with_new_commits_without_outcome_is_completed_without_outcome(self):
        active = _active()
        task = _task()
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                autospec=True,
                return_value=True,
            ),
        ):
            decision = _decide_completed_worktree_outcome(active, task)
        assert decision.action == "completed_without_outcome"
        assert decision.subtask_id == "task-a"

    def test_clean_with_new_commits_and_outcome_done_is_completed(self):
        active = _active()
        task = _task()
        fake_forge = MagicMock()
        outcome = OutcomeRecord(result="done", issue=280)
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
        ]
        fake_forge.list_prs.return_value = []
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                autospec=True,
                return_value=True,
            ),
        ):
            decision = _decide_completed_worktree_outcome(
                active, task, forge=fake_forge
            )
        assert decision.action == "completed"
        assert decision.subtask_id == "task-a"

    def test_clean_with_new_commits_and_outcome_not_needed_is_not_needed(self):
        active = _active()
        task = _task()
        fake_forge = MagicMock()
        outcome = OutcomeRecord(result="not-needed", issue=280)
        fake_forge.list_comments.return_value = [
            {"body": outcome.render(), "created_at": "2026-01-01T00:00:00Z"}
        ]
        fake_forge.list_prs.return_value = []
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                autospec=True,
                return_value=True,
            ),
        ):
            decision = _decide_completed_worktree_outcome(
                active, task, forge=fake_forge
            )
        assert decision.action == "not_needed"
        assert decision.subtask_id == "task-a"

    def test_ignores_stale_closed_pr_outcome(self):
        active = _active(started_at=1700000100.0)
        task = _task()
        fake_forge = MagicMock()
        fake_forge.list_comments.side_effect = lambda num: (
            []
            if num == 280
            else [
                {
                    "body": OutcomeRecord(result="done", issue=280).render(),
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ]
        )
        stale_closed_pr = PrRecord(
            number=999,
            head_ref="claude/issue-280-task-a",
            changed_files=(),
            state="CLOSED",
            closed_at="2023-11-14T22:14:00Z",  # Before started_at (1700000100.0 is Nov 14 2023 22:15:00 UTC)
        )
        fake_forge.list_prs.return_value = [stale_closed_pr]
        with (
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                autospec=True,
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                autospec=True,
                return_value=True,
            ),
        ):
            decision = _decide_completed_worktree_outcome(
                active, task, forge=fake_forge
            )
        # Since the stale PR is ignored and Issue #280 has no comments, it is completed_without_outcome
        assert decision.action == "completed_without_outcome"
        assert decision.subtask_id == "task-a"


class TestDecideNotNeededDirtyWorktree:
    def test_true_when_dirty(self):
        with patch(
            "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
            autospec=True,
            return_value=True,
        ):
            assert _decide_not_needed_dirty_worktree(_active()) is True

    def test_false_when_clean(self):
        with patch(
            "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
            autospec=True,
            return_value=False,
        ):
            assert _decide_not_needed_dirty_worktree(_active()) is False


class TestIsWorktreeComplete:
    """#239: external_id経由の完了判定に、issue_numberが正しく引き渡されること。"""

    def test_passes_issue_number_to_dispatch_target_handle(self, tmp_path, fake_forge):
        fake_target = MagicMock()
        fake_target.completion_status.return_value = "completed"
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            dispatch_target=fake_target,
            forge=fake_forge,
        )
        active = _active(
            issue_number=218,
            branch="claude/issue-218-review-history-backend-api",
            worktree_path=str(tmp_path / "w1"),
            pid=None,
            started_at=1_699_999_000.0,
            declared_footprint=("src/foo.py",),
            external_id="session_1",
            external_url="https://claude.ai/code/session_1",
        )

        result = _is_worktree_complete(active, config)

        assert result is True
        handle = fake_target.completion_status.call_args.args[0]
        assert handle.issue_number == 218
        assert handle.branch_name == "claude/issue-218-review-history-backend-api"

    def test_codex_cloud_active_worktree_waits_for_pr(self, tmp_path, fake_forge):
        target = CodexCloudDispatchTarget("env_123")
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            dispatch_target=target,
            forge=fake_forge,
        )
        active = _active(
            issue_number=1,
            branch="claude/issue-1-task-a",
            worktree_path=str(tmp_path / "w1"),
            pid=4_204_242,
            started_at=1_699_999_000.0,
            declared_footprint=("src/foo.py",),
            external_id="codex-cloud:claude/issue-1-task-a",
        )

        with (
            patch(
                "orchestune.dispatch.gc.completion.is_process_alive", autospec=True
            ) as mock_is_alive,
        ):
            assert _is_worktree_complete(active, config) is False

        mock_is_alive.assert_not_called()

    def test_codex_cloud_active_worktree_reclaims_when_task_failed_in_cloud(
        self, tmp_path, fake_forge
    ):
        target = CodexCloudDispatchTarget("env_123")
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            dispatch_target=target,
            forge=fake_forge,
        )
        active = _active(
            issue_number=1,
            branch="claude/issue-1-task-a",
            worktree_path=str(tmp_path / "w1"),
            pid=4_204_242,
            started_at=1_699_999_000.0,
            declared_footprint=("src/foo.py",),
            external_id="task_fail_123",
        )

        with (
            patch.object(
                target, "_fetch_task_status", autospec=True, return_value="failed"
            ),
        ):
            assert _cloud_worktree_completion_status(active, config) == "abandoned"

    def test_recovered_local_active_worktree_waits_for_pid_reconciliation(
        self, tmp_path, fake_forge
    ):
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            forge=fake_forge,
        )
        active = _active(
            issue_number=1,
            branch="claude/issue-1-task-a",
            worktree_path=str(tmp_path / "missing-worktree"),
            pid=None,
            started_at=None,
            declared_footprint=(),
        )

        assert _is_worktree_complete(active, config) is False

    def test_legacy_handoff_without_completion_id_is_not_complete(
        self, tmp_path, fake_forge
    ):
        """T09: 予約なし legacy handoff (completion_handoff_ready=True, completion_id=None) はプロセス終了でも False。"""
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            forge=fake_forge,
        )
        active = _active(
            issue_number=1,
            branch="claude/issue-1-task-a",
            worktree_path=str(tmp_path / "w1"),
            pid=4_200_123,
            started_at=1_699_999_000.0,
            completion_handoff_ready=True,
            completion_stage="handed_off_to_gc",
            completion_id=None,
        )
        with patch(
            "orchestune.dispatch.gc.completion.is_process_alive", return_value=False
        ):
            assert _is_worktree_complete(active, config) is False

    def test_completing_state_with_completion_id_is_not_complete(
        self, tmp_path, fake_forge
    ):
        """T09: completing状態 (completion_id is not None) は False。"""
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            forge=fake_forge,
        )
        active = _active(
            issue_number=1,
            branch="claude/issue-1-task-a",
            worktree_path=str(tmp_path / "w1"),
            pid=4_200_123,
            started_at=1_699_999_000.0,
            completion_id="cmpl-123",
            completion_stage="completing",
        )
        with patch(
            "orchestune.dispatch.gc.completion.is_process_alive", return_value=False
        ):
            assert _is_worktree_complete(active, config) is False
