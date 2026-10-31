"""#943レビュー対応: claim_task失敗outcomeのLaunchResultへのマッピングと、
claim成功後にagent起動が失敗した場合のラベル遷移に関するテスト。

`test_dispatch_launch_basic.py`の肥大化検知回避のため分離。
"""

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.launch import TaskLaunchPlan
from orchestune.dispatch.scoring import Task
from tests.conftest import register_task_issue


def _task(issue_number, subtask_id=None, footprint_error=None, yaml_error=False):
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
        yaml_error=yaml_error,
        footprint_error=footprint_error,
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

    def test_invalid_footprint_is_a_validation_error(self, tmp_path):
        from orchestune.claim.contracts import ClaimFailureReason
        from orchestune.dispatch.launch import _resolve_claim_failure_launch_result

        outcome = self._outcome(
            ClaimFailureReason.INVALID_FOOTPRINT, "invalid footprint"
        )
        result = _resolve_claim_failure_launch_result(self._plan(tmp_path), outcome)
        assert result.validation_error is True


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
        from orchestune.ledger.run_state import RunState

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


class TestDecideInvalidFootprintTasks:
    """decide層: 副作用なしでFootprint不正のタスクのみを判定する。"""

    def test_returns_only_invalid_footprint_tasks(self):
        from orchestune.dispatch.launch import _decide_invalid_footprint_tasks

        ok_task = _task(1)
        bad_task = _task(2, footprint_error="invalid path")
        yaml_bad_task = _task(3, yaml_error=True, footprint_error="syntax error")
        assert _decide_invalid_footprint_tasks([ok_task, bad_task, yaml_bad_task]) == [
            bad_task
        ]

    def test_returns_empty_when_no_errors(self):
        from orchestune.dispatch.launch import _decide_invalid_footprint_tasks

        assert _decide_invalid_footprint_tasks([_task(1)]) == []


class TestApplyInvalidFootprintBlocking:
    def test_transitions_to_blocked_and_posts_comment(self, tmp_path):
        from unittest.mock import patch

        from orchestune.dispatch.launch import _apply_invalid_footprint_blocking

        task = _task(
            1, subtask_id="task-1", footprint_error="invalid path: /etc/passwd"
        )
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        call_order: list[tuple[str, str]] = []

        with (
            patch(
                "fake_forge_proxy.active_fake_forge.add_label",
                side_effect=lambda issue, label: call_order.append(("add", label)),
            ),
            patch(
                "fake_forge_proxy.active_fake_forge.remove_label",
                side_effect=lambda issue, label: call_order.append(("remove", label)),
            ),
            patch("fake_forge_proxy.active_fake_forge.add_comment") as mock_comment,
        ):
            _apply_invalid_footprint_blocking([task], config)

        assert call_order == [
            ("add", "status:blocked-human-review"),
            ("remove", "status:queued"),
        ]
        assert "invalid path: /etc/passwd" in mock_comment.call_args[0][1]
        assert "footprint が不正なため" in mock_comment.call_args[0][1]


class TestLaunchSelectedTasksInvalidFootprint:
    def test_blocks_invalid_footprint_candidate_not_included_in_selected(
        self, tmp_path
    ):
        from unittest.mock import patch

        from orchestune.dispatch.launch import LaunchContext, _launch_selected_tasks
        from orchestune.ledger.run_state import RunState
        from tests.conftest import real_claim_fn

        bad_task = _task(1, subtask_id="bad-task", footprint_error="bad path")
        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        run_state = RunState(active_worktrees={})
        ctx = LaunchContext(
            selected=[],
            task_to_base_branch={},
            candidate_tasks=[bad_task],
            run_state=run_state,
            now=1000.0,
            config=config,
            claim_fn=real_claim_fn(config),
        )

        with (
            patch("fake_forge_proxy.active_fake_forge.add_label") as mock_add_label,
            patch("fake_forge_proxy.active_fake_forge.remove_label"),
            patch("fake_forge_proxy.active_fake_forge.add_comment") as mock_comment,
        ):
            _launch_selected_tasks(ctx)

        mock_add_label.assert_called_once_with(1, "status:blocked-human-review")
        assert "bad path" in mock_comment.call_args[0][1]
        assert "footprint が不正なため" in mock_comment.call_args[0][1]


class TestRetiredModelTierBlocksLaunchAndClaim:
    """#1473: 子Issue本文の非null `model_tier` は既存のfootprint_error経路で
    起動除外・blocked-human-review遷移・claim拒否される。"""

    @staticmethod
    def _issue():
        from orchestune.models import IssueRecord

        return IssueRecord(
            number=7,
            title="[FEAT] legacy-tier: test",
            body=(
                "```yaml\n"
                "subtask_id: legacy-tier\n"
                "footprint: [src/a.py]\n"
                "execution_profile: balanced\n"
                "model_tier: strong\n"
                "```\n"
            ),
            labels=("status:queued",),
            created_at="2026-01-01T00:00:00Z",
        )

    def test_queued_task_is_excluded_blocked_and_commented(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from orchestune.config_contracts import retired_model_tier_message
        from orchestune.dispatch.launch import (
            LaunchContext,
            _decide_invalid_footprint_tasks,
            _launch_selected_tasks,
        )
        from orchestune.dispatch.scoring import (
            REASON_INVALID_FOOTPRINT,
            _ineligibility_reason,
        )
        from orchestune.issue_parsing import parse_task_from_issue
        from orchestune.ledger.run_state import RunState

        task = parse_task_from_issue(self._issue())
        assert _ineligibility_reason(task, set()) == REASON_INVALID_FOOTPRINT
        assert _decide_invalid_footprint_tasks([task]) == [task]

        config = DispatcherConfig(
            parent_issue_number=100,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        claim_fn = MagicMock()
        ctx = LaunchContext(
            selected=[],
            task_to_base_branch={},
            candidate_tasks=[task],
            run_state=RunState(active_worktrees={}),
            now=1000.0,
            config=config,
            claim_fn=claim_fn,
        )

        with (
            patch("fake_forge_proxy.active_fake_forge.add_label") as mock_add_label,
            patch("fake_forge_proxy.active_fake_forge.remove_label") as mock_remove,
            patch("fake_forge_proxy.active_fake_forge.add_comment") as mock_comment,
        ):
            _launch_selected_tasks(ctx)

        claim_fn.assert_not_called()
        mock_add_label.assert_called_once_with(7, "status:blocked-human-review")
        mock_remove.assert_called_once_with(7, "status:queued")
        comment = mock_comment.call_args[0][1]
        assert "footprint が不正なため" in comment
        assert retired_model_tier_message() in comment

    def test_claim_preflight_rejects_with_invalid_footprint(self):
        from orchestune.claim.contracts import ClaimExitCode, ClaimFailureReason
        from orchestune.claim.preflight import evaluate_claim_preflight
        from orchestune.config_contracts import retired_model_tier_message

        decision = evaluate_claim_preflight(self._issue())

        assert decision.allowed is False
        assert decision.failure is not None
        assert decision.failure.reason == ClaimFailureReason.INVALID_FOOTPRINT
        assert decision.failure.exit_code == ClaimExitCode.INVALID_FOOTPRINT
        assert retired_model_tier_message() in decision.failure.message
