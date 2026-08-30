"""Guarded consistency repair across the dispatch-cycle boundary (#709)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from orchestune.consistency.intents import IntentJournal
from orchestune.consistency.invariants.status import PRIMARY_STATUS_CONFLICT
from orchestune.consistency.supervisor import (
    ConsistencyMode,
    RepairDisposition,
    consistency_cycle_to_dict,
)
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import run_dispatch_cycle
from orchestune.dispatch.cycle_context import IssuesByStatus
from orchestune.dispatch.cycle_report import CycleReport
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.state import RunState
from orchestune.dispatch.status_repair import status_intent_journal_path
from tests.conftest import make_issue, make_task


def _pipeline_report() -> CycleReport:
    return CycleReport(
        selected=[],
        quota_slots_available=1,
        lock_changes={"to_lock": [], "to_unlock": []},
        deviation_events=[],
        completion_events=[],
        promotion_events=[],
        applied=True,
    )


def test_repair_mode_applies_simultaneous_allowlisted_repairs_and_reobserves(
    tmp_path, fake_forge
) -> None:
    labels_by_issue = {
        709: ["status:queued", "status:done"],
        710: ["status:queued", "status:done"],
    }

    def current_issue(issue_number: int):
        return make_issue(
            issue_number,
            labels=tuple(labels_by_issue[issue_number]),
            parent=None,
        )

    def list_issues(label: str, *args, **kwargs):
        return [
            current_issue(issue_number)
            for issue_number, labels in labels_by_issue.items()
            if label in labels
        ]

    def remove_label(issue_number: int, label: str) -> None:
        labels_by_issue[issue_number].remove(label)

    issues_by_number = {
        issue_number: current_issue(issue_number) for issue_number in labels_by_issue
    }
    tasks = {
        issue_number: make_task(
            issue_number,
            subtask_id=f"guarded-repair-{issue_number}",
            status_labels=tuple(labels),
        )
        for issue_number, labels in labels_by_issue.items()
    }
    run_state = RunState()
    config = DispatcherConfig(
        apply=True,
        consistency_mode=ConsistencyMode.REPAIR,
        consistency_repair_allowlist=frozenset({PRIMARY_STATUS_CONFLICT}),
        consistency_max_repair_passes=2,
        run_state_path=tmp_path / "state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=tmp_path / "worktrees",
    )
    issues = IssuesByStatus(
        queued=list(issues_by_number.values()),
        locked=[],
        in_progress=[],
        blocked=[],
        done=list(issues_by_number.values()),
        not_needed=[],
    )
    ctx = CycleContext(
        run_state=run_state,
        tasks_by_issue=tasks,
        issue_number_by_subtask_id={
            task.subtask_id: issue_number for issue_number, task in tasks.items()
        },
        done_subtask_ids=set(),
        ci_passed_pr_subtask_ids=set(),
        changes_requested_subtask_ids=set(),
        subtask_branch_map={
            task.subtask_id: f"codex/issue-{issue_number}"
            for issue_number, task in tasks.items()
        },
        prs=[],
        pr_by_branch={},
        config=config,
    )
    fake_forge.list_issues_by_label.side_effect = list_issues
    fake_forge.list_open_prs.return_value = []
    fake_forge.get_issue_state.return_value = "OPEN"
    fake_forge.get_issue_labels.side_effect = lambda issue_number: tuple(
        labels_by_issue[issue_number]
    )
    fake_forge.remove_label.side_effect = remove_label

    with (
        patch("orchestune.dispatch.cycle.load_run_state", return_value=run_state),
        patch("orchestune.dispatch.cycle._prepare_cycle_issues", return_value=issues),
        patch("orchestune.dispatch.cycle._build_cycle_context", return_value=ctx),
        patch(
            "orchestune.dispatch.cycle._execute_cycle_pipeline",
            return_value=_pipeline_report(),
        ),
    ):
        report = run_dispatch_cycle(config)

    assert all(not scan.diagnostics for scan in report.consistency.scans), [
        scan.diagnostics for scan in report.consistency.scans
    ]
    assert all(
        labels == ["status:queued"] for labels in labels_by_issue.values()
    ), consistency_cycle_to_dict(report.consistency)
    assert report.consistency.mode is ConsistencyMode.REPAIR
    assert len(report.consistency.repair_passes) == 1
    matching = [
        outcome
        for outcome in report.consistency.repair_outcomes
        if outcome.finding_code == PRIMARY_STATUS_CONFLICT
    ]
    assert len(matching) == 2
    assert all(
        outcome.disposition is RepairDisposition.RESOLVED for outcome in matching
    )
    assert [scan.boundary for scan in report.consistency.scans] == [
        "start",
        "end",
        "repair-1",
    ]


def test_repair_mode_with_empty_allowlist_remains_report_only(tmp_path, fake_forge):
    issue = make_issue(709, labels=("status:queued", "status:done"), parent=None)
    fake_forge.list_issues_by_label.side_effect = lambda label, *args, **kwargs: (
        [issue] if label in issue.labels else []
    )
    fake_forge.list_open_prs.return_value = []
    config = DispatcherConfig(
        apply=False,
        consistency_mode=ConsistencyMode.REPAIR,
        run_state_path=tmp_path / "state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=tmp_path / "worktrees",
    )

    report = run_dispatch_cycle(config)

    assert report.consistency.mode is ConsistencyMode.REPAIR
    assert report.consistency.repair_passes == ()
    conflict = next(
        outcome
        for outcome in report.consistency.repair_outcomes
        if outcome.finding_code == PRIMARY_STATUS_CONFLICT
    )
    assert conflict.disposition is RepairDisposition.DEFERRED
    assert any(
        outcome.disposition is RepairDisposition.OBSERVATION_UNKNOWN
        for outcome in report.consistency.repair_outcomes
    )
    fake_forge.add_label.assert_not_called()
    fake_forge.remove_label.assert_not_called()


def test_repair_failure_is_reported_and_intent_remains_resumable(tmp_path, fake_forge):
    issue = make_issue(709, labels=("status:queued", "status:done"), parent=None)
    task = make_task(709, status_labels=issue.labels)
    run_state = RunState()
    config = DispatcherConfig(
        apply=True,
        consistency_mode=ConsistencyMode.REPAIR,
        consistency_repair_allowlist=frozenset({PRIMARY_STATUS_CONFLICT}),
        run_state_path=tmp_path / "state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=tmp_path / "worktrees",
    )
    issues = IssuesByStatus(
        queued=[issue],
        locked=[],
        in_progress=[],
        blocked=[],
        done=[issue],
        not_needed=[],
    )
    ctx = CycleContext(
        run_state=run_state,
        tasks_by_issue={709: task},
        issue_number_by_subtask_id={task.subtask_id: 709},
        done_subtask_ids=set(),
        ci_passed_pr_subtask_ids=set(),
        changes_requested_subtask_ids=set(),
        subtask_branch_map={task.subtask_id: "codex/issue-709"},
        prs=[],
        pr_by_branch={},
        config=config,
    )
    fake_forge.list_issues_by_label.side_effect = lambda label, *args, **kwargs: (
        [issue] if label in issue.labels else []
    )
    fake_forge.list_open_prs.return_value = []
    fake_forge.get_issue_state.return_value = "OPEN"
    fake_forge.get_issue_labels.return_value = issue.labels
    fake_forge.remove_label.side_effect = RuntimeError("Forge unavailable")

    with (
        patch("orchestune.dispatch.cycle.load_run_state", return_value=run_state),
        patch("orchestune.dispatch.cycle._prepare_cycle_issues", return_value=issues),
        patch("orchestune.dispatch.cycle._build_cycle_context", return_value=ctx),
        patch(
            "orchestune.dispatch.cycle._execute_cycle_pipeline",
            return_value=_pipeline_report(),
        ),
    ):
        report = run_dispatch_cycle(config)

    outcome = next(
        item
        for item in report.consistency.repair_outcomes
        if item.finding_code == PRIMARY_STATUS_CONFLICT
    )
    assert outcome.disposition is RepairDisposition.FAILED
    assert "Forge unavailable" in outcome.diagnostics[0]
    assert (
        len(
            IntentJournal(status_intent_journal_path(config)).pending(
                now=datetime.now(UTC)
            )
        )
        == 1
    )
