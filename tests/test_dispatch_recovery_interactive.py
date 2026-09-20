"""#940: Tests for interactive claim ownership preservation in recovery and self-healing."""

from unittest.mock import MagicMock, patch

from orchestune.consistency.invariants.execution import RUN_STATE_MISSING
from orchestune.consistency.models import ConsistencyScope, RepairCommand, RepairStatus
from orchestune.consistency.repairs.execution import (
    COMMAND_BOOKKEEPING,
    COMMAND_REQUEUE,
)
from orchestune.dispatch.attempt_record import LaunchAttempt
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.dependency_resolution import EMPTY_DEPENDENCIES
from orchestune.dispatch.recovery import (
    RecoveryBookkeepingSnapshot,
    _build_restored_active_worktree,
    _restorable,
    execute_bookkeeping_repair_command,
    execute_recovery_requeue_command,
)
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.models import IssueRecord
from orchestune.task_branch_resolution import TaskBranchResolver
from tests.dispatch_gc_test_support import _task


def _snapshot(*restorations):
    return RecoveryBookkeepingSnapshot(
        tasks_by_issue={},
        open_prs=(),
        restorations=tuple(restorations),
        counter_targets=(),
        launch_history=(),
    )


def _bookkeeping_command(subject_id: str) -> RepairCommand:
    return RepairCommand(
        code=COMMAND_BOOKKEEPING,
        scope=ConsistencyScope.TASK,
        subject_id=subject_id,
        idempotency_key=f"execution:{subject_id}:bookkeeping",
        parameters=(("finding_codes", (RUN_STATE_MISSING,)),),
    )


