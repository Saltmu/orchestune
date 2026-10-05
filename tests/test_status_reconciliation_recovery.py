"""Safety and recovery of real reconciliation (#1218)."""

from dataclasses import replace
from datetime import timedelta
from itertools import combinations
from unittest.mock import patch

import pytest

from orchestune.consistency.desired import TaskLifecycle
from orchestune.consistency.intents import IntentJournal
from orchestune.consistency.invariants.status import (
    PRIMARY_STATUS_CONFLICT,
    REPOSITORY_POLICY_INVARIANT,
)
from orchestune.consistency.models import (
    ConsistencyFinding,
    ConsistencyReport,
    ConsistencyScope,
    Evidence,
    FindingSeverity,
    ObservationCertainty,
    Repairability,
    RepairStatus,
)
from orchestune.consistency.repairs.status import plan_status_repairs
from orchestune.dispatch.status_repair import (
    _new_intent,
    execute_status_repair_command,
)
from orchestune.ledger.run_state import RunState, save_run_state
from tests.conftest import make_issue, make_task
from tests.status_reconciliation_test_support import (
    NOW,
    ReconciliationLoop,
    recovery_oracle,
)
from tests.test_consistency_status_repair import _config, _evidence
from tests.test_consistency_status_repair import _plan as _executor_plan
from tests.test_consistency_status_repairs import _plan
from tests.test_status_reconciliation_stateful import LIFECYCLE, assert_recovery


@pytest.mark.parametrize(
    "labels,lifecycle",
    [
        (("status:done", "status:not-needed"), TaskLifecycle.DONE),
        (("status:done", "status:queued", "status:blocked"), TaskLifecycle.OPEN),
        (("status:not-needed", "status:queued"), TaskLifecycle.OPEN),
        (("status:blocked-human-review", "status:queued"), TaskLifecycle.OPEN),
        (("ci:base-branch-red",), TaskLifecycle.OPEN),
        (("status:blocked", "status:queued", "ci:base-branch-red"), TaskLifecycle.OPEN),
        (
            ("status:done", "status:queued", "status:blocked-recompute"),
            TaskLifecycle.OPEN,
        ),
    ],
)
def test_protected_and_held_cardinality_has_no_plan(labels, lifecycle):
    assert _plan(labels, lifecycle=lifecycle) == ()


@pytest.mark.parametrize(
    "protected",
    [
        "status:done",
        "status:not-needed",
        "status:blocked-human-review",
        "status:manual-merge-required",
    ],
)
@pytest.mark.parametrize(
    "initial,completed",
    [(("status:blocked",), ("dep",)), (("status:blocked", "status:queued"), ("dep",))],
)
def test_fresh_protection_stops_old_transition_and_remove(
    tmp_path, in_memory_forge, protected, initial, completed
):
    task = make_task(708, status_labels=initial, depends_on=("dep",))
    dep = make_task(709, subtask_id="dep", status_labels=("status:done",))
    tasks = {708: task, 709: dep}
    _, commands = _executor_plan(tasks, completed_subtask_ids=completed)
    command = next(c for c in commands if c.subject_id == "708")
    labels = (*initial, protected)
    in_memory_forge.seed_issue(make_issue(708, labels=labels, depends_on=("dep",)))
    in_memory_forge.seed_issue(
        make_issue(709, labels=dep.status_labels, subtask_id="dep")
    )
    result = execute_status_repair_command(
        command,
        tasks,
        completion_evidence=_evidence((709,)),
        config=_config(tmp_path, in_memory_forge),
    )
    assert result.status is RepairStatus.SKIPPED
    assert in_memory_forge.get_issue_labels(708) == labels


