"""dispatch cycleへのconsistency shadow統合 (#706)。"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from unittest.mock import patch

from orchestune.consistency.supervisor import ConsistencyMode, ScanKind
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import run_dispatch_cycle
from orchestune.dispatch.cycle_context import IssuesByStatus
from orchestune.dispatch.cycle_report import CycleReport, build_event_log_entry
from orchestune.dispatch.dispatcher import _build_arg_parser, main
from orchestune.dispatch.report import _report_to_dict
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.state import RunState
from tests.conftest import make_issue, make_task


def _issues(issue) -> IssuesByStatus:
    return IssuesByStatus(
        queued=[issue],
        locked=[],
        in_progress=[],
        blocked=[],
        done=[],
        not_needed=[],
    )


def _report(
    *, applied: bool, promotion_events: list[dict] | None = None
) -> CycleReport:
    return CycleReport(
        selected=[],
        quota_slots_available=1,
        lock_changes={"to_lock": [], "to_unlock": []},
        deviation_events=[],
        completion_events=[],
        promotion_events=promotion_events or [],
        applied=applied,
    )


def _ctx(config: DispatcherConfig, run_state: RunState, task) -> CycleContext:
    return CycleContext(
        run_state=run_state,
        tasks_by_issue={task.issue_number: task},
        issue_number_by_subtask_id={task.subtask_id: task.issue_number},
        done_subtask_ids=set(),
        ci_passed_pr_subtask_ids=set(),
        changes_requested_subtask_ids=set(),
        subtask_branch_map={task.subtask_id: f"codex/issue-{task.issue_number}"},
        prs=[],
        pr_by_branch={},
        config=config,
    )


def _run_patched_cycle(
    config: DispatcherConfig,
    *,
    issue,
    task,
    run_state: RunState,
    pipeline_report: CycleReport,
) -> CycleReport:
    issues = _issues(issue)
    ctx = _ctx(config, run_state, task)
    with (
        patch("orchestune.dispatch.cycle.load_run_state", return_value=run_state),
        patch("orchestune.dispatch.cycle._prepare_cycle_issues", return_value=issues),
        patch("orchestune.dispatch.cycle._build_cycle_context", return_value=ctx),
        patch(
            "orchestune.dispatch.cycle._execute_cycle_pipeline",
            return_value=pipeline_report,
        ),
    ):
        return run_dispatch_cycle(config)


def test_consistency_mode_is_exposed_by_cli_and_defaults_off(
    tmp_path, fake_forge
) -> None:
    parser = _build_arg_parser()
    assert parser.parse_args([]).consistency_mode == "off"
    assert (
        parser.parse_args(["--consistency-mode", "shadow"]).consistency_mode == "shadow"
    )

    captured = []

    def capture(config: DispatcherConfig) -> CycleReport:
        captured.append(config)
        return _report(applied=False)

    with patch(
        "orchestune.dispatch.dispatcher.run_dispatch_cycle", side_effect=capture
    ):
        assert (
            main(
                [
                    "--no-apply",
                    "--dispatch-target",
                    "local",
                    "--consistency-mode",
                    "shadow",
                    "--run-state-path",
                    str(tmp_path / "state.json"),
                    "--events-log-path",
                    str(tmp_path / "events.jsonl"),
                ]
            )
            == 0
        )

    assert captured[0].consistency_mode is ConsistencyMode.SHADOW
    direct = DispatcherConfig(
        events_log_path=tmp_path / "direct-events.jsonl",
        consistency_mode="shadow",  # type: ignore[arg-type]
    )
    assert direct.consistency_mode is ConsistencyMode.SHADOW


def test_real_pipeline_shadow_is_read_only_and_keeps_scheduling(
    tmp_path, fake_forge
) -> None:
    issue = make_issue(706, labels=("status:queued",), parent=None)

    def issues_for(label: str, *args, **kwargs):
        return [issue] if label == "status:queued" else []

    fake_forge.list_issues_by_label.side_effect = issues_for
    fake_forge.list_open_prs.return_value = []
    common = {
        "apply": False,
        "max_concurrent": 1,
        "max_launches_per_window": 1,
        "worktree_root": tmp_path / "worktrees",
    }
    off = run_dispatch_cycle(
        DispatcherConfig(
            **common,
            consistency_mode=ConsistencyMode.OFF,
            events_log_path=tmp_path / "off-events.jsonl",
            run_state_path=tmp_path / "off-state.json",
        )
    )
    shadow = run_dispatch_cycle(
        DispatcherConfig(
            **common,
            consistency_mode=ConsistencyMode.SHADOW,
            events_log_path=tmp_path / "shadow-events.jsonl",
            run_state_path=tmp_path / "shadow-state.json",
        )
    )

    assert [task.subtask_id for task in shadow.selected] == [
        task.subtask_id for task in off.selected
    ]
    assert shadow.scheduling_decisions == off.scheduling_decisions
    assert not (tmp_path / "shadow-state.json").exists()
    assert not (tmp_path / "worktrees").exists()
    fake_forge.add_label.assert_not_called()
    fake_forge.remove_label.assert_not_called()
    fake_forge.add_comment.assert_not_called()


def test_off_and_shadow_preserve_existing_pipeline_outcome(
    tmp_path, fake_forge
) -> None:
    issue = make_issue(706, labels=("status:queued",), parent=None)
    task = make_task(706, subtask_id="shadow-supervisor")
    original_state = RunState()
    common = {
        "apply": False,
        "events_log_path": tmp_path / "events.jsonl",
        "run_state_path": tmp_path / "state.json",
        "worktree_root": tmp_path / "worktrees",
    }
    off = _run_patched_cycle(
        DispatcherConfig(**common, consistency_mode=ConsistencyMode.OFF),
        issue=issue,
        task=task,
        run_state=copy.deepcopy(original_state),
        pipeline_report=_report(applied=False),
    )
    shadow = _run_patched_cycle(
        DispatcherConfig(**common, consistency_mode=ConsistencyMode.SHADOW),
        issue=issue,
        task=task,
        run_state=copy.deepcopy(original_state),
        pipeline_report=_report(applied=False),
    )

    for field in (
        "selected",
        "quota_slots_available",
        "lock_changes",
        "deviation_events",
        "completion_events",
        "promotion_events",
        "applied",
        "scheduling_decisions",
        "execution_selections",
    ):
        assert getattr(shadow, field) == getattr(off, field)
    assert off.consistency.mode is ConsistencyMode.OFF
    assert off.consistency.scans == ()
    assert [scan.boundary for scan in shadow.consistency.scans] == ["start", "end"]


def test_shadow_records_targeted_events_and_authoritative_end_diff(
    tmp_path, fake_forge
) -> None:
    issue = make_issue(706, labels=("status:queued",), parent=None)
    task = make_task(706, subtask_id="shadow-supervisor")
    run_state = RunState()
    config = DispatcherConfig(
        apply=True,
        consistency_mode=ConsistencyMode.SHADOW,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "state.json",
        worktree_root=tmp_path / "worktrees",
    )
    # The start scan reuses the cycle snapshot. The fresh end scan sees no task,
    # modeling an out-of-process Forge change that emitted no in-process event.
    fake_forge.list_issues_by_label.return_value = []
    fake_forge.list_open_prs.return_value = []

    report = _run_patched_cycle(
        config,
        issue=issue,
        task=task,
        run_state=run_state,
        pipeline_report=_report(
            applied=True,
            promotion_events=[
                {"subtask_id": "shadow-supervisor", "action": "promoted"}
            ],
        ),
    )

    assert [scan.kind for scan in report.consistency.scans] == [
        ScanKind.FULL,
        ScanKind.TARGETED,
        ScanKind.FULL,
    ]
    targeted = report.consistency.scans[1]
    assert targeted.state_changes[0].scope.value == "task"
    assert targeted.state_changes[0].subject_id == "706"
    assert targeted.state_changes[0].fields == ("issue_labels",)
    end = report.consistency.scans[2]
    assert any(
        change.scope.value == "task" and change.subject_id == "706"
        for change in end.state_changes
    )


def test_shadow_observation_failure_is_reported_without_failing_cycle(
    tmp_path, fake_forge
) -> None:
    issue = make_issue(706, labels=("status:queued",), parent=None)
    task = make_task(706, subtask_id="shadow-supervisor")
    config = DispatcherConfig(
        apply=False,
        consistency_mode=ConsistencyMode.SHADOW,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "state.json",
        worktree_root=tmp_path / "worktrees",
    )
    fake_forge.list_issues_by_label.side_effect = OSError("forge unavailable")

    report = _run_patched_cycle(
        config,
        issue=issue,
        task=task,
        run_state=RunState(),
        pipeline_report=_report(applied=False),
    )

    assert report.quota_slots_available == 1
    end = report.consistency.scans[-1]
    assert [finding.code for finding in end.report.findings] == [
        "supervisor.observation-failed"
    ]
    assert end.unknown_facts[0].diagnostics == ("OSError: forge unavailable",)


def test_consistency_data_is_in_cycle_json_and_event_log(tmp_path, fake_forge) -> None:
    issue = make_issue(706, labels=("status:queued",), parent=None)
    task = make_task(706, subtask_id="shadow-supervisor")
    config = DispatcherConfig(
        apply=False,
        consistency_mode=ConsistencyMode.SHADOW,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "state.json",
        worktree_root=tmp_path / "worktrees",
    )
    report = _run_patched_cycle(
        config,
        issue=issue,
        task=task,
        run_state=RunState(),
        pipeline_report=_report(applied=False),
    )

    report_payload = _report_to_dict(report)
    event_payload = build_event_log_entry(
        report, datetime(2026, 8, 29, tzinfo=UTC).timestamp()
    )
    assert report_payload["consistency"]["mode"] == "shadow"
    assert event_payload["consistency"] == report_payload["consistency"]
    json.dumps(report_payload)
    json.dumps(event_payload)
