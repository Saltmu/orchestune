from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

from orchestune.consistency.invariants.execution import (
    EXECUTION_OBSERVATION_UNKNOWN,
    EXECUTION_TIMED_OUT,
    LOCAL_PROCESS_DEAD,
    RUN_STATE_MISSING,
    RUN_STATE_STALE,
)
from orchestune.consistency.models import (
    ConsistencyScope,
    RepairCommand,
    RepairResult,
    RepairStatus,
)
from orchestune.consistency.repairs.execution import (
    COMMAND_BOOKKEEPING,
    COMMAND_RECLAIM,
    COMMAND_REQUEUE,
)
from orchestune.consistency.repairs.status import (
    COMMAND_ADD_LABEL,
    COMMAND_REMOVE_LABEL,
    COMMAND_TRANSITION_LABEL,
)
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.execution_repair import (
    REPAIR_COMMAND_BINDINGS,
    DispatchRepairExecutorAdapter,
    RepairCommandDomain,
    RepairCommandOperation,
    evaluate_execution_repair_plan,
)
from orchestune.dispatch.gc.zombies import _collect_zombies_and_timeouts
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import ActiveWorktree, RunState


def _task(issue_number: int, *labels: str) -> Task:
    return Task(
        issue_number=issue_number,
        subtask_id=f"task-{issue_number}",
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=labels or ("status:in-progress",),
        created_at="2026-08-29T00:00:00+00:00",
        parent_number=700,
    )


def _active(
    tmp_path,
    issue_number: int,
    *,
    pid: int | None = 100,
    external_id: str | None = None,
    started_at: float | None = 1_000.0,
) -> ActiveWorktree:
    worktree = tmp_path / f"worktree-{issue_number}"
    worktree.mkdir()
    return ActiveWorktree(
        issue_number=issue_number,
        branch=f"codex/issue-{issue_number}",
        worktree_path=str(worktree),
        pid=pid,
        started_at=started_at,
        declared_footprint=(),
        external_id=external_id,
        base_branch="parent/issue-700",
    )


@dataclass
class _ExternalTarget:
    status: str = "running"
    error: Exception | None = None

    def completion_status(self, handle, *, forge):
        del handle, forge
        if self.error is not None:
            raise self.error
        return self.status


def _config(tmp_path, fake_forge, **overrides) -> DispatcherConfig:
    values = {
        "events_log_path": tmp_path / "events.jsonl",
        "run_state_path": tmp_path / "run_state.json",
        "worktree_root": tmp_path / "worktrees",
        "forge": fake_forge,
        "zombie_gc": True,
        "task_timeout_seconds": 0,
    }
    values.update(overrides)
    return DispatcherConfig(**values)


def _codes(evaluation) -> list[str]:
    return [finding.code for finding in evaluation.report.findings]


def _command_codes(evaluation, issue_number: int) -> list[str]:
    return [
        command.code
        for command in evaluation.commands
        if command.subject_id == str(issue_number)
    ]


def _repair_command(code: str, *, suffix: str = "") -> RepairCommand:
    return RepairCommand(
        code=code,
        scope=ConsistencyScope.TASK,
        subject_id="742",
        idempotency_key=f"test:{code}{suffix}",
    )


def test_every_automatic_command_has_one_explicit_adapter_domain() -> None:
    assert {
        code: (binding.domain, binding.operation)
        for code, binding in REPAIR_COMMAND_BINDINGS.items()
    } == {
        COMMAND_ADD_LABEL: (
            RepairCommandDomain.STATUS,
            RepairCommandOperation.FORGE_ADD_LABEL,
        ),
        COMMAND_REMOVE_LABEL: (
            RepairCommandDomain.STATUS,
            RepairCommandOperation.FORGE_REMOVE_LABEL,
        ),
        COMMAND_TRANSITION_LABEL: (
            RepairCommandDomain.STATUS,
            RepairCommandOperation.FORGE_TRANSITION_LABEL,
        ),
        COMMAND_RECLAIM: (
            RepairCommandDomain.GC,
            RepairCommandOperation.GC_RECLAIM_LIFECYCLE,
        ),
        COMMAND_REQUEUE: (
            RepairCommandDomain.EXECUTION,
            RepairCommandOperation.GC_REQUEUE_NOTIFICATION,
        ),
        COMMAND_BOOKKEEPING: (
            RepairCommandDomain.RECOVERY,
            RepairCommandOperation.RECOVERY_BOOKKEEPING,
        ),
    }