@pytest.mark.parametrize(
    "initial,op,mode",
    [
        ((), "add", "before"),
        ((), "add", "after"),
        (("status:blocked",), "add", "before"),
        (("status:blocked",), "add", "after"),
        (("status:blocked",), "remove", "before"),
        (("status:blocked",), "remove", "after"),
        (("status:blocked", "status:queued", "status:in-progress"), "remove", "before"),
        (("status:blocked", "status:queued", "status:in-progress"), "remove", "after"),
    ],
)
def test_partial_api_failure_restarts_into_one_cycle_recovery(
    tmp_path, in_memory_forge, initial, op, mode
):
    loop = ReconciliationLoop(tmp_path, in_memory_forge)
    loop.relabel(initial)
    loop.boundary.armed = op, mode
    loop.cycle()
    persisted = loop.journal().load()
    assert persisted
    loop.restart()
    assert loop.journal().load() == persisted
    assert_recovery(loop)


@pytest.mark.parametrize("boundary", ["planned", "applied", "verified"])
def test_stop_at_journal_boundary_retains_intent_and_recovers(
    tmp_path, in_memory_forge, boundary
):
    loop = ReconciliationLoop(tmp_path, in_memory_forge)
    loop.relabel(("status:blocked",))
    method = {
        "planned": "plan",
        "applied": "mark_applied",
        "verified": "mark_verified",
    }[boundary]
    original = getattr(IntentJournal, method)

    def stop_after_save(journal, *args, **kwargs):
        original(journal, *args, **kwargs)
        raise OSError("stopped after durable journal boundary")

    with patch.object(IntentJournal, method, stop_after_save):
        _, _, report = loop.cycle()
    assert any(
        r.status is RepairStatus.FAILED for p in report.repair_passes for r in p.results
    )
    assert loop.journal().load()
    loop.restart()
    assert_recovery(loop)


def test_same_key_is_not_reexecuted_in_passes_but_fresh_cycle_retries(
    tmp_path, in_memory_forge
):
    loop = ReconciliationLoop(tmp_path, in_memory_forge)
    loop.relabel(())
    loop.boundary.armed = "add", "before"
    _, _, report = loop.cycle(max_passes=5)
    results = [r for p in report.repair_passes for r in p.results]
    assert len(results) == 1 and results[0].status is RepairStatus.FAILED
    assert_recovery(loop)


@pytest.mark.parametrize(
    "mode",
    ["undeclared", "apply-false", "allowlist", "hold", "unknown", "closed", "absent"],
)
def test_deferred_intervals_are_not_claimed_as_converged(
    tmp_path, in_memory_forge, mode
):
    loop = ReconciliationLoop(tmp_path, in_memory_forge)
    loop.relabel(("status:blocked",))
    if mode == "undeclared":
        loop.declared = False
        loop.relabel(("status:blocked",))
    elif mode == "apply-false":
        loop.config.apply = False
    elif mode == "allowlist":
        loop.allowlist = ()
    elif mode == "hold":
        loop.relabel(("status:blocked", "ci:base-branch-red"))
    elif mode == "unknown":
        loop.forge_known = False
    elif mode == "closed":
        loop.relabel(("status:blocked",), state="CLOSED")
    elif mode == "absent":
        loop.forge.issues.pop(708)
    before = loop.labels()
    for _ in range(3):
        initial, final, report = loop.cycle()
        assert loop.labels() == before
        assert not loop.boundary.history
        if mode == "undeclared":
            assert initial.report.findings and final.repair_candidates
            assert all(
                r.status is RepairStatus.SKIPPED
                for p in report.repair_passes
                for r in p.results
            )
        if mode in {"unknown", "hold"}:
            assert final.report.findings and not final.repair_candidates


@pytest.mark.parametrize(
    "labels", [tuple(c) for size in range(8) for c in combinations(LIFECYCLE, size)]
)
@pytest.mark.parametrize("resolved", [True, False])
def test_complete_lifecycle_case_table(tmp_path, labels, resolved):
    loop = ReconciliationLoop(tmp_path)
    loop.resolved = resolved
    loop.relabel((*labels, "ordinary", "status:future"))
    target = recovery_oracle(loop.labels(), resolved)
    if target is not None:
        assert_recovery(loop)
    else:
        before = loop.labels()
        _, final, _ = loop.cycle()
        assert loop.labels() == before
        assert not loop.boundary.history
        assert final.report.findings


