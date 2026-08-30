"""Dispatch-side execution of consistency status repair plans (#708)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from orchestune.consistency.intents import IntentJournal
from orchestune.consistency.invariants.status import (
    BLOCKED_WITH_RESOLVED_DEPENDENCIES,
    PRIMARY_STATUS_CONFLICT,
)
from orchestune.consistency.models import IntentStatus
from orchestune.consistency.repairs.status import (
    COMMAND_REMOVE_LABEL,
    COMMAND_TRANSITION_LABEL,
)
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.status_repair import (
    StatusRepairPhase,
    evaluate_status_repair_plan,
    reconcile_status_repairs,
    status_intent_journal_path,
)
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


def test_blocked_promotion_is_selected_from_kernel_finding_and_typed_plan():
    task = make_task(
        708,
        subtask_id="status-migration",
        status_labels=("status:blocked",),
        depends_on=("shadow-supervisor",),
    )

    evaluation = evaluate_status_repair_plan(
        {708: task},
        completed_subtask_ids={"shadow-supervisor"},
        now=NOW,
    )

    assert [finding.code for finding in evaluation.report.findings] == [
        BLOCKED_WITH_RESOLVED_DEPENDENCIES
    ]
    assert [command.code for command in evaluation.commands] == [
        COMMAND_TRANSITION_LABEL
    ]
    assert _finding_code(evaluation.commands[0]) == (BLOCKED_WITH_RESOLVED_DEPENDENCIES)


def test_dual_status_recovery_is_selected_from_kernel_finding_and_typed_plan():
    task = make_task(
        708,
        subtask_id="status-migration",
        status_labels=("status:done", "status:queued"),
    )

    evaluation = evaluate_status_repair_plan({708: task}, now=NOW)

    assert [finding.code for finding in evaluation.report.findings] == [
        PRIMARY_STATUS_CONFLICT
    ]
    assert [command.code for command in evaluation.commands] == [COMMAND_REMOVE_LABEL]
    assert dict(evaluation.commands[0].parameters)["label"] == "status:done"


def test_transition_intent_is_persisted_before_forge_mutation_and_verified(
    tmp_path, in_memory_forge
):
    task = make_task(
        708,
        subtask_id="status-migration",
        status_labels=("status:blocked",),
        depends_on=("shadow-supervisor",),
    )
    in_memory_forge.seed_issue(
        make_issue(
            708,
            subtask_id="status-migration",
            labels=("status:blocked",),
            depends_on=("shadow-supervisor",),
        )
    )
    config = _config(tmp_path, in_memory_forge)
    journal = IntentJournal(status_intent_journal_path(config))
    calls: list[str] = []
    original_add = in_memory_forge.add_label
    original_remove = in_memory_forge.remove_label

    def add_label(issue_number, label):
        pending = journal.pending(now=NOW)
        assert len(pending) == 1
        assert pending[0].status is IntentStatus.PLANNED
        calls.append(f"add:{label}")
        original_add(issue_number, label)

    def remove_label(issue_number, label):
        pending = journal.pending(now=NOW)
        assert len(pending) == 1
        assert pending[0].status is IntentStatus.APPLIED
        calls.append(f"remove:{label}")
        original_remove(issue_number, label)

    in_memory_forge.add_label = add_label
    in_memory_forge.remove_label = remove_label

    repaired = reconcile_status_repairs(
        {708: task},
        completed_subtask_ids={"shadow-supervisor"},
        config=config,
        phase=StatusRepairPhase.BLOCKED_PROMOTION,
        now=NOW,
    )

    assert repaired == (task,)
    assert calls == ["add:status:queued", "remove:status:blocked"]
    (intent,) = journal.load()
    assert intent.status is IntentStatus.VERIFIED
    assert in_memory_forge.get_issue_labels(708) == ("status:queued",)


def test_partial_transition_resumes_the_live_intent_without_new_conflicting_plan(
    tmp_path, in_memory_forge
):
    task = make_task(
        708,
        subtask_id="status-migration",
        status_labels=("status:blocked",),
        depends_on=("shadow-supervisor",),
    )
    in_memory_forge.seed_issue(
        make_issue(
            708,
            subtask_id="status-migration",
            labels=("status:blocked",),
            depends_on=("shadow-supervisor",),
        )
    )
    config = _config(tmp_path, in_memory_forge)
    original_remove = in_memory_forge.remove_label
    failed_once = False

    def fail_first_remove(issue_number, label):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("partial Forge failure")
        original_remove(issue_number, label)

    in_memory_forge.remove_label = fail_first_remove

    with pytest.raises(RuntimeError, match="partial Forge failure"):
        reconcile_status_repairs(
            {708: task},
            completed_subtask_ids={"shadow-supervisor"},
            config=config,
            phase=StatusRepairPhase.BLOCKED_PROMOTION,
            now=NOW,
        )

    journal = IntentJournal(status_intent_journal_path(config))
    (interrupted,) = journal.load()
    assert interrupted.status is IntentStatus.APPLIED
    assert set(in_memory_forge.get_issue_labels(708)) == {
        "status:blocked",
        "status:queued",
    }

    resumed_task = replace(
        task,
        status_labels=("status:blocked", "status:queued"),
    )
    repaired = reconcile_status_repairs(
        {708: resumed_task},
        completed_subtask_ids={"shadow-supervisor"},
        config=config,
        phase=StatusRepairPhase.BLOCKED_PROMOTION,
        now=NOW,
    )

    assert repaired == (resumed_task,)
    assert len(journal.load()) == 1
    assert journal.load()[0].status is IntentStatus.VERIFIED
    assert in_memory_forge.get_issue_labels(708) == ("status:queued",)


def test_fresh_hold_precondition_defers_stale_promotion_without_writing_intent(
    tmp_path, in_memory_forge
):
    task = make_task(
        708,
        subtask_id="status-migration",
        status_labels=("status:blocked",),
        depends_on=("shadow-supervisor",),
    )
    in_memory_forge.seed_issue(
        make_issue(
            708,
            subtask_id="status-migration",
            labels=("status:blocked", "ci:base-branch-red"),
            depends_on=("shadow-supervisor",),
        )
    )
    config = _config(tmp_path, in_memory_forge)

    repaired = reconcile_status_repairs(
        {708: task},
        completed_subtask_ids={"shadow-supervisor"},
        config=config,
        phase=StatusRepairPhase.BLOCKED_PROMOTION,
        now=NOW,
    )

    assert repaired == ()
    assert not status_intent_journal_path(config).exists()
    assert set(in_memory_forge.get_issue_labels(708)) == {
        "status:blocked",
        "ci:base-branch-red",
    }


def test_fresh_dependency_precondition_defers_when_a_dependency_reopened(
    tmp_path, in_memory_forge
):
    dependency = make_task(
        706,
        subtask_id="shadow-supervisor",
        status_labels=("status:done",),
    )
    task = make_task(
        708,
        subtask_id="status-migration",
        status_labels=("status:blocked",),
        depends_on=("shadow-supervisor",),
    )
    in_memory_forge.seed_issue(
        make_issue(
            706,
            subtask_id="shadow-supervisor",
            labels=("status:queued",),
        )
    )
    in_memory_forge.seed_issue(
        make_issue(
            708,
            subtask_id="status-migration",
            labels=("status:blocked",),
            depends_on=("shadow-supervisor",),
        )
    )
    config = _config(tmp_path, in_memory_forge)

    repaired = reconcile_status_repairs(
        {706: dependency, 708: task},
        completed_subtask_ids={"shadow-supervisor"},
        config=config,
        phase=StatusRepairPhase.BLOCKED_PROMOTION,
        now=NOW,
    )

    assert repaired == ()
    assert not status_intent_journal_path(config).exists()
    assert in_memory_forge.get_issue_labels(708) == ("status:blocked",)


def test_intent_write_failure_prevents_the_first_forge_mutation(
    tmp_path, in_memory_forge
):
    task = make_task(
        708,
        subtask_id="status-migration",
        status_labels=("status:done", "status:queued"),
    )
    in_memory_forge.seed_issue(
        make_issue(
            708,
            subtask_id="status-migration",
            labels=("status:done", "status:queued"),
        )
    )
    config = _config(tmp_path, in_memory_forge)

    with (
        patch.object(IntentJournal, "plan", side_effect=OSError("journal full")),
        pytest.raises(OSError, match="journal full"),
    ):
        reconcile_status_repairs(
            {708: task},
            completed_subtask_ids=set(),
            config=config,
            phase=StatusRepairPhase.DUAL_STATUS,
            now=NOW,
        )

    assert set(in_memory_forge.get_issue_labels(708)) == {
        "status:done",
        "status:queued",
    }


def test_first_forge_failure_keeps_a_planned_restartable_intent(
    tmp_path, in_memory_forge
):
    task = make_task(
        708,
        subtask_id="status-migration",
        status_labels=("status:blocked",),
        depends_on=("shadow-supervisor",),
    )
    in_memory_forge.seed_issue(
        make_issue(
            708,
            subtask_id="status-migration",
            labels=("status:blocked",),
            depends_on=("shadow-supervisor",),
        )
    )
    config = _config(tmp_path, in_memory_forge)

    with (
        patch.object(
            in_memory_forge, "add_label", side_effect=RuntimeError("Forge down")
        ),
        pytest.raises(RuntimeError, match="Forge down"),
    ):
        reconcile_status_repairs(
            {708: task},
            completed_subtask_ids={"shadow-supervisor"},
            config=config,
            phase=StatusRepairPhase.BLOCKED_PROMOTION,
            now=NOW,
        )

    (intent,) = IntentJournal(status_intent_journal_path(config)).load()
    assert intent.status is IntentStatus.PLANNED
    assert in_memory_forge.get_issue_labels(708) == ("status:blocked",)


def test_applied_marker_failure_leaves_the_intent_and_intermediate_labels(
    tmp_path, in_memory_forge
):
    task = make_task(
        708,
        subtask_id="status-migration",
        status_labels=("status:blocked",),
        depends_on=("shadow-supervisor",),
    )
    in_memory_forge.seed_issue(
        make_issue(
            708,
            subtask_id="status-migration",
            labels=("status:blocked",),
            depends_on=("shadow-supervisor",),
        )
    )
    config = _config(tmp_path, in_memory_forge)

    with (
        patch.object(
            IntentJournal, "mark_applied", side_effect=OSError("journal full")
        ),
        pytest.raises(OSError, match="journal full"),
    ):
        reconcile_status_repairs(
            {708: task},
            completed_subtask_ids={"shadow-supervisor"},
            config=config,
            phase=StatusRepairPhase.BLOCKED_PROMOTION,
            now=NOW,
        )

    (intent,) = IntentJournal(status_intent_journal_path(config)).load()
    assert intent.status is IntentStatus.PLANNED
    assert set(in_memory_forge.get_issue_labels(708)) == {
        "status:blocked",
        "status:queued",
    }


def test_post_apply_observation_failure_is_verified_on_restart(
    tmp_path, in_memory_forge
):
    task = make_task(
        708,
        subtask_id="status-migration",
        status_labels=("status:done", "status:queued"),
    )
    in_memory_forge.seed_issue(
        make_issue(
            708,
            subtask_id="status-migration",
            labels=("status:done", "status:queued"),
        )
    )
    config = _config(tmp_path, in_memory_forge)
    original_get_labels = in_memory_forge.get_issue_labels
    observations = 0

    def fail_verification(issue_number):
        nonlocal observations
        observations += 1
        if observations == 2:
            raise RuntimeError("verification unavailable")
        return original_get_labels(issue_number)

    with (
        patch.object(in_memory_forge, "get_issue_labels", fail_verification),
        pytest.raises(RuntimeError, match="verification unavailable"),
    ):
        reconcile_status_repairs(
            {708: task},
            completed_subtask_ids=set(),
            config=config,
            phase=StatusRepairPhase.DUAL_STATUS,
            now=NOW,
        )

    journal = IntentJournal(status_intent_journal_path(config))
    assert journal.load()[0].status is IntentStatus.APPLIED
    assert in_memory_forge.get_issue_labels(708) == ("status:queued",)

    resumed = replace(task, status_labels=("status:queued",))
    assert reconcile_status_repairs(
        {708: resumed},
        completed_subtask_ids=set(),
        config=config,
        phase=StatusRepairPhase.DUAL_STATUS,
        now=NOW,
    ) == (resumed,)
    assert journal.load()[0].status is IntentStatus.VERIFIED


def test_dry_run_preserves_event_candidate_without_writing_intent(
    tmp_path, in_memory_forge
):
    task = make_task(
        708,
        subtask_id="status-migration",
        status_labels=("status:done", "status:queued"),
    )
    config = _config(tmp_path, in_memory_forge, apply=False)

    repaired = reconcile_status_repairs(
        {708: task},
        completed_subtask_ids=set(),
        config=config,
        phase=StatusRepairPhase.DUAL_STATUS,
        now=NOW,
    )

    assert repaired == (task,)
    assert not status_intent_journal_path(config).exists()
