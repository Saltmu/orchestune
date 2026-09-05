"""Guarded consistency repair across the dispatch-cycle boundary (#709)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from orchestune.consistency.intents import IntentJournal
from orchestune.consistency.invariants.status import (
    BLOCKED_WITH_RESOLVED_DEPENDENCIES,
    PRIMARY_STATUS_CONFLICT,
    QUEUED_WITH_UNRESOLVED_DEPENDENCIES,
)
from orchestune.consistency.models import IntentStatus, RepairStatus
from orchestune.consistency.repairs.execution import (
    COMMAND_BOOKKEEPING,
    COMMAND_RECLAIM,
    COMMAND_REQUEUE,
)
from orchestune.consistency.supervisor import (
    ConsistencyMode,
    RepairDisposition,
    consistency_cycle_to_dict,
)
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import (
    _run_recovery_bookkeeping_boundary,
    run_dispatch_cycle,
)
from orchestune.dispatch.cycle_context import IssuesByStatus
from orchestune.dispatch.cycle_report import CycleReport
from orchestune.dispatch.locks import ExternalLockScanResult
from orchestune.dispatch.phase_gc import run_gc_phase
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.state import ActiveWorktree, RunState, load_run_state
from orchestune.dispatch.status_repair import status_intent_journal_path
from tests.conftest import make_issue, make_pr, make_task

pytestmark = pytest.mark.e2e


def _repair_command_codes(report) -> list[str]:
    return [
        result.command.code
        for repair_pass in report.repair_passes
        for result in repair_pass.results
    ]


def test_recovery_boundary_restores_missing_run_state_and_reobserves(
    tmp_path, in_memory_forge
) -> None:
    worktree_root = tmp_path / "worktrees"
    restored_worktree = worktree_root / "claude-issue-744-recovery-cutover"
    restored_worktree.mkdir(parents=True)
    issue = make_issue(
        744,
        labels=("status:in-progress",),
        subtask_id="recovery-cutover",
        parent=None,
    )
    in_memory_forge.seed_issue(issue)
    in_memory_forge.seed_pr(
        make_pr(
            755,
            head_ref="claude/issue-744-recovery-cutover",
            closes_issue_numbers=(744,),
        )
    )
    run_state = RunState()
    config = DispatcherConfig(
        apply=True,
        max_concurrent=0,
        run_state_path=tmp_path / "run_state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=worktree_root,
        forge=in_memory_forge,
    )

    report = _run_recovery_bookkeeping_boundary(run_state, config, now=1_000.0)

    assert _repair_command_codes(report) == [COMMAND_REQUEUE, COMMAND_BOOKKEEPING]
    assert report.repair_passes[0].results[0].status is RepairStatus.SKIPPED
    assert report.repair_passes[0].results[1].status is RepairStatus.APPLIED
    assert [scan.boundary for scan in report.scans] == [
        "recovery-bookkeeping",
        "repair-1",
    ]
    assert run_state.active_worktrees["744"].worktree_path == str(restored_worktree)
    assert (
        load_run_state(config.run_state_path).active_worktrees["744"]
        == (run_state.active_worktrees["744"])
    )


def test_recovery_boundary_requeues_missing_execution_without_restorable_resource(
    tmp_path, in_memory_forge
) -> None:
    issue = make_issue(
        744,
        labels=("status:in-progress",),
        subtask_id="recovery-cutover",
        parent=None,
    )
    in_memory_forge.seed_issue(issue)
    run_state = RunState()
    config = DispatcherConfig(
        apply=True,
        max_concurrent=0,
        run_state_path=tmp_path / "run_state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=tmp_path / "worktrees",
        forge=in_memory_forge,
    )

    first = _run_recovery_bookkeeping_boundary(run_state, config, now=1_000.0)
    second = _run_recovery_bookkeeping_boundary(run_state, config, now=1_001.0)

    assert _repair_command_codes(first) == [COMMAND_REQUEUE, COMMAND_BOOKKEEPING]
    assert first.repair_passes[0].results[0].status is RepairStatus.APPLIED
    assert first.repair_passes[0].results[1].status is RepairStatus.SKIPPED
    assert in_memory_forge.get_issue_labels(744) == ("status:queued",)
    assert run_state.active_worktrees == {}
    assert second.repair_passes == ()


def test_recovery_bookkeeping_is_monotonic_and_idempotent_after_restart(
    tmp_path, in_memory_forge
) -> None:
    now = datetime.now(UTC).timestamp()
    parent = make_issue(
        741,
        body=(
            "<!-- orchestune:launch-history -->\n"
            f"```yaml\nlaunch_history:\n- {now - 60}\n- {now - 60}\n```\n"
        ),
        labels=(),
        parent=None,
    )
    child = make_issue(
        744,
        body=(
            "```yaml\nsubtask_id: recovery-cutover\nrecompute_count: 3\n"
            "forced_serial: true\n```\n"
        ),
        labels=("status:in-progress", "status:force-serial"),
        parent={"number": 741},
    )
    in_memory_forge.seed_issue(parent)
    in_memory_forge.seed_issue(child)
    active = ActiveWorktree(
        issue_number=744,
        branch="codex/issue-744-recovery-cutover",
        worktree_path=str(tmp_path / "worktrees" / "issue-744"),
        pid=744,
        started_at=now - 120,
        declared_footprint=(),
        recompute_count=1,
        forced_serial=False,
    )
    run_state = RunState(
        active_worktrees={"744": active},
        launch_history=[now - 60],
    )
    config = DispatcherConfig(
        apply=True,
        parent_issue_number=741,
        window_seconds=3_600,
        run_state_path=tmp_path / "run_state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=tmp_path / "worktrees",
        forge=in_memory_forge,
    )

    with patch(
        "orchestune.dispatch.execution_repair.is_process_alive", return_value=True
    ):
        first = _run_recovery_bookkeeping_boundary(run_state, config, now=now)
        restarted = load_run_state(config.run_state_path)
        second = _run_recovery_bookkeeping_boundary(restarted, config, now=now + 1)

    assert _repair_command_codes(first) == [COMMAND_BOOKKEEPING, COMMAND_BOOKKEEPING]
    assert restarted.active_worktrees["744"].recompute_count == 3
    assert restarted.active_worktrees["744"].forced_serial is True
    assert restarted.launch_history == [now - 60, now - 60]
    assert second.repair_passes == ()


def test_recovery_counters_use_repository_wide_in_progress_snapshot(
    tmp_path, in_memory_forge
) -> None:
    """A parent-scoped cycle must still repair the repository-shared run state."""
    issue = make_issue(
        745,
        body=(
            "```yaml\nsubtask_id: recovery-other-parent\nrecompute_count: 4\n"
            "forced_serial: true\n```\n"
        ),
        labels=("status:in-progress", "status:force-serial"),
        parent={"number": 999},
    )
    in_memory_forge.seed_issue(issue)
    active = ActiveWorktree(
        issue_number=745,
        branch="codex/issue-745-recovery-other-parent",
        worktree_path=str(tmp_path / "worktrees" / "issue-745"),
        pid=745,
        started_at=900.0,
        declared_footprint=(),
        recompute_count=1,
        forced_serial=False,
    )
    run_state = RunState(active_worktrees={"745": active})
    config = DispatcherConfig(
        apply=True,
        parent_issue_number=741,
        run_state_path=tmp_path / "run_state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=tmp_path / "worktrees",
        forge=in_memory_forge,
    )

    with patch(
        "orchestune.dispatch.execution_repair.is_process_alive", return_value=True
    ):
        report = _run_recovery_bookkeeping_boundary(run_state, config, now=1_000.0)

    assert _repair_command_codes(report) == [COMMAND_BOOKKEEPING]
    assert run_state.active_worktrees["745"].recompute_count == 4
    assert run_state.active_worktrees["745"].forced_serial is True
    persisted = load_run_state(config.run_state_path).active_worktrees["745"]
    assert persisted.recompute_count == 4
    assert persisted.forced_serial is True


def test_recovery_launch_history_updates_preview_without_persisting(
    tmp_path, in_memory_forge
) -> None:
    now = datetime.now(UTC).timestamp()
    in_memory_forge.seed_issue(
        make_issue(
            741,
            body=(
                "<!-- orchestune:launch-history -->\n"
                f"```yaml\nlaunch_history:\n- {now - 60}\n```\n"
            ),
            labels=(),
            parent=None,
        )
    )
    run_state = RunState()
    config = DispatcherConfig(
        apply=False,
        parent_issue_number=741,
        window_seconds=3_600,
        run_state_path=tmp_path / "run_state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=tmp_path / "worktrees",
        forge=in_memory_forge,
    )

    report = _run_recovery_bookkeeping_boundary(run_state, config, now=now)

    assert _repair_command_codes(report) == [COMMAND_BOOKKEEPING]
    assert report.repair_passes[0].results[0].status is RepairStatus.APPLIED
    assert run_state.launch_history == [now - 60]
    assert not config.run_state_path.exists()


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


def test_gc_reclaim_runs_as_a_supervisor_typed_repair(tmp_path, fake_forge) -> None:
    active = ActiveWorktree(
        issue_number=745,
        branch="codex/issue-745-gc-reclaim",
        worktree_path=str(tmp_path / "missing-worktree"),
        pid=745,
        started_at=None,
        declared_footprint=(),
    )
    run_state = RunState(active_worktrees={"745": active})
    task = make_task(
        745,
        subtask_id="gc-reclaim",
        status_labels=("status:in-progress",),
    )
    config = DispatcherConfig(
        apply=True,
        zombie_gc=True,
        run_state_path=tmp_path / "state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=tmp_path / "worktrees",
        forge=fake_forge,
    )

    with patch(
        "orchestune.dispatch.execution_repair.is_process_alive",
        side_effect=(False, False),
    ):
        outcome = run_gc_phase(run_state, {745: task}, config, [], open_prs=[])

    results = tuple(
        result
        for repair_pass in outcome.consistency.repair_passes
        for result in repair_pass.results
    )
    assert [(result.command.code, result.status) for result in results] == [
        (COMMAND_RECLAIM, RepairStatus.APPLIED)
    ]
    assert [event["action"] for event in outcome.completion_events] == ["gc_reclaimed"]
    assert run_state.active_worktrees == {}


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
        dependency_resolution={},
        done_issue_numbers=set(),
        ci_passed_pr_issue_numbers=set(),
        changes_requested_issue_numbers=set(),
        branch_by_issue_number={
            issue_number: f"codex/issue-{issue_number}"
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
        dependency_resolution={},
        done_issue_numbers=set(),
        ci_passed_pr_issue_numbers=set(),
        changes_requested_issue_numbers=set(),
        branch_by_issue_number={709: "codex/issue-709"},
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


def test_cycle_resumes_partial_forge_failure_once_on_the_next_cycle(
    tmp_path, fake_forge
):
    labels = {
        742: ["status:done"],
        743: ["status:blocked"],
    }
    remove_attempts = 0

    def current_issue(issue_number):
        return make_issue(
            issue_number,
            labels=tuple(labels[issue_number]),
            subtask_id="adapter-foundation"
            if issue_number == 742
            else "status-cutover",
            depends_on=() if issue_number == 742 else ("adapter-foundation",),
        )

    def list_issues(label, *args, **kwargs):
        return [
            current_issue(issue_number)
            for issue_number in labels
            if label in labels[issue_number]
        ]

    def add_label(issue_number, label):
        if label not in labels[issue_number]:
            labels[issue_number].append(label)

    def remove_label(issue_number, label):
        nonlocal remove_attempts
        if issue_number == 743 and label == "status:blocked":
            remove_attempts += 1
        if remove_attempts == 1 and label == "status:blocked":
            raise RuntimeError("partial Forge failure")
        labels[issue_number].remove(label)

    fake_forge.list_issues_by_label.side_effect = list_issues
    fake_forge.list_open_prs.return_value = []
    fake_forge.get_issue_state.return_value = "OPEN"
    fake_forge.get_issue_labels.side_effect = lambda issue_number: tuple(
        labels[issue_number]
    )
    fake_forge.get_label_actor.return_value = "trusted-actor"
    fake_forge.get_actor_permission.return_value = "write"
    fake_forge.add_label.side_effect = add_label
    fake_forge.remove_label.side_effect = remove_label
    config = DispatcherConfig(
        apply=True,
        max_concurrent=0,
        run_state_path=tmp_path / "state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=tmp_path / "worktrees",
    )

    with (
        patch("orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]),
        patch(
            "orchestune.dispatch.cycle._sync_external_locks",
            return_value=ExternalLockScanResult(to_lock=[], to_unlock=[]),
        ),
    ):
        first = run_dispatch_cycle(config)
        second = run_dispatch_cycle(config)

    assert first.consistency.mode is ConsistencyMode.OFF
    assert second.consistency.mode is ConsistencyMode.OFF
    assert first.consistency.repair_passes[-1].results[0].status is RepairStatus.FAILED
    assert (
        second.consistency.repair_passes[-1].results[0].status is RepairStatus.APPLIED
    )
    persisted = [
        json.loads(line)["consistency"]
        for line in config.events_log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [item["mode"] for item in persisted] == ["off", "off"]
    assert [
        item["repair_passes"][-1]["results"][0]["status"] for item in persisted
    ] == [
        "failed",
        "applied",
    ]
    assert remove_attempts == 2
    assert labels[743] == ["status:queued"]
    journal = IntentJournal(status_intent_journal_path(config))
    assert len(journal.load()) == 1
    assert journal.load()[0].status is IntentStatus.VERIFIED


def test_user_allowlisted_status_repair_resumes_when_first_forge_write_fails(
    tmp_path, fake_forge
):
    labels = {
        758: ["status:queued"],
        759: ["status:queued"],
    }
    add_attempts = 0

    def current_issue(issue_number):
        return make_issue(
            issue_number,
            labels=tuple(labels[issue_number]),
            subtask_id="dependency" if issue_number == 758 else "main-merge",
            depends_on=() if issue_number == 758 else ("dependency",),
            parent=None,
        )

    def list_issues(label, *args, **kwargs):
        return [
            current_issue(issue_number)
            for issue_number in labels
            if label in labels[issue_number]
        ]

    def fail_first_add(issue_number, label):
        nonlocal add_attempts
        if issue_number == 759 and label == "status:blocked":
            add_attempts += 1
            if add_attempts == 1:
                raise RuntimeError("transient Forge failure")
        if label not in labels[issue_number]:
            labels[issue_number].append(label)

    def remove_label(issue_number, label):
        labels[issue_number].remove(label)

    fake_forge.list_issues_by_label.side_effect = list_issues
    fake_forge.list_open_prs.return_value = []
    fake_forge.get_issue_state.return_value = "OPEN"
    fake_forge.get_issue_labels.side_effect = lambda issue_number: tuple(
        labels[issue_number]
    )
    fake_forge.get_label_actor.return_value = "trusted-actor"
    fake_forge.get_actor_permission.return_value = "write"
    fake_forge.add_label.side_effect = fail_first_add
    fake_forge.remove_label.side_effect = remove_label
    config = DispatcherConfig(
        apply=True,
        consistency_mode=ConsistencyMode.REPAIR,
        consistency_repair_allowlist=frozenset({QUEUED_WITH_UNRESOLVED_DEPENDENCIES}),
        max_concurrent=0,
        run_state_path=tmp_path / "state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=tmp_path / "worktrees",
        forge=fake_forge,
    )

    with (
        patch("orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]),
        patch(
            "orchestune.dispatch.cycle._sync_external_locks",
            return_value=ExternalLockScanResult(to_lock=[], to_unlock=[]),
        ),
    ):
        first = run_dispatch_cycle(config)
        second = run_dispatch_cycle(config)

    first_result = next(
        result
        for repair_pass in first.consistency.repair_passes
        for result in repair_pass.results
        if dict(result.command.parameters).get("finding_code")
        == QUEUED_WITH_UNRESOLVED_DEPENDENCIES
    )
    second_result = next(
        result
        for repair_pass in second.consistency.repair_passes
        for result in repair_pass.results
        if dict(result.command.parameters).get("finding_code")
        == QUEUED_WITH_UNRESOLVED_DEPENDENCIES
    )
    assert first_result.status is RepairStatus.FAILED
    assert second_result.status is RepairStatus.APPLIED
    assert add_attempts == 2
    assert labels[759] == ["status:blocked"]
    (intent,) = IntentJournal(status_intent_journal_path(config)).load()
    assert intent.status is IntentStatus.VERIFIED


def test_applied_status_intent_is_verified_next_cycle_after_read_failure(
    tmp_path, fake_forge
):
    labels = {
        758: ["status:queued"],
        759: ["status:queued"],
    }
    fail_verification_read = True

    def current_issue(issue_number):
        return make_issue(
            issue_number,
            labels=tuple(labels[issue_number]),
            subtask_id="dependency" if issue_number == 758 else "main-merge",
            depends_on=() if issue_number == 758 else ("dependency",),
            # #799: 依存解決は`(parent_number, subtask_id)`でスコープするため、
            # 依存元(758)・依存先(759)が同じ親を持つ必要がある（`make_issue`の
            # 既定`{"number": 100}`のままでよい。このテストは758が
            # `status:done`になった後に759が解決される展開を検証する）。
        )

    def list_issues(label, *args, **kwargs):
        return [
            current_issue(issue_number)
            for issue_number in labels
            if label in labels[issue_number]
        ]

    def get_issue_labels(issue_number):
        nonlocal fail_verification_read
        current = tuple(labels[issue_number])
        if issue_number == 759 and current == ("status:blocked",):
            if fail_verification_read:
                fail_verification_read = False
                raise RuntimeError("verification read failed")
        return current

    def add_label(issue_number, label):
        if label not in labels[issue_number]:
            labels[issue_number].append(label)

    def remove_label(issue_number, label):
        labels[issue_number].remove(label)

    fake_forge.list_issues_by_label.side_effect = list_issues
    fake_forge.list_open_prs.return_value = []
    fake_forge.get_issue_state.return_value = "OPEN"
    fake_forge.get_issue_labels.side_effect = get_issue_labels
    fake_forge.get_label_actor.return_value = "trusted-actor"
    fake_forge.get_actor_permission.return_value = "write"
    fake_forge.add_label.side_effect = add_label
    fake_forge.remove_label.side_effect = remove_label
    config = DispatcherConfig(
        apply=True,
        consistency_mode=ConsistencyMode.REPAIR,
        consistency_repair_allowlist=frozenset({QUEUED_WITH_UNRESOLVED_DEPENDENCIES}),
        max_concurrent=0,
        run_state_path=tmp_path / "state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=tmp_path / "worktrees",
        forge=fake_forge,
    )

    with (
        patch("orchestune.dispatch.phase_rebase.list_remote_branches", return_value=[]),
        patch(
            "orchestune.dispatch.cycle._sync_external_locks",
            return_value=ExternalLockScanResult(to_lock=[], to_unlock=[]),
        ),
    ):
        first = run_dispatch_cycle(config)
        first_result = next(
            result
            for repair_pass in first.consistency.repair_passes
            for result in repair_pass.results
            if dict(result.command.parameters).get("finding_code")
            == QUEUED_WITH_UNRESOLVED_DEPENDENCIES
        )
        assert first_result.status is RepairStatus.FAILED
        assert first_result.diagnostics == ("RuntimeError: verification read failed",)
        assert labels[759] == ["status:blocked"]
        (applied_intent,) = IntentJournal(status_intent_journal_path(config)).load()
        assert applied_intent.status is IntentStatus.APPLIED

        labels[758] = ["status:done"]
        second = run_dispatch_cycle(config)

    second_result = next(
        result
        for repair_pass in second.consistency.repair_passes
        for result in repair_pass.results
        if dict(result.command.parameters).get("finding_code")
        == BLOCKED_WITH_RESOLVED_DEPENDENCIES
    )
    assert second_result.status is RepairStatus.APPLIED
    assert labels[759] == ["status:queued"]
    intents = IntentJournal(status_intent_journal_path(config)).load()
    assert len(intents) == 2
    assert all(intent.status is IntentStatus.VERIFIED for intent in intents)
