"""#943レビュー対応: claim_task失敗outcomeのLaunchResultへのマッピングと、
claim成功後にagent起動が失敗した場合のラベル遷移に関するテスト。

`test_dispatch_launch_basic.py`の肥大化検知回避のため分離。
"""

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.launch import TaskLaunchPlan
from orchestune.dispatch.scoring import Task
from tests.conftest import register_task_issue


def _task(issue_number, subtask_id=None):
    resolved_subtask_id = subtask_id or f"task-{issue_number}"
    register_task_issue(issue_number, resolved_subtask_id)
    return Task(
        issue_number=issue_number,
        subtask_id=resolved_subtask_id,
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:queued",),
        created_at="2023-01-01T00:00:00+00:00",
        depends_on=(),
    )


class TestResolveClaimFailureLaunchResult:
    """#943レビュー対応(Codex P2): INVALID_BRANCH_NAMEのみvalidation_error。"""

    def _plan(self, tmp_path):
        task = _task(1)
        return TaskLaunchPlan(task, "claude/issue-1-task-1", None, "origin/main")

    def _outcome(self, reason, message):
        from orchestune.claim.contracts import ClaimFailure, ClaimOutcome

        return ClaimOutcome(
            success=False,
            issue_number=1,
            failure=ClaimFailure(reason=reason, message=message),
        )

    def test_invalid_branch_name_is_a_validation_error(self, tmp_path):
        from orchestune.claim.contracts import ClaimFailureReason
        from orchestune.dispatch.launch import _resolve_claim_failure_launch_result

        outcome = self._outcome(
            ClaimFailureReason.INVALID_BRANCH_NAME, "invalid branch name"
        )
        result = _resolve_claim_failure_launch_result(self._plan(tmp_path), outcome)
        assert result.validation_error is True

    def test_worktree_creation_failed_is_not_a_validation_error(self, tmp_path):
        from orchestune.claim.contracts import ClaimFailureReason
        from orchestune.dispatch.launch import _resolve_claim_failure_launch_result

        outcome = self._outcome(
            ClaimFailureReason.WORKTREE_CREATION_FAILED, "disk full"
        )
        result = _resolve_claim_failure_launch_result(self._plan(tmp_path), outcome)
        assert result.validation_error is False


class TestTryPlannedLaunchPassesWorktreeRoot:
    """#943レビュー対応(Codex P1, round4): dispatchが`--worktree-root`で
    既定値以外を設定した場合、claimへの`ClaimRequest`にもそれを渡さないと、
    claimは`<repo>/worktrees`へ固定してしまい、agentの起動先とdispatch自身の
    journal復元・GCが参照するディレクトリが食い違う。"""

    def test_claim_request_carries_configured_worktree_root(self, tmp_path):
        from orchestune.claim.contracts import (
            ClaimFailure,
            ClaimFailureReason,
            ClaimOutcome,
        )
        from orchestune.dispatch.launch import TaskLaunchPlan, _try_planned_launch
        from orchestune.dispatch.state import RunState

        custom_root = tmp_path / "custom-worktrees"
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=custom_root,
        )
        plan = TaskLaunchPlan(_task(1), "claude/issue-1-task-1", None, "origin/main")
        captured = {}

        def _spy_claim_fn(request, default_base):
            captured["worktree_root"] = request.worktree_root
            return ClaimOutcome(
                success=False,
                issue_number=1,
                failure=ClaimFailure(
                    reason=ClaimFailureReason.ISSUE_NOT_FOUND, message="n/a"
                ),
            )

        _try_planned_launch(plan, object(), config, RunState(), _spy_claim_fn)

        assert captured["worktree_root"] == custom_root


class TestHandleLaunchFailureStripsClaimLabel:
    """#943レビュー対応(Codex P1, r3): claim成功後にagent起動が失敗した場合、
    IN_PROGRESSを剥がさないと`status:blocked`と併存してしまう。"""

    def test_strips_in_progress_when_launch_carries_a_claim_id(self, tmp_path):
        from unittest.mock import patch

        from orchestune.dispatch.launch import _handle_launch_failure
        from orchestune.dispatch.worktree import LaunchResult
        from orchestune.labels import StatusLabel

        task = _task(1)
        launch = LaunchResult(
            issue_number=1,
            branch="claude/issue-1-task-1",
            worktree_path="worktrees/w1",
            pid=None,
            launched=False,
            error_message="provider call failed",
            claim_id="claim-xyz",
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with patch("fake_forge_proxy.active_fake_forge.remove_label") as mock_remove:
            _handle_launch_failure(task, launch, config)

        assert (1, StatusLabel.IN_PROGRESS) in [
            (call.args[0], call.args[1]) for call in mock_remove.call_args_list
        ]
