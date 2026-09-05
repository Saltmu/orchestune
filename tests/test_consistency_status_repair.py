"""Typed dispatch-side status repair execution through Supervisor commands."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch

import orchestune.dispatch.phase_reconciliation as phase_reconciliation
import orchestune.dispatch.reconciliation as reconciliation
import orchestune.dispatch.status_repair as status_repair
from orchestune.consistency.desired import (
    DesiredTaskInput,
    DispatchPolicy,
    TaskLifecycle,
    derive_desired_repository_state,
)
from orchestune.consistency.engine import ConsistencyEngine
from orchestune.consistency.intents import IntentJournal
from orchestune.consistency.invariants.status import (
    BLOCKED_WITH_RESOLVED_DEPENDENCIES,
    PRIMARY_STATUS_CONFLICT,
    status_invariants,
)
from orchestune.consistency.models import (
    ConsistencyScope,
    IntentStatus,
    RepairCommand,
    RepairStatus,
)
from orchestune.consistency.observation import ForgeSnapshot, ObservationCollector
from orchestune.consistency.repairs.execution import COMMAND_RECLAIM
from orchestune.consistency.repairs.status import (
    COMMAND_REMOVE_LABEL,
    COMMAND_TRANSITION_LABEL,
    plan_status_repairs,
)
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.status_repair import (
    execute_status_repair_command,
    status_intent_journal_path,
    task_lifecycle,
)
from orchestune.models import Task
from tests.conftest import make_issue, make_task

NOW = datetime(2026, 8, 30, 0, 0, tzinfo=UTC)


def _config(tmp_path, forge, *, apply=True) -> DispatcherConfig:
    return DispatcherConfig(
        run_state_path=tmp_path / "run_state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=tmp_path / "worktrees",
        forge=forge,
        apply=apply,
    )


def _finding_code(command) -> object:
    return dict(command.parameters).get("finding_code")


def _shadow_dependency() -> Task:
    """`shadow-supervisor`という名前で参照される、完了済みの依存元タスク。

    #799: `execute_status_repair_command`のfresh依存解決は、渡された
    `tasks_by_issue`から実際に`(parent_number, subtask_id)`で引けるタスクしか
    解決しないため、実在しないsubtask_idを`depends_on`が指すテストでは
    このタスクを`tasks_by_issue`へ含める必要がある。
    """
    return make_task(
        709, subtask_id="shadow-supervisor", status_labels=("status:done",)
    )


def _plan(tasks_by_issue, *, completed_subtask_ids=(), intents=()):
    completed = frozenset(completed_subtask_ids)
    tasks = tuple(
        DesiredTaskInput(
            task_id=task.subtask_id,
            subject_id=str(task.issue_number),
            depends_on=task.depends_on,
            lifecycle=task_lifecycle(
                task.status_labels,
                completed=task.subtask_id in completed,
            ),
        )
        for task in tasks_by_issue.values()
        if task.subtask_id
    )
    observed = ObservationCollector(
        repository_id="test-repository", clock=lambda: NOW
    ).collect(
        forge=ForgeSnapshot(
            issues=tuple(
                make_issue(
                    task.issue_number,
                    labels=task.status_labels,
                    subtask_id=task.subtask_id,
                    depends_on=task.depends_on,
                    parent=None,
                )
                for task in tasks_by_issue.values()
            ),
            fetched_at=NOW,
        )
    )
    desired = derive_desired_repository_state(
        "test-repository",
        tasks,
        completed_task_ids=completed,
        policy=DispatchPolicy(max_concurrent=max(1, len(tasks))),
        intents=intents,
        now=NOW,
    )
    report = ConsistencyEngine(status_invariants()).evaluate(observed, desired)
    return report, plan_status_repairs(report)


def test_legacy_status_repair_entrypoints_are_removed():
    assert not hasattr(status_repair, "StatusRepairPhase")
    assert not hasattr(status_repair, "reconcile_status_repairs")
    assert not hasattr(reconciliation, "_promote_blocked_tasks")
    assert not hasattr(reconciliation, "_reconcile_dual_status_tasks")
    assert not hasattr(phase_reconciliation, "run_blocked_promotion_phase")
    assert not hasattr(phase_reconciliation, "run_dual_status_reconciliation")


def test_closed_loop_handler_fails_closed_for_non_status_command(
    tmp_path, in_memory_forge
):
    command = RepairCommand(
        code=COMMAND_RECLAIM,
        scope=ConsistencyScope.TASK,
        subject_id="708",
        idempotency_key="test:wrong-domain",
    )

    result = execute_status_repair_command(
        command,
        {},
        completed_issue_numbers=(),
        config=_config(tmp_path, in_memory_forge),
        now=NOW,
    )

    assert result.status is RepairStatus.FAILED
    assert result.diagnostics == (
        "unsupported status repair command: execution.reclaim",
    )


def test_task_lifecycle_uses_one_shared_completion_override():
    dual_status = ("status:done", "status:queued")

    assert task_lifecycle(dual_status) is TaskLifecycle.OPEN
    assert task_lifecycle(dual_status, completed=True) is TaskLifecycle.DONE


def test_kernel_plans_blocked_promotion_as_typed_transition():
    task = make_task(
        708,
        subtask_id="status-migration",
        status_labels=("status:blocked",),
        depends_on=("shadow-supervisor",),
    )

    report, commands = _plan({708: task}, completed_subtask_ids={"shadow-supervisor"})

    assert [finding.code for finding in report.findings] == [
        BLOCKED_WITH_RESOLVED_DEPENDENCIES
    ]
    assert [command.code for command in commands] == [COMMAND_TRANSITION_LABEL]
    assert _finding_code(commands[0]) == BLOCKED_WITH_RESOLVED_DEPENDENCIES


def test_kernel_plans_dual_status_as_typed_removal():
    task = make_task(
        708,
        subtask_id="status-migration",
        status_labels=("status:done", "status:queued"),
    )

    report, commands = _plan({708: task})

    assert [finding.code for finding in report.findings] == [PRIMARY_STATUS_CONFLICT]
    assert [command.code for command in commands] == [COMMAND_REMOVE_LABEL]
    assert dict(commands[0].parameters)["label"] == "status:done"


def test_transition_intent_precedes_forge_mutation_and_is_verified(
    tmp_path, in_memory_forge
):
    task = make_task(
        708,
        subtask_id="status-migration",
        status_labels=("status:blocked",),
        depends_on=("shadow-supervisor",),
    )
    dependency = _shadow_dependency()
    in_memory_forge.seed_issue(
        make_issue(708, labels=task.status_labels, subtask_id=task.subtask_id)
    )
    in_memory_forge.seed_issue(
        make_issue(
            709, labels=dependency.status_labels, subtask_id=dependency.subtask_id
        )
    )
    tasks_by_issue = {708: task, 709: dependency}
    command = _plan(tasks_by_issue, completed_subtask_ids={"shadow-supervisor"})[1][0]
    config = _config(tmp_path, in_memory_forge)
    journal = IntentJournal(status_intent_journal_path(config))
    calls = []
    original_add = in_memory_forge.add_label
    original_remove = in_memory_forge.remove_label

    def add_label(issue_number, label):
        assert journal.pending(now=NOW)[0].status is IntentStatus.PLANNED
        calls.append(f"add:{label}")
        original_add(issue_number, label)

    def remove_label(issue_number, label):
        assert journal.pending(now=NOW)[0].status is IntentStatus.APPLIED
        calls.append(f"remove:{label}")
        original_remove(issue_number, label)

    in_memory_forge.add_label = add_label
    in_memory_forge.remove_label = remove_label
    result = execute_status_repair_command(
        command,
        tasks_by_issue,
        completed_issue_numbers={709},
        config=config,
        now=NOW,
    )

    assert result.status is RepairStatus.APPLIED
    assert calls == ["add:status:queued", "remove:status:blocked"]
    assert journal.load()[0].status is IntentStatus.VERIFIED


def test_partial_transition_resumes_with_the_followup_typed_command(
    tmp_path, in_memory_forge
):
    task = make_task(
        708,
        subtask_id="status-migration",
        status_labels=("status:blocked",),
        depends_on=("shadow-supervisor",),
    )
    dependency = _shadow_dependency()
    in_memory_forge.seed_issue(
        make_issue(708, labels=task.status_labels, subtask_id=task.subtask_id)
    )
    in_memory_forge.seed_issue(
        make_issue(
            709, labels=dependency.status_labels, subtask_id=dependency.subtask_id
        )
    )
    tasks_by_issue = {708: task, 709: dependency}
    transition = _plan(tasks_by_issue, completed_subtask_ids={"shadow-supervisor"})[1][
        0
    ]
    config = _config(tmp_path, in_memory_forge)
    original_remove = in_memory_forge.remove_label
    in_memory_forge.remove_label = lambda *_: (_ for _ in ()).throw(
        RuntimeError("partial Forge failure")
    )

    failed = execute_status_repair_command(
        transition,
        tasks_by_issue,
        completed_issue_numbers={709},
        config=config,
        now=NOW,
    )
    assert failed.status is RepairStatus.FAILED
    assert set(in_memory_forge.get_issue_labels(708)) == {
        "status:blocked",
        "status:queued",
    }

    in_memory_forge.remove_label = original_remove
    partial_task = replace(task, status_labels=("status:blocked", "status:queued"))
    partial_tasks_by_issue = {708: partial_task, 709: dependency}
    followup = _plan(
        partial_tasks_by_issue, completed_subtask_ids={"shadow-supervisor"}
    )[1][0]
    resumed = execute_status_repair_command(
        followup,
        partial_tasks_by_issue,
        completed_issue_numbers={709},
        config=config,
        now=NOW,
    )

    assert resumed.status is RepairStatus.APPLIED
    journal = IntentJournal(status_intent_journal_path(config))
    assert len(journal.load()) == 1
    assert journal.load()[0].status is IntentStatus.VERIFIED
    assert in_memory_forge.get_issue_labels(708) == ("status:queued",)


def test_conflicting_live_intent_defers_a_different_transition(
    tmp_path, in_memory_forge
):
    dual = make_task(708, status_labels=("status:done", "status:queued"))
    in_memory_forge.seed_issue(make_issue(708, labels=dual.status_labels))
    config = _config(tmp_path, in_memory_forge)
    remove_done = _plan({708: dual})[1][0]
    with patch.object(
        in_memory_forge, "remove_label", side_effect=RuntimeError("Forge down")
    ):
        failed = execute_status_repair_command(
            remove_done, {708: dual}, completed_issue_numbers=(), config=config, now=NOW
        )
    assert failed.status is RepairStatus.FAILED

    blocked = replace(
        dual,
        status_labels=("status:blocked",),
        depends_on=("shadow-supervisor",),
    )
    in_memory_forge.seed_issue(make_issue(708, labels=blocked.status_labels))
    transition = _plan({708: blocked}, completed_subtask_ids={"shadow-supervisor"})[1][
        0
    ]
    # #799: `shadow-supervisor`は`tasks_by_issue`に実在しないため未解決だが、
    # このケースは「別の生きたstatus遷移がこのsubjectを覆っている」判定で
    # 依存解決チェックへ到達する前にSKIPPEDになるため、依存を解決できる
    # 必要はない。
    deferred = execute_status_repair_command(
        transition,
        {708: blocked},
        completed_issue_numbers=(),
        config=config,
        now=NOW,
    )

    assert deferred.status is RepairStatus.SKIPPED
    assert deferred.diagnostics == (
        "another live status transition covers this subject",
    )
    assert in_memory_forge.get_issue_labels(708) == ("status:blocked",)


def test_fresh_hold_precondition_defers_stale_promotion(tmp_path, in_memory_forge):
    task = make_task(
        708,
        status_labels=("status:blocked",),
        depends_on=("shadow-supervisor",),
    )
    in_memory_forge.seed_issue(
        make_issue(708, labels=("status:blocked", "ci:base-branch-red"))
    )
    command = _plan({708: task}, completed_subtask_ids={"shadow-supervisor"})[1][0]
    config = _config(tmp_path, in_memory_forge)

    # #799: `shadow-supervisor`は未解決になるが、この検証の主眼である
    # `no-promotion-hold`（ci:base-branch-red）の失敗はそれとは独立に
    # 全体をSKIPPEDにするため、依存を解決できる必要はない。
    result = execute_status_repair_command(
        command,
        {708: task},
        completed_issue_numbers=(),
        config=config,
        now=NOW,
    )

    assert result.status is RepairStatus.SKIPPED
    assert not status_intent_journal_path(config).exists()


def test_fresh_dependency_precondition_defers_reopened_dependency(
    tmp_path, in_memory_forge
):
    dependency = make_task(706, subtask_id="dep", status_labels=("status:done",))
    task = make_task(
        708,
        status_labels=("status:blocked",),
        depends_on=("dep",),
    )
    in_memory_forge.seed_issue(make_issue(706, labels=("status:queued",)))
    in_memory_forge.seed_issue(make_issue(708, labels=task.status_labels))
    command = _plan({706: dependency, 708: task})[1][0]
    config = _config(tmp_path, in_memory_forge)

    result = execute_status_repair_command(
        command,
        {706: dependency, 708: task},
        completed_issue_numbers={706},
        config=config,
        now=NOW,
    )

    assert result.status is RepairStatus.SKIPPED
    assert not status_intent_journal_path(config).exists()


def test_intent_write_failure_prevents_first_forge_mutation(tmp_path, in_memory_forge):
    task = make_task(708, status_labels=("status:done", "status:queued"))
    in_memory_forge.seed_issue(make_issue(708, labels=task.status_labels))
    command = _plan({708: task})[1][0]
    config = _config(tmp_path, in_memory_forge)

    with patch.object(IntentJournal, "plan", side_effect=OSError("journal full")):
        result = execute_status_repair_command(
            command, {708: task}, completed_issue_numbers=(), config=config, now=NOW
        )

    assert result.status is RepairStatus.FAILED
    assert "journal full" in result.diagnostics[0]
    assert set(in_memory_forge.get_issue_labels(708)) == {
        "status:done",
        "status:queued",
    }


def test_first_forge_failure_keeps_planned_restartable_intent(
    tmp_path, in_memory_forge
):
    dependency = make_task(706, subtask_id="dep", status_labels=("status:done",))
    task = make_task(
        708,
        status_labels=("status:blocked",),
        depends_on=("dep",),
    )
    in_memory_forge.seed_issue(make_issue(708, labels=task.status_labels))
    in_memory_forge.seed_issue(
        make_issue(
            706, labels=dependency.status_labels, subtask_id=dependency.subtask_id
        )
    )
    tasks_by_issue = {706: dependency, 708: task}
    command = _plan(tasks_by_issue, completed_subtask_ids={"dep"})[1][0]
    config = _config(tmp_path, in_memory_forge)

    with patch.object(
        in_memory_forge, "add_label", side_effect=RuntimeError("Forge down")
    ):
        result = execute_status_repair_command(
            command,
            tasks_by_issue,
            completed_issue_numbers={706},
            config=config,
            now=NOW,
        )

    assert result.status is RepairStatus.FAILED
    assert IntentJournal(status_intent_journal_path(config)).load()[0].status is (
        IntentStatus.PLANNED
    )


def test_dry_run_skips_mutation_and_intent(tmp_path, in_memory_forge):
    task = make_task(708, status_labels=("status:done", "status:queued"))
    command = _plan({708: task})[1][0]
    config = _config(tmp_path, in_memory_forge, apply=False)

    result = execute_status_repair_command(
        command, {708: task}, completed_issue_numbers=(), config=config, now=NOW
    )

    assert result.status is RepairStatus.SKIPPED
    assert not status_intent_journal_path(config).exists()
    assert in_memory_forge.get_issue_labels(708) == ()
