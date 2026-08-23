"""dispatch_gcの`_rule_completed`本体のテスト。

test_dispatch_gc.py (1418行) から完了ルール関連(#479)を分割。gitプリミティブは
test_dispatch_gc_git_primitives.py、stale entryルールは
test_dispatch_gc_stale_rules.py、エンドツーエンド統合テストは
test_dispatch_gc_integration.py を参照。
"""

from unittest.mock import patch

from orchestune.dispatch_gc import _rule_completed
from orchestune.models import PrRecord
from tests.dispatch_gc_test_support import _active, _ctx, _task


class TestRuleCompleted:
    def test_closed_unmerged_local_pr_is_requeued_without_completing_dependency(
        self, fake_forge
    ):
        active = _active(pid=123, started_at=1_699_999_000.0)
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx(forge=fake_forge)
        ctx.config.apply = True
        ctx.run_state.active_worktrees["1"] = active
        ctx.prs = [
            PrRecord(
                number=210,
                head_ref=active.branch,
                changed_files=(),
                closes_issue_numbers=(active.issue_number,),
                state="CLOSED",
            )
        ]
        fake_forge.list_prs.return_value = ctx.prs
        with (
            patch(
                "orchestune.dispatch_gc_completion.is_process_alive", return_value=False
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc_completion.remove_worktree") as mock_remove,
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is not None
        assert outcome.terminal is True
        assert outcome.completed_subtask_id is None
        assert outcome.completion_event["action"] == "abandoned_pr_requeued"
        assert "1" not in ctx.run_state.active_worktrees
        fake_forge.list_prs.assert_called_once_with(state="all")
        mock_remove.assert_called_once_with(active.worktree_path)
        fake_forge.remove_label.assert_called_once_with(280, "status:in-progress")
        fake_forge.add_label.assert_called_once_with(280, "status:queued")
        fake_forge.add_comment.assert_called_once()

    def test_abandoned_requeue_adds_queued_before_removing_in_progress(self):
        # #381: 途中でクラッシュしてもIssueが必ずいずれかのstatus:*ラベルを
        # 持ち続けるよう、addがremoveより先に呼ばれなければならない。
        active = _active(pid=123, started_at=1_699_999_000.0)
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        ctx.config.apply = True
        ctx.run_state.active_worktrees["1"] = active
        ctx.prs = [
            PrRecord(
                number=210,
                head_ref=active.branch,
                changed_files=(),
                closes_issue_numbers=(active.issue_number,),
                state="CLOSED",
            )
        ]
        call_order: list[tuple[str, str]] = []
        with (
            patch(
                "orchestune.dispatch_gc_completion.is_process_alive", return_value=False
            ),
            patch("orchestune.forge.GitHubForge.list_prs", return_value=ctx.prs),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc_completion.remove_worktree"),
            patch(
                "orchestune.forge.GitHubForge.remove_label",
                side_effect=lambda issue, label: call_order.append(("remove", label)),
            ),
            patch(
                "orchestune.forge.GitHubForge.add_label",
                side_effect=lambda issue, label: call_order.append(("add", label)),
            ),
            patch("orchestune.forge.GitHubForge.add_comment"),
        ):
            _rule_completed(ctx, "1", active, task)

        assert call_order == [
            ("add", "status:queued"),
            ("remove", "status:in-progress"),
        ]

    def test_closed_unmerged_cloud_pr_is_requeued_without_completing_dependency(
        self,
    ):
        active = _active(external_id="session-1")
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        ctx.config.apply = True
        ctx.run_state.active_worktrees["1"] = active

        with (
            patch.object(
                ctx.config.dispatch_target,
                "completion_status",
                return_value="abandoned",
                create=True,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc_completion.remove_worktree") as mock_remove,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is not None
        assert outcome.terminal is True
        assert outcome.completed_subtask_id is None
        assert outcome.completion_event["action"] == "abandoned_pr_requeued"
        assert "1" not in ctx.run_state.active_worktrees
        mock_remove.assert_called_once_with(active.worktree_path)
        mock_remove_label.assert_called_once_with(280, "status:in-progress")
        mock_add_label.assert_called_once_with(280, "status:queued")
        mock_add_comment.assert_called_once()

    def test_local_pr_waits_for_process_before_using_open_pr_as_completion(self):
        active = _active(pid=123, started_at=1_699_999_000.0)
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        with (
            patch(
                "orchestune.dispatch_gc_completion.is_process_alive", return_value=True
            ),
            patch("orchestune.forge.GitHubForge.list_prs") as mock_list_prs,
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is None
        mock_list_prs.assert_not_called()

    def test_local_closed_pr_closed_before_launch_is_ignored_as_stale(self):
        active = _active(pid=123, started_at=1_800_000_000.0)
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        ctx.config.apply = True
        ctx.run_state.active_worktrees["1"] = active
        stale_pr = PrRecord(
            number=210,
            head_ref=active.branch,
            changed_files=(),
            closes_issue_numbers=(active.issue_number,),
            created_at="2026-01-01T00:00:00Z",
            closed_at="2026-01-02T00:00:00Z",
            state="CLOSED",
        )
        with (
            patch(
                "orchestune.dispatch_gc_completion.is_process_alive", return_value=False
            ),
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[stale_pr]),
            patch(
                "orchestune.dispatch_gc._finalize_completed_worktree",
                return_value={"action": "completed_no_commits"},
            ),
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is not None
        assert outcome.completion_event["action"] == "completed_no_commits"
        assert "1" not in ctx.run_state.active_worktrees

    def test_local_existing_pr_closed_after_launch_is_requeued(self):
        active = _active(pid=123, started_at=1_800_000_000.0)
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        ctx.config.apply = True
        ctx.run_state.active_worktrees["1"] = active
        closed_pr = PrRecord(
            number=210,
            head_ref=active.branch,
            changed_files=(),
            closes_issue_numbers=(active.issue_number,),
            created_at="2026-01-01T00:00:00Z",
            closed_at="2030-01-01T00:00:00Z",
            state="CLOSED",
        )
        with (
            patch(
                "orchestune.dispatch_gc_completion.is_process_alive", return_value=False
            ),
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[closed_pr]),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc_completion.remove_worktree"),
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch("orchestune.forge.GitHubForge.add_label"),
            patch("orchestune.forge.GitHubForge.add_comment"),
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is not None
        assert outcome.completion_event["action"] == "abandoned_pr_requeued"
        assert outcome.completed_subtask_id is None

    def test_all_state_lookup_failure_holds_local_completion_for_retry(self):
        active = _active(pid=123)
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        with (
            patch(
                "orchestune.dispatch_gc_completion.is_process_alive", return_value=False
            ),
            patch(
                "orchestune.forge.GitHubForge.list_prs",
                side_effect=RuntimeError("temporary GitHub failure"),
            ),
            patch(
                "orchestune.dispatch_gc._finalize_completed_worktree",
                return_value={"action": "completed_no_commits"},
            ) as mock_finalize,
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is None
        mock_finalize.assert_not_called()

    def test_recovered_entry_uses_all_state_prs_and_requeues_closed_pr(self):
        active = _active(pid=None, started_at=None)
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        ctx.config.apply = True
        ctx.run_state.active_worktrees["1"] = active
        closed_pr = PrRecord(
            number=210,
            head_ref=active.branch,
            changed_files=(),
            closes_issue_numbers=(active.issue_number,),
            state="CLOSED",
        )
        with (
            patch(
                "orchestune.forge.GitHubForge.list_prs", return_value=[closed_pr]
            ) as mock_list_prs,
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc_completion.remove_worktree"),
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch("orchestune.forge.GitHubForge.add_label"),
            patch("orchestune.forge.GitHubForge.add_comment"),
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is not None
        assert outcome.completion_event["action"] == "abandoned_pr_requeued"
        assert outcome.completed_subtask_id is None
        mock_list_prs.assert_called_once_with(state="all")

    def test_pending_cloud_completion_status_returns_none(self):
        active = _active(external_id="session-1")
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        with patch.object(
            ctx.config.dispatch_target,
            "completion_status",
            return_value="pending",
            create=True,
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is None

    def test_dirty_worktree_is_terminal(self):
        active = _active()
        task = _task()
        ctx = _ctx()
        with (
            patch(
                "orchestune.dispatch_gc._is_worktree_complete",
                return_value=True,
            ),
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch(
                "orchestune.dispatch_gc._finalize_completed_worktree",
                return_value={"action": "completion_skipped_dirty_worktree"},
            ),
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is not None
        assert outcome.terminal is True
        assert outcome.completion_event["action"] == "completion_skipped_dirty_worktree"

    def test_completed_worktree_inherits_base_branch(self):
        active = _active(base_branch="parent-branch")
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        ctx.config.apply = True
        ctx.run_state.active_worktrees["1"] = active

        with (
            patch(
                "orchestune.dispatch_gc._is_worktree_complete",
                return_value=True,
            ),
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch(
                "orchestune.dispatch_gc._finalize_completed_worktree",
                return_value={"action": "completed", "commit_sha": "abc123d"},
            ),
        ):
            outcome = _rule_completed(ctx, "1", active, task)

        assert outcome is not None
        assert len(ctx.run_state.completed_worktrees) == 1
        assert ctx.run_state.completed_worktrees[0].base_branch == "parent-branch"

    def test_completed_worktree_preserves_unknown_start_time(self):
        active = _active(started_at=None)
        task = _task(status_labels=("status:in-progress",))
        ctx = _ctx()
        ctx.config.apply = True
        ctx.run_state.active_worktrees["1"] = active
        ctx.prs = [
            PrRecord(
                number=281,
                head_ref="agent/issue-280-task-a",
                changed_files=(),
                closes_issue_numbers=(280,),
            )
        ]

        with (
            patch("orchestune.forge.GitHubForge.list_prs", return_value=ctx.prs),
            patch(
                "orchestune.dispatch_gc._finalize_completed_worktree",
                return_value={"action": "completed", "commit_sha": "abc123d"},
            ),
        ):
            _rule_completed(ctx, "1", active, task)

        assert ctx.run_state.completed_worktrees[0].started_at is None