@pytest.mark.parametrize("certainty", tuple(ObservationCertainty)[1:])
@pytest.mark.parametrize(
    "shape", ["valid", "missing", "duplicate", "malformed", "ambiguous"]
)
def test_task_uncertainty_only_defers_that_task(tmp_path, certainty, shape):
    loop = ReconciliationLoop(tmp_path)
    loop.relabel(())
    loop.resolved = False  # peer queued has unresolved dependencies below
    loop.certainty, loop.fact_shape = certainty, shape
    original = loop.observe
    loop.forge.seed_issue(make_issue(710, labels=()))

    def observe_two():
        snapshot = original()
        loop.tasks[710] = make_task(710, status_labels=loop.forge.get_issue_labels(710))
        return snapshot

    loop.observe = observe_two
    _, final, _ = loop.cycle()
    assert any(
        f.code == "status.observation-unknown" and f.subject_id == "708"
        for f in final.report.findings
    )
    assert not [c for c in final.repair_candidates if c.subject_id == "708"]
    assert any(m[1] == 710 for m in loop.boundary.history)


@pytest.mark.parametrize(
    "change", ["dependency-reopened", "closed", "retained-gone", "hold"]
)
def test_stale_command_rechecks_fresh_state(tmp_path, change):
    loop = ReconciliationLoop(tmp_path)
    loop.relabel(
        ("status:blocked",)
        if change != "retained-gone"
        else ("status:blocked", "status:queued")
    )
    loop.observe()
    report = loop.supervisor.full_scan("before-change", observer=loop, deriver=loop)
    command = next(c for c in report.repair_candidates if c.subject_id == "708")
    if change == "dependency-reopened":
        loop.resolved = False
        loop.sync_tasks()
    elif change == "closed":
        loop.relabel(tuple(loop.labels()), state="CLOSED")
    elif change == "retained-gone":
        loop.relabel(("status:blocked",))
    else:
        loop.relabel((*loop.labels(), "ci:base-branch-red"))
    before = loop.labels()
    result = loop.execute(command)
    assert result.status is RepairStatus.SKIPPED
    assert loop.labels() == before and not loop.boundary.history
    assert not loop.journal().load()


@pytest.mark.parametrize(
    "pending", ["matching", "conflicting", "expired", "reservation"]
)
def test_persisted_intent_and_completion_reservation_intervals(tmp_path, pending):
    loop = ReconciliationLoop(tmp_path)
    loop.relabel(())
    initial = loop.supervisor.full_scan("intent", observer=loop, deriver=loop)
    command = next(c for c in initial.repair_candidates if c.subject_id == "708")
    if pending == "reservation":
        save_run_state(
            RunState(
                completion_reservations={
                    "repo::708": {"issue_number": 708, "stage": "reserved"}
                }
            ),
            loop.config.run_state_path,
        )
    else:
        intent = _new_intent(command, NOW)
        if pending == "conflicting":
            intent = replace(
                intent,
                expected_changes=(
                    replace(intent.expected_changes[0], value="status:blocked"),
                ),
            )
        if pending == "expired":
            intent = replace(
                intent,
                created_at=NOW - timedelta(seconds=2),
                expires_at=NOW - timedelta(seconds=1),
            )
        loop.journal().plan(intent)
    if pending in {"matching", "expired"}:
        assert_recovery(loop)
    else:
        _, final, report = loop.cycle()
        assert final.repair_candidates and not loop.boundary.history
        assert all(
            r.status is RepairStatus.SKIPPED
            for p in report.repair_passes
            for r in p.results
        )