def test_dispatch_adapter_routes_an_exact_registered_command() -> None:
    command = _repair_command(COMMAND_RECLAIM)
    seen: list[RepairCommand] = []

    def apply(selected: RepairCommand) -> RepairResult:
        seen.append(selected)
        return RepairResult(command=selected, status=RepairStatus.APPLIED)

    executor = DispatchRepairExecutorAdapter({COMMAND_RECLAIM: apply})

    assert executor.execute(command) == RepairResult(
        command=command,
        status=RepairStatus.APPLIED,
    )
    assert seen == [command]


def test_dispatch_adapter_fails_closed_for_unknown_or_unbound_commands() -> None:
    executor = DispatchRepairExecutorAdapter({})
    unknown = _repair_command("execution.unrecognized")
    unbound = _repair_command(COMMAND_BOOKKEEPING)

    unknown_result = executor.execute(unknown)
    unbound_result = executor.execute(unbound)

    assert unknown_result.status is RepairStatus.FAILED
    assert unknown_result.diagnostics == (
        "unsupported repair command: execution.unrecognized",
    )
    assert unbound_result.status is RepairStatus.FAILED
    assert unbound_result.diagnostics == (
        "unbound recovery repair command: execution.update-bookkeeping",
    )


def test_dispatch_adapter_normalizes_handler_failure_and_wrong_result() -> None:
    failed_command = _repair_command(COMMAND_REQUEUE)
    mismatched_command = _repair_command(COMMAND_RECLAIM)

    def fail(_command: RepairCommand) -> RepairResult:
        raise RuntimeError("provider unavailable")

    def mismatch(_command: RepairCommand) -> RepairResult:
        return RepairResult(
            command=_repair_command(COMMAND_RECLAIM, suffix=":other"),
            status=RepairStatus.APPLIED,
        )

    executor = DispatchRepairExecutorAdapter(
        {
            COMMAND_REQUEUE: fail,
            COMMAND_RECLAIM: mismatch,
        }
    )

    failed = executor.execute(failed_command)
    mismatched = executor.execute(mismatched_command)

    assert failed.status is RepairStatus.FAILED
    assert failed.diagnostics == ("RuntimeError: provider unavailable",)
    assert mismatched.status is RepairStatus.FAILED
    assert mismatched.diagnostics == (
        "repair handler returned a result for another command",
    )


def test_dead_local_is_reclaimed_but_cloud_pid_none_is_not(tmp_path, fake_forge):
    local = _active(tmp_path, 707, pid=707)
    cloud = _active(tmp_path, 708, pid=None, external_id="cloud-708")
    run_state = RunState(active_worktrees={"707": local, "708": cloud})
    config = _config(
        tmp_path,
        fake_forge,
        dispatch_target=_ExternalTarget(status="running"),
    )

    with (
        patch(
            "orchestune.dispatch.execution_repair.is_process_alive",
            return_value=False,
        ),
        patch.object(fake_forge, "branch_exists", return_value=True),
    ):
        evaluation = evaluate_execution_repair_plan(
            run_state,
            {707: _task(707), 708: _task(708)},
            config,
            now=2_000.0,
        )

    assert LOCAL_PROCESS_DEAD in _codes(evaluation)
    assert _command_codes(evaluation, 707) == [COMMAND_RECLAIM, COMMAND_REQUEUE]
    assert _command_codes(evaluation, 708) == []


def test_handleless_local_is_deferred_but_cloud_pid_none_is_not(tmp_path, fake_forge):
    local = _active(tmp_path, 707, pid=None)
    cloud = _active(tmp_path, 708, pid=None, external_id="cloud-708")
    config = _config(
        tmp_path,
        fake_forge,
        dispatch_target=_ExternalTarget(status="running"),
    )

    with patch.object(fake_forge, "branch_exists", return_value=True):
        evaluation = evaluate_execution_repair_plan(
            RunState(active_worktrees={"707": local, "708": cloud}),
            {707: _task(707), 708: _task(708)},
            config,
            now=2_000.0,
        )

    assert _command_codes(evaluation, 707) == []
    assert _command_codes(evaluation, 708) == []


def test_elapsed_timeout_is_a_kernel_finding_and_typed_plan(tmp_path, fake_forge):
    active = _active(tmp_path, 707, pid=707, started_at=1_000.0)
    config = _config(tmp_path, fake_forge, task_timeout_seconds=60)

    with (
        patch(
            "orchestune.dispatch.execution_repair.is_process_alive",
            return_value=True,
        ),
        patch.object(fake_forge, "branch_exists", return_value=True),
    ):
        evaluation = evaluate_execution_repair_plan(
            RunState(active_worktrees={"707": active}),
            {707: _task(707)},
            config,
            now=2_000.0,
        )

    assert EXECUTION_TIMED_OUT in _codes(evaluation)
    assert _command_codes(evaluation, 707) == [COMMAND_RECLAIM, COMMAND_REQUEUE]


