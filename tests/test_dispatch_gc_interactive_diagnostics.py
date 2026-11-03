"""Tests for unattended interactive claim diagnostics in dispatch GC (#1643)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from orchestune.dispatch.config.model import DispatcherConfig
from orchestune.dispatch.cycle.events import InteractiveClaimInspectionRequired
from orchestune.dispatch.cycle.run import run_dispatch_cycle
from orchestune.dispatch.gc.interactive_diagnostics import (
    diagnose_unattended_interactive_claims,
)
from orchestune.ledger.run_state import ActiveWorktree, RunState, save_run_state
from orchestune.models import IssueRecord
from tests.conftest import FakeForge
from tests.dispatch_test_support import make_test_active_worktree


class MockForge(FakeForge):
    def __init__(self, states: dict[int, str] | None = None) -> None:
        super().__init__()
        self.states = states or {}
        self.raise_on_get: bool = False

    def get_issue_state(self, issue_number: int | str) -> str:
        if self.raise_on_get:
            raise RuntimeError("Forge connection failure")
        return self.states.get(int(issue_number), "OPEN")


def _active(
    issue_number: int,
    *,
    owner_kind: str = "interactive",
    claim_id: str = "claim-test",
    branch: str = "claude/issue-1-task",
    worktree_path: str = "/tmp/worktree",
    pid: int | None = None,
    completion_id: str | None = None,
    completion_stage: str | None = None,
) -> ActiveWorktree:
    return make_test_active_worktree(
        issue_number=issue_number,
        owner_kind=owner_kind,
        claim_id=claim_id,
        branch=branch,
        worktree_path=worktree_path,
        pid=pid,
        completion_id=completion_id,
        completion_stage=completion_stage,
    )


def test_diagnose_open_interactive_claim_with_no_pid(tmp_path: Path) -> None:
    active = _active(1629, pid=None, claim_id="claim-1629")
    run_state = RunState(active_worktrees={"1629": active})
    forge = MockForge({1629: "OPEN"})
    config = DispatcherConfig(
        parent_issue_number=1588,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "run_state.json",
        worktree_root=tmp_path / "worktrees",
        forge=forge,
    )

    events = diagnose_unattended_interactive_claims(run_state, config, forge=forge)

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, InteractiveClaimInspectionRequired)
    assert event.issue_number == 1629
    assert event.claim_id == "claim-1629"
    assert event.pid is None
    assert event.process_state == "unknown"
    assert (
        "orchestune recover --issue 1629 --claim-id claim-1629 --release-to-review"
        in event.recovery_command
    )
    assert event.action == "interactive_claim_inspection_required"


def test_diagnose_skips_non_interactive_and_completing_claims(tmp_path: Path) -> None:
    dispatch_claim = _active(101, owner_kind="dispatch")
    completing_claim = _active(102, completion_id="comp-1", completion_stage="reserved")
    interactive_claim = _active(103, pid=None)

    run_state = RunState(
        active_worktrees={
            "101": dispatch_claim,
            "102": completing_claim,
            "103": interactive_claim,
        }
    )
    forge = MockForge({101: "OPEN", 102: "OPEN", 103: "OPEN"})
    config = DispatcherConfig(
        parent_issue_number=1588,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "run_state.json",
        worktree_root=tmp_path / "worktrees",
        forge=forge,
    )

    events = diagnose_unattended_interactive_claims(run_state, config, forge=forge)

    assert len(events) == 1
    assert events[0].issue_number == 103


def test_diagnose_skips_closed_and_merged_targets(tmp_path: Path) -> None:
    closed_claim = _active(104)
    merged_claim = _active(105)
    open_claim = _active(106)

    run_state = RunState(
        active_worktrees={
            "104": closed_claim,
            "105": merged_claim,
            "106": open_claim,
        }
    )
    forge = MockForge({104: "CLOSED", 105: "MERGED", 106: "OPEN"})
    config = DispatcherConfig(
        parent_issue_number=1588,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "run_state.json",
        worktree_root=tmp_path / "worktrees",
        forge=forge,
    )

    events = diagnose_unattended_interactive_claims(run_state, config, forge=forge)

    assert len(events) == 1
    assert events[0].issue_number == 106


def test_diagnose_alive_and_dead_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alive_claim = _active(107, pid=1111)
    dead_claim = _active(108, pid=2222)

    run_state = RunState(
        active_worktrees={
            "107": alive_claim,
            "108": dead_claim,
        }
    )
    forge = MockForge({107: "OPEN", 108: "OPEN"})
    config = DispatcherConfig(
        parent_issue_number=1588,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "run_state.json",
        worktree_root=tmp_path / "worktrees",
        forge=forge,
    )

    from orchestune.infra import process_utils

    monkeypatch.setattr(process_utils, "is_process_alive", lambda pid: pid == 1111)

    events = diagnose_unattended_interactive_claims(run_state, config, forge=forge)

    assert len(events) == 2
    by_issue = {e.issue_number: e for e in events}
    assert by_issue[107].process_state == "alive"
    assert by_issue[107].pid == 1111
    assert by_issue[108].process_state == "dead"
    assert by_issue[108].pid == 2222


def test_diagnose_forge_failure_is_held_as_unknown(tmp_path: Path) -> None:
    claim = _active(109, pid=3333)
    run_state = RunState(active_worktrees={"109": claim})
    forge = MockForge()
    forge.raise_on_get = True
    config = DispatcherConfig(
        parent_issue_number=1588,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "run_state.json",
        worktree_root=tmp_path / "worktrees",
        forge=forge,
    )

    events = diagnose_unattended_interactive_claims(run_state, config, forge=forge)

    assert len(events) == 1
    assert events[0].issue_number == 109
    assert events[0].process_state == "unknown"
    assert "forge" in events[0].reason.lower() or "Forge" in events[0].reason


def test_dispatch_cycle_reports_unattended_interactive_claim(
    tmp_path: Path,
) -> None:
    """#1643: An OPEN unattended interactive claim is diagnosed and reported in cycle completion events."""
    events_file = tmp_path / "events.jsonl"
    run_state_file = tmp_path / "run_state.json"
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)

    interactive_active = make_test_active_worktree(
        issue_number=1629,
        branch="claude/issue-1629-task",
        worktree_path=str(worktrees_dir / "wt-1629"),
        owner_kind="interactive",
        claim_id="claim-1629",
        pid=None,
    )
    run_state = RunState(active_worktrees={"1629": interactive_active})
    save_run_state(run_state, run_state_file)

    fake_forge = MagicMock()
    config = DispatcherConfig(
        parent_issue_number=1588,
        events_log_path=events_file,
        run_state_path=run_state_file,
        worktree_root=worktrees_dir,
        forge=fake_forge,
        dispatch_target=MagicMock(),
    )

    issue_1629 = IssueRecord(
        number=1629,
        title="Unattended interactive issue",
        body="## Footprint\n```yaml\nsubtask_id: task-1629\nfootprint: [a.py]\n```",
        labels=("status:in-progress",),
        state="OPEN",
        created_at="",
    )
    fake_forge.get_issue.side_effect = (
        lambda num: issue_1629
        if num == 1629
        else IssueRecord(
            number=num, title="", body="", labels=(), state="OPEN", created_at=""
        )
    )
    fake_forge.get_issue_state.side_effect = lambda num: "OPEN"
    fake_forge.list_sub_issues.return_value = []
    fake_forge.list_open_prs.return_value = []
    fake_forge.list_merged_prs_for_base.return_value = []

    report = run_dispatch_cycle(config)

    inspection_events = [
        e
        for e in report.completion_events
        if isinstance(e, InteractiveClaimInspectionRequired)
    ]
    assert len(inspection_events) == 1
    assert inspection_events[0].issue_number == 1629
    assert inspection_events[0].claim_id == "claim-1629"
    assert inspection_events[0].process_state == "unknown"