class TestRestorationPreservesClaimOwnership:
    """#940: recovery による active 再構築で owner_kind / claim_id / reservation_kind を保持し、
    欠落時は dispatch として扱う。"""

    def test_restored_active_worktree_preserves_claim_ownership(self, tmp_path):
        body = (
            "## Footprint\n```yaml\n"
            "subtask_id: interactive-task\n"
            "footprint:\n"
            "  - src/interactive.py\n"
            "owner_kind: interactive\n"
            "claim_id: claim-recovery-999\n"
            "reservation_kind: repository\n"
            "```\n"
        )
        issue = IssueRecord(
            number=940,
            title="Interactive Task",
            body=body,
            labels=("status:in-progress",),
            created_at="2026-01-01T00:00:00+00:00",
        )
        resolver = TaskBranchResolver([])
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=str(tmp_path / "worktrees"),
        )

        active = _build_restored_active_worktree(
            issue=issue,
            subtask_id="interactive-task",
            declared_footprint=("src/interactive.py",),
            resolver=resolver,
            resolutions={},
            issue_to_subtask_id={940: "interactive-task"},
            dependency_resolution={940: EMPTY_DEPENDENCIES},
            config=config,
        )

        assert active.owner_kind == "interactive"
        assert active.claim_id == "claim-recovery-999"
        assert active.reservation_kind == "repository"

    def test_restored_active_worktree_defaults_missing_to_dispatch(self, tmp_path):
        body = (
            "## Footprint\n```yaml\n"
            "subtask_id: ordinary-task\n"
            "footprint:\n"
            "  - src/ordinary.py\n"
            "```\n"
        )
        issue = IssueRecord(
            number=941,
            title="Ordinary Task",
            body=body,
            labels=("status:in-progress",),
            created_at="2026-01-01T00:00:00+00:00",
        )
        resolver = TaskBranchResolver([])
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=str(tmp_path / "worktrees"),
        )

        active = _build_restored_active_worktree(
            issue=issue,
            subtask_id="ordinary-task",
            declared_footprint=("src/ordinary.py",),
            resolver=resolver,
            resolutions={},
            issue_to_subtask_id={941: "ordinary-task"},
            dependency_resolution={941: EMPTY_DEPENDENCIES},
            config=config,
        )

        assert active.owner_kind == "dispatch"
        assert active.claim_id is None
        assert active.reservation_kind == "footprint"

    def test_restored_active_worktree_with_launched_attempt_preserves_interactive_claim(
        self, tmp_path
    ):
        body = (
            "## Footprint\n```yaml\n"
            "subtask_id: interactive-task\n"
            "owner_kind: interactive\n"
            "claim_id: claim-recovery-launched-123\n"
            "reservation_kind: footprint\n"
            "footprint:\n"
            "  - src/interactive.py\n"
            "```\n\n"
            "<!-- orchestune:launch-attempt -->\n"
            "```json\n"
            "{\n"
            '  "attempt_id": "attempt-old-cloud-999",\n'
            '  "phase": "launched",\n'
            '  "target": "claude",\n'
            '  "branch": "task/subtask-942",\n'
            '  "base_branch": "main",\n'
            '  "external_id": "ext-job-12345",\n'
            '  "external_url": "https://example.com/job/12345",\n'
            '  "started_at": 1000.0\n'
            "}\n"
            "```\n"
        )
        issue = IssueRecord(
            number=942,
            title="Interactive Task with Prior Cloud Attempt",
            body=body,
            labels=("status:in-progress",),
            created_at="2026-01-01T00:00:00+00:00",
        )
        resolver = TaskBranchResolver([])
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=str(tmp_path / "worktrees"),
        )

        active = _build_restored_active_worktree(
            issue=issue,
            subtask_id="interactive-task",
            declared_footprint=("src/interactive.py",),
            resolver=resolver,
            resolutions={},
            issue_to_subtask_id={942: "interactive-task"},
            dependency_resolution={942: EMPTY_DEPENDENCIES},
            config=config,
        )

        assert active.owner_kind == "interactive"
        assert active.claim_id == "claim-recovery-launched-123"
        assert active.reservation_kind == "footprint"
        assert active.external_id is None
        assert active.launch_attempt_id is None
        assert active.launch_phase is None

    def test_restored_active_worktree_with_launched_attempt_defaults_to_dispatch(
        self, tmp_path
    ):
        body = (
            "## Footprint\n```yaml\n"
            "subtask_id: dispatch-task\n"
            "footprint:\n"
            "  - src/dispatch.py\n"
            "```\n\n"
            "<!-- orchestune:launch-attempt -->\n"
            "```json\n"
            "{\n"
            '  "attempt_id": "attempt-cloud-888",\n'
            '  "phase": "launched",\n'
            '  "target": "claude",\n'
            '  "branch": "task/subtask-943",\n'
            '  "base_branch": "main",\n'
            '  "external_id": "ext-job-88888",\n'
            '  "external_url": "https://example.com/job/88888",\n'
            '  "started_at": 2000.0\n'
            "}\n"
            "```\n"
        )
        issue = IssueRecord(
            number=943,
            title="Dispatch Task with Cloud Attempt",
            body=body,
            labels=("status:in-progress",),
            created_at="2026-01-01T00:00:00+00:00",
        )
        resolver = TaskBranchResolver([])
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=str(tmp_path / "worktrees"),
        )

        active = _build_restored_active_worktree(
            issue=issue,
            subtask_id="dispatch-task",
            declared_footprint=("src/dispatch.py",),
            resolver=resolver,
            resolutions={},
            issue_to_subtask_id={943: "dispatch-task"},
            dependency_resolution={943: EMPTY_DEPENDENCIES},
            config=config,
        )

        assert active.owner_kind == "dispatch"
        assert active.claim_id is None
        assert active.reservation_kind == "footprint"
        assert active.external_id == "ext-job-88888"
        assert active.launch_attempt_id == "attempt-cloud-888"
        assert active.launch_phase == "launched"

    def test_interactive_candidate_without_external_id_is_restorable_and_persisted(
        self, tmp_path, fake_forge
    ):
        active = ActiveWorktree(
            issue_number=105,
            branch="codex/issue-105-task",
            worktree_path=str(tmp_path / "worktrees" / "issue-105"),
            pid=None,
            started_at=None,
            declared_footprint=(),
            external_id=None,
            owner_kind="interactive",
            claim_id="claim-interactive-105",
        )
        assert _restorable(active) is True

        run_state = RunState(active_worktrees={})
        command = _bookkeeping_command("105")
        config = DispatcherConfig(
            apply=True,
            run_state_path=tmp_path / "run_state.json",
            events_log_path=tmp_path / "events.jsonl",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
        )
        fake_forge.get_issue_labels.return_value = ("status:in-progress",)

        result = execute_bookkeeping_repair_command(
            command,
            run_state,
            _snapshot(("105", "task-105", active)),
            config,
        )
        assert result.status is RepairStatus.APPLIED
        assert run_state.active_worktrees["105"].owner_kind == "interactive"

        run_state_empty = RunState(active_worktrees={})
        requeue_cmd = RepairCommand(
            code=COMMAND_REQUEUE,
            scope=ConsistencyScope.TASK,
            subject_id="105",
            idempotency_key="execution:105:requeue",
            parameters=(),
        )
        requeue_res = execute_recovery_requeue_command(
            requeue_cmd,
            run_state_empty,
            _snapshot(("105", "task-105", active)),
            config,
        )
        assert requeue_res.status is RepairStatus.SKIPPED
        assert (
            "a resumable execution resource is available" in requeue_res.diagnostics[0]
        )

    def test_interactive_issue_with_launched_attempt_skips_reconcile_attempt_override(
        self, tmp_path, fake_forge
    ):
        active = ActiveWorktree(
            issue_number=106,
            branch="codex/issue-106-task",
            worktree_path=str(tmp_path / "worktrees" / "issue-106"),
            pid=None,
            started_at=None,
            declared_footprint=(),
            external_id=None,
            owner_kind="interactive",
            claim_id="claim-interactive-106",
        )
        task = _task(
            issue_number=106,
            subtask_id="task-106",
            status_labels=("status:in-progress",),
        )
        run_state = RunState(active_worktrees={})
        command = _bookkeeping_command("106")
        target = MagicMock()
        target.launch_capabilities.durable_attempt = True
        config = DispatcherConfig(
            apply=True,
            run_state_path=tmp_path / "run_state.json",
            events_log_path=tmp_path / "events.jsonl",
            worktree_root=tmp_path / "worktrees",
            forge=fake_forge,
            dispatch_target=target,
        )
        fake_forge.get_issue_labels.return_value = ("status:in-progress",)

        attempt = LaunchAttempt(
            attempt_id="old-attempt-106",
            phase="launched",
            target="claude",
            branch="task/106",
            base_branch="main",
            started_at=100.0,
            external_id="old-ext-106",
        )

        with (
            patch("orchestune.dispatch.recovery.read_attempt", return_value=attempt),
            patch("orchestune.dispatch.recovery.reconcile_attempt") as mock_reconcile,
        ):
            result = execute_bookkeeping_repair_command(
                command,
                run_state,
                RecoveryBookkeepingSnapshot(
                    tasks_by_issue={106: task},
                    open_prs=(),
                    restorations=(("106", "task-106", active),),
                    counter_targets=(),
                    launch_history=(),
                    attempt_tasks=(task,),
                ),
                config,
            )
            mock_reconcile.assert_not_called()
            assert result.status is RepairStatus.APPLIED
            assert run_state.active_worktrees["106"].owner_kind == "interactive"