def test_provider_unknown_blocks_same_cycle_timeout_reclaim(tmp_path, fake_forge):
    active = _active(
        tmp_path,
        707,
        pid=None,
        external_id="cloud-707",
        started_at=1_000.0,
    )
    config = _config(
        tmp_path,
        fake_forge,
        task_timeout_seconds=60,
        dispatch_target=_ExternalTarget(error=RuntimeError("temporary provider error")),
    )

    with patch.object(fake_forge, "branch_exists", return_value=True):
        evaluation = evaluate_execution_repair_plan(
            RunState(active_worktrees={"707": active}),
            {707: _task(707)},
            config,
            now=2_000.0,
        )

    assert EXECUTION_TIMED_OUT in _codes(evaluation)
    assert EXECUTION_OBSERVATION_UNKNOWN in _codes(evaluation)
    assert _command_codes(evaluation, 707) == []


def test_missing_and_stale_run_state_use_bookkeeping_commands(tmp_path, fake_forge):
    stale = _active(tmp_path, 708, pid=708)
    run_state = RunState(active_worktrees={"708": stale})
    tasks = {
        707: _task(707, "status:in-progress"),
        708: _task(708, "status:queued"),
    }
    config = _config(tmp_path, fake_forge)

    with (
        patch(
            "orchestune.dispatch.execution_repair.is_process_alive",
            return_value=True,
        ),
        patch.object(fake_forge, "branch_exists", return_value=True),
    ):
        evaluation = evaluate_execution_repair_plan(
            run_state,
            tasks,
            config,
            now=2_000.0,
        )

    assert RUN_STATE_MISSING in _codes(evaluation)
    assert RUN_STATE_STALE in _codes(evaluation)
    assert _command_codes(evaluation, 707) == [COMMAND_REQUEUE, COMMAND_BOOKKEEPING]
    assert _command_codes(evaluation, 708) == [COMMAND_RECLAIM, COMMAND_BOOKKEEPING]


def test_completion_hold_filters_destructive_commands(tmp_path, fake_forge):
    active = _active(tmp_path, 707, pid=707)
    config = _config(tmp_path, fake_forge)

    with (
        patch(
            "orchestune.dispatch.execution_repair.is_process_alive",
            return_value=False,
        ),
        patch.object(fake_forge, "branch_exists", return_value=True),
    ):
        evaluation = evaluate_execution_repair_plan(
            RunState(active_worktrees={"707": active}),
            {707: _task(707)},
            config,
            held_issue_numbers={707},
            now=2_000.0,
        )

    assert LOCAL_PROCESS_DEAD in _codes(evaluation)
    assert _command_codes(evaluation, 707) == []


def test_gc_reobserves_and_defers_when_process_state_changes(tmp_path, fake_forge):
    active = _active(tmp_path, 707, pid=707, started_at=None)
    run_state = RunState(active_worktrees={"707": active})
    config = _config(tmp_path, fake_forge, apply=True)

    with (
        patch(
            "orchestune.dispatch.execution_repair.is_process_alive",
            side_effect=(False, True),
        ) as process_probe,
        patch.object(fake_forge, "branch_exists", return_value=True),
        patch("orchestune.dispatch.gc.zombies.time.time", return_value=2_000.0),
    ):
        events = _collect_zombies_and_timeouts(
            run_state,
            {707: _task(707)},
            config,
        )

    assert process_probe.call_count == 2
    assert events == []
    assert run_state.active_worktrees == {"707": active}
    fake_forge.remove_label.assert_not_called()
    fake_forge.add_label.assert_not_called()


def test_gc_reobserves_only_the_reclaim_candidate(tmp_path, fake_forge):
    dead = _active(tmp_path, 707, pid=707, started_at=None)
    alive = _active(tmp_path, 708, pid=708, started_at=None)
    run_state = RunState(active_worktrees={"707": dead, "708": alive})
    config = _config(tmp_path, fake_forge, apply=False)

    with (
        patch(
            "orchestune.dispatch.execution_repair.is_process_alive",
            side_effect=lambda pid: pid == 708,
        ) as process_probe,
        patch("orchestune.dispatch.gc.zombies.time.time", return_value=2_000.0),
    ):
        events = _collect_zombies_and_timeouts(
            run_state,
            {707: _task(707), 708: _task(708)},
            config,
        )

    assert [call.args for call in process_probe.call_args_list] == [
        (707,),
        (708,),
        (707,),
    ]
    assert [event["issue_number"] for event in events] == [707]
