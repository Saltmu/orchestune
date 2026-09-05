"""Final Supervisor repair ownership rollout contracts (#746)."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from orchestune.consistency.invariants.status import (
    BLOCKED_WITH_RESOLVED_DEPENDENCIES,
    PRIMARY_STATUS_CONFLICT,
)
from orchestune.consistency.models import (
    ConsistencyReport,
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
from orchestune.consistency.supervisor import (
    ConsistencyCycleReport,
    ConsistencyMode,
    ConsistencyRepairOutcome,
    ConsistencyRepairPass,
    ConsistencyScanResult,
    RepairDisposition,
    ScanKind,
)
from orchestune.dispatch.config import (
    DEFAULT_SELF_HEALING_REPAIR_ALLOWLIST,
    DispatcherConfig,
)
from orchestune.dispatch.cycle import (
    _DispatchRepairExecutor,
    _merge_consistency_reports,
    _RepairCycleState,
    run_dispatch_cycle,
)
from tests.conftest import make_issue

pytestmark = pytest.mark.e2e


def _outcome(disposition: RepairDisposition) -> ConsistencyRepairOutcome:
    return ConsistencyRepairOutcome(
        finding_code="execution.local-process-dead",
        scope=ConsistencyScope.TASK,
        subject_id="746",
        disposition=disposition,
        diagnostics=(disposition.value,),
    )


def test_default_self_healing_allowlist_is_stable_and_separate(tmp_path) -> None:
    assert DEFAULT_SELF_HEALING_REPAIR_ALLOWLIST == frozenset(
        {
            BLOCKED_WITH_RESOLVED_DEPENDENCIES,
            PRIMARY_STATUS_CONFLICT,
            COMMAND_BOOKKEEPING,
            COMMAND_RECLAIM,
            COMMAND_REQUEUE,
        }
    )
    config = DispatcherConfig(
        run_state_path=tmp_path / "state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=tmp_path / "worktrees",
    )
    assert config.consistency_repair_allowlist == frozenset()


def test_unbound_execution_command_fails_closed_without_phase_owned_skip(
    tmp_path,
) -> None:
    command = RepairCommand(
        code=COMMAND_RECLAIM,
        scope=ConsistencyScope.TASK,
        subject_id="746",
        idempotency_key="execution:746:reclaim",
        parameters=(("finding_codes", ("execution.local-process-dead",)),),
    )
    executor = _DispatchRepairExecutor(
        config=DispatcherConfig(
            run_state_path=tmp_path / "state.json",
            events_log_path=tmp_path / "events.jsonl",
            worktree_root=tmp_path / "worktrees",
        ),
        adapter=Mock(),
        completed_issue_numbers=frozenset(),
    )

    result = executor.execute(command)

    assert result.status is RepairStatus.FAILED
    assert result.diagnostics == ("unbound gc repair command: execution.reclaim",)
    assert all(
        "repair remains owned by its existing execution phase" not in diagnostic
        for diagnostic in result.diagnostics
    )


def test_cycle_claims_only_commands_attempted_by_a_builtin_boundary() -> None:
    finding_code = "execution.local-process-dead"
    reclaim = RepairCommand(
        code=COMMAND_RECLAIM,
        scope=ConsistencyScope.TASK,
        subject_id="746",
        idempotency_key="execution:746:reclaim",
        parameters=(("finding_codes", (finding_code,)),),
    )
    unattempted_requeue = RepairCommand(
        code=COMMAND_REQUEUE,
        scope=ConsistencyScope.TASK,
        subject_id="746",
        idempotency_key="execution:746:requeue",
        parameters=(("finding_codes", (finding_code,)),),
    )
    report = ConsistencyCycleReport(
        mode=ConsistencyMode.REPAIR,
        scans=(
            ConsistencyScanResult(
                boundary="gc-reclaim",
                kind=ScanKind.FULL,
                report=ConsistencyReport(repository_id="owner/repo"),
                repair_candidates=(reclaim, unattempted_requeue),
            ),
        ),
        repair_passes=(
            ConsistencyRepairPass(
                number=1,
                results=(RepairResult(command=reclaim, status=RepairStatus.SKIPPED),),
            ),
        ),
    )
    cycle_state = _RepairCycleState()

    cycle_state.add_report(report)

    assert cycle_state.claimed_repair_codes == {COMMAND_RECLAIM, finding_code}
    assert COMMAND_REQUEUE not in cycle_state.claimed_repair_codes


def test_user_allowlisted_execution_requeue_uses_a_bound_handler(
    tmp_path, in_memory_forge
) -> None:
    issue = make_issue(
        746,
        labels=("status:in-progress",),
        subtask_id="supervisor-rollout",
        parent=None,
    )
    in_memory_forge.seed_issue(issue)
    config = DispatcherConfig(
        apply=True,
        consistency_mode=ConsistencyMode.REPAIR,
        consistency_repair_allowlist=frozenset({COMMAND_REQUEUE}),
        max_concurrent=0,
        run_state_path=tmp_path / "state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=tmp_path / "worktrees",
        forge=in_memory_forge,
    )

    # Isolate the optional repository-wide loop from the built-in startup
    # boundary so this test proves that the former has its own real handler.
    with patch(
        "orchestune.dispatch.cycle._run_recovery_bookkeeping_boundary",
        return_value=ConsistencyCycleReport(mode=ConsistencyMode.REPAIR),
    ):
        report = run_dispatch_cycle(config)

    requeues = [
        result
        for repair_pass in report.consistency.repair_passes
        for result in repair_pass.results
        if result.command.code == COMMAND_REQUEUE
    ]
    assert [result.status for result in requeues] == [RepairStatus.APPLIED]
    assert in_memory_forge.get_issue_labels(746) == ("status:queued",)
    assert all(
        "unbound execution repair command" not in diagnostic
        for result in requeues
        for diagnostic in result.diagnostics
    )


def test_no_apply_off_mode_reports_default_status_repair_as_deferred(
    tmp_path, fake_forge
) -> None:
    issue = make_issue(
        746,
        labels=("status:queued", "status:done"),
        parent=None,
    )
    fake_forge.list_issues_by_label.side_effect = lambda label, *args, **kwargs: (
        [issue] if label in issue.labels else []
    )
    fake_forge.list_open_prs.return_value = []
    config = DispatcherConfig(
        apply=False,
        consistency_mode=ConsistencyMode.OFF,
        max_concurrent=0,
        run_state_path=tmp_path / "state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=tmp_path / "worktrees",
        forge=fake_forge,
    )

    report = run_dispatch_cycle(config)

    conflict = next(
        outcome
        for outcome in report.consistency.repair_outcomes
        if outcome.finding_code == PRIMARY_STATUS_CONFLICT
    )
    assert report.consistency.mode is ConsistencyMode.OFF
    assert conflict.disposition is RepairDisposition.DEFERRED
    assert report.consistency.repair_passes == ()
    fake_forge.add_label.assert_not_called()
    fake_forge.remove_label.assert_not_called()


def test_cycle_aggregation_keeps_a_failed_repair_attempt_sticky() -> None:
    main = ConsistencyCycleReport(
        mode=ConsistencyMode.REPAIR,
        repair_outcomes=(_outcome(RepairDisposition.FAILED),),
    )
    resolved_boundary = ConsistencyCycleReport(
        mode=ConsistencyMode.REPAIR,
        repair_outcomes=(_outcome(RepairDisposition.RESOLVED),),
    )

    merged = _merge_consistency_reports(main, [resolved_boundary])

    assert merged.repair_outcomes == (
        ConsistencyRepairOutcome(
            finding_code="execution.local-process-dead",
            scope=ConsistencyScope.TASK,
            subject_id="746",
            disposition=RepairDisposition.FAILED,
            diagnostics=("resolved", "failed"),
        ),
    )