def test_stop_after_mutation_before_verification(tmp_path):
    loop = ReconciliationLoop(tmp_path)
    loop.relabel(("status:blocked",))
    with patch(
        "orchestune.dispatch.status_repair._verified_status_labels",
        side_effect=OSError("verification stopped"),
    ):
        loop.cycle()
    assert loop.labels() == {"status:queued"}
    assert loop.journal().pending(now=NOW)
    loop.restart()
    assert_recovery(loop)


@pytest.mark.parametrize(
    "labels,keep",
    [
        (("status:done", "status:not-needed"), "status:done"),
        (("status:not-needed", "status:queued"), "status:queued"),
        (("status:done", "status:queued", "status:blocked"), "status:queued"),
        (("status:blocked-human-review", "status:queued"), "status:queued"),
    ],
)
def test_handwritten_automatic_report_cannot_remove_protection(labels, keep):
    finding = ConsistencyFinding(
        code=PRIMARY_STATUS_CONFLICT,
        scope=ConsistencyScope.TASK,
        subject_id="708",
        severity=FindingSeverity.ERROR,
        expected=Evidence("keep", value=keep),
        observed=Evidence("labels", value=labels),
        repairability=Repairability.AUTOMATIC,
    )
    report = ConsistencyReport(
        repository_id="test",
        findings=(finding,),
        evaluated_invariants=(REPOSITORY_POLICY_INVARIANT,),
    )
    assert plan_status_repairs(report) == ()


@pytest.mark.parametrize(
    "initial",
    [(), ("status:blocked", "status:queued"), ("status:done", "status:queued")],
)
def test_stale_queued_initialization_or_retention_cannot_bypass_hold(tmp_path, initial):
    loop = ReconciliationLoop(tmp_path)
    loop.relabel(initial)
    scan = loop.supervisor.full_scan("hold", observer=loop, deriver=loop)
    command = next(c for c in scan.repair_candidates if c.subject_id == "708")
    assert "no-promotion-hold" in command.preconditions
    # Even older/manual commands without the new precondition are guarded.
    command = replace(
        command,
        preconditions=tuple(
            p for p in command.preconditions if p != "no-promotion-hold"
        ),
    )
    loop.relabel((*initial, "ci:base-branch-red"))
    before = loop.labels()
    assert loop.execute(command).status is RepairStatus.SKIPPED
    assert loop.labels() == before and not loop.journal().load()


def test_pending_transition_cannot_resume_after_external_final_label(tmp_path):
    loop = ReconciliationLoop(tmp_path)
    loop.relabel(("status:blocked",))
    loop.boundary.armed = "add", "before"
    loop.cycle()
    assert loop.journal().pending(now=NOW)
    loop.relabel(("status:blocked", "status:done"))
    loop.restart()
    # Replay only here, to test a stale command; normal cycle always replans.
    command = next(
        c
        for c in _executor_plan(
            {
                708: make_task(
                    708, status_labels=("status:blocked",), depends_on=("dep",)
                ),
                709: make_task(709, subtask_id="dep", status_labels=("status:done",)),
            },
            completed_subtask_ids=("dep",),
        )[1]
        if c.subject_id == "708"
    )
    before = loop.labels()
    result = loop.execute(command)
    assert result.status is RepairStatus.SKIPPED and result.diagnostics
    assert loop.labels() == before and not loop.boundary.history
    assert len(loop.journal().load()) == 1


@pytest.mark.parametrize(
    "labels,finding",
    [
        (("status:in-progress",), None),
        (("status:done",), "status.done-with-active-execution"),
    ],
)
def test_execution_evidence_is_observed_and_derived_from_fixture(
    tmp_path, labels, finding
):
    loop = ReconciliationLoop(tmp_path)
    loop.execution = True
    loop.relabel(labels)
    _, final, _ = loop.cycle()
    codes = [f.code for f in final.report.findings if f.subject_id == "708"]
    assert codes == ([] if finding is None else [finding])
    assert not final.repair_candidates and not loop.boundary.history
