"""Contracts for the dispatch launch owner API (#1130)."""

from __future__ import annotations

from dataclasses import fields
from typing import Any, cast
from unittest.mock import patch

import pytest

from orchestune.claim.contracts import ClaimOutcome
from orchestune.dispatch.launch import (
    TaskLaunchPlan,
    _build_active_worktree_from_launch,
    _try_planned_launch,
)
from orchestune.dispatch.launch_state import (
    build_launch_record,
    launch_info_with_selection,
    launch_info_with_token_estimate,
    with_launch,
    with_launch_phase,
)
from orchestune.dispatch.recovery_counters import (
    launch_with_recovery_counters,
    merged_recovery_counters,
)
from orchestune.dispatch.worktree import LaunchResult
from orchestune.issue_parsing import backfill_recovery_counters
from orchestune.labels import StatusLabel
from orchestune.ledger.active_records import (
    ActiveCompletionJournal,
    ActiveWorktreeCore,
    ClaimInfo,
    LaunchInfo,
)
from orchestune.ledger.run_state import RunState, TaskReclaimRecord, load_run_state
from orchestune.models import IssueRecord
from orchestune.targets.contracts import ExecutionSelection
from tests.conftest import FakeForge, make_issue
from tests.dispatch_test_support import (
    make_test_active_worktree,
    make_test_dispatcher_config,
    make_test_task,
)
from tests.status_event_test_support import NOW


def _core() -> ActiveWorktreeCore:
    return ActiveWorktreeCore(
        issue_number=7,
        branch="claude/issue-7-task",
        worktree_path="worktrees/claude-issue-7-task",
        declared_footprint=("a.py",),
        base_branch="origin/parent/issue-1",
    )


def _held_record():
    return make_test_active_worktree(
        7,
        pid=11,
        launch_phase="launching",
        launch_attempt_id="attempt-7",
        owner_kind="dispatch",
        claim_id="claim-7",
        claim_stage="completed",
        reservation_kind="repository",
        owner_token_digest="digest-7",
        completion_id="completion-7",
        completion_stage="posting",
        completion_payload={"result": "done"},
    )


def test_build_launch_record_keeps_each_owner_record() -> None:
    claim = ClaimInfo(claim_id="claim-7", claim_stage="completed")
    completion = ActiveCompletionJournal(completion_policy_config={"source": "x"})
    launch = LaunchInfo(pid=5, started_at=2.0, launch_phase="launched")

    active = build_launch_record(
        core=_core(), launch=launch, claim=claim, completion=completion
    )

    assert active.core == _core()
    assert active.launch == launch
    assert active.claim == claim
    assert active.completion == completion


def test_build_launch_record_defaults_to_unclaimed_without_completion() -> None:
    active = build_launch_record(core=_core(), launch=LaunchInfo())

    assert active.claim == ClaimInfo()
    assert active.completion == ActiveCompletionJournal()


def test_with_launch_replaces_only_launch_owned_fields() -> None:
    active = _held_record()
    launch = LaunchInfo(
        pid=None, external_id="ext-1", launch_attempt_id="attempt-8", recompute_count=3
    )

    updated = with_launch(active, launch)

    assert updated.launch == launch
    assert updated.core == active.core
    assert updated.claim == active.claim
    assert updated.completion == active.completion
    assert active.launch.launch_attempt_id == "attempt-7"


def test_with_launch_rejects_other_records() -> None:
    with pytest.raises(TypeError, match="LaunchInfo"):
        with_launch(_held_record(), ClaimInfo())  # type: ignore[arg-type]


@pytest.mark.parametrize("phase", ["launching", "failed", None])
def test_with_launch_phase_updates_phase_and_returns_a_copy(phase) -> None:
    active = _held_record()

    updated = with_launch_phase(active, phase)

    assert updated is not active
    assert updated.launch.launch_phase == phase
    assert active.launch.launch_phase == "launching"
    expected = {
        f.name: getattr(active.launch, f.name)
        for f in fields(LaunchInfo)
        if f.name != "launch_phase"
    }
    assert {name: getattr(updated.launch, name) for name in expected} == expected
    assert updated.claim == active.claim
    assert updated.completion == active.completion


def test_selection_fields_come_from_the_execution_selection() -> None:
    selection = ExecutionSelection(
        profile="luna-xhigh", model="m", reasoning_effort="high", reason="tier"
    )

    launch = launch_info_with_selection(
        LaunchInfo(pid=3), selection, fallback_profile="ignored"
    )

    assert (
        launch.pid,
        launch.profile,
        launch.model,
        launch.reasoning_effort,
        launch.selection_reason,
    ) == (3, "luna-xhigh", "m", "high", "tier")


def test_selection_absent_keeps_only_the_fallback_profile() -> None:
    launch = launch_info_with_selection(
        LaunchInfo(model="stale", selection_reason="stale"),
        None,
        fallback_profile="declared",
    )

    assert (
        launch.profile,
        launch.model,
        launch.reasoning_effort,
        launch.selection_reason,
    ) == ("declared", None, None, None)


@pytest.mark.parametrize("tokens", [1200, None])
def test_token_estimate_is_recorded_even_when_unknown(tokens) -> None:
    launch = launch_info_with_token_estimate(LaunchInfo(pid=3), tokens)

    assert launch.estimated_tokens == tokens
    assert launch.token_estimate_recorded is True
    assert launch.pid == 3


class TestUsageLimitLaunchGate:
    """#1270: the chosen target is re-checked right before launch."""

    @staticmethod
    def _plan_and_config(tmp_path, target_name):
        from orchestune.dispatch.config import DispatcherConfig
        from orchestune.dispatch.launch import TaskLaunchPlan
        from orchestune.dispatch.targets import LocalProcessDispatchTarget
        from tests.conftest import FakeForge, make_task

        config = DispatcherConfig(
            parent_issue_number=100,
            forge=FakeForge(),
            dispatch_target=LocalProcessDispatchTarget(target_name=target_name),
            apply=True,
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "state.json",
            worktree_root=tmp_path / "worktrees",
        )
        plan = TaskLaunchPlan(
            make_task(1, parent_number=100), "claude/issue-1-task-1", None, "main"
        )
        return plan, config

    @pytest.mark.parametrize(
        "state_kwargs",
        [
            {"usage_limit_cooldowns": {"claude-cli": 200.0}},
            {
                "task_reclaim_counts": {
                    1: TaskReclaimRecord(
                        usage_limit_retry_count=1, usage_limit_retry_at=200.0
                    )
                }
            },
        ],
    )
    def test_waiting_task_is_not_claimed_or_launched(self, tmp_path, state_kwargs):
        from unittest.mock import MagicMock

        from orchestune.dispatch.launch import _apply_task_launches

        plan, config = self._plan_and_config(tmp_path, "claude-cli")
        claim_fn = MagicMock()
        run_state = RunState(**state_kwargs)

        launched = _apply_task_launches(
            [plan], run_state, 100.0, config, claim_fn=claim_fn
        )

        assert launched == []
        claim_fn.assert_not_called()
        assert run_state.active_worktrees == {}
        assert run_state.launch_history == []


# ---- #1280: the persisted recovery counters reach a normal launch ------------

ISSUE = 41
BRANCH = f"claude/issue-{ISSUE}-task-a"
FORCE_SERIAL = StatusLabel.FORCE_SERIAL


def _issue(count: int, serial: bool, *labels: str) -> IssueRecord:
    base = make_issue(ISSUE, labels=labels)
    body = backfill_recovery_counters(base.body, count, serial) or base.body
    return make_issue(ISSUE, labels=labels, body=body)


@pytest.mark.parametrize(
    ("body", "labels", "launch", "task_serial", "expected"),
    [
        ((0, False), (), (0, False), False, (0, False)),
        ((1, False), (), (0, False), False, (1, False)),
        ((2, False), (), (0, False), False, (2, False)),
        ((2, True), (), (0, False), False, (2, True)),
        ((0, False), (FORCE_SERIAL,), (0, False), False, (0, True)),
        ((1, True), (), (0, False), False, (1, True)),
        ((0, False), (), (2, False), False, (2, False)),
        ((1, False), (), (0, True), False, (1, True)),
        ((0, False), (), (0, False), True, (0, True)),
    ],
    ids=[
        "empty",
        "body-count",
        "body-at-limit",
        "body-forced",
        "label-only",
        "body-forced-without-label",
        "active-count-larger",
        "active-forced-kept",
        "task-snapshot-forced",
    ],
)
def test_recovery_counters_merge_by_max_and_or(
    body, labels, launch, task_serial, expected
) -> None:
    issue = _issue(*body, *labels)
    current = LaunchInfo(recompute_count=launch[0], forced_serial=launch[1])

    assert (
        merged_recovery_counters(issue, current, task_forced_serial=task_serial)
        == expected
    )
    merged = launch_with_recovery_counters(
        current, issue, task_forced_serial=task_serial
    )
    assert (merged.recompute_count, merged.forced_serial) == expected


def test_recovery_counters_without_an_active_launch_use_the_issue_only() -> None:
    assert merged_recovery_counters(_issue(2, True)) == (2, True)
    broken = make_issue(ISSUE, body="## Footprint\n```yaml\nrecompute_count: -3\n```\n")
    assert merged_recovery_counters(broken) == (0, False)


class _Launched:
    """What `_try_planned_launch` returned: the provider ran once."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *_args, **_kwargs) -> LaunchResult:
        self.calls += 1
        return LaunchResult(
            issue_number=ISSUE,
            branch=BRANCH,
            worktree_path="worktrees/w2",
            pid=2,
            launched=True,
            dispatch_started_at=NOW,
        )


def _reservation(count: int = 0, serial: bool = False):
    return make_test_active_worktree(
        ISSUE,
        branch=BRANCH,
        pid=None,
        started_at=None,
        recompute_count=count,
        forced_serial=serial,
        owner_kind="dispatch",
        claim_id="claim-41",
        claim_stage="completed",
        reservation_kind="repository",
        owner_token_digest="digest-41",
    )


def _drive_launch(tmp_path, issue, *, reservation=None, forge_error=None):
    """The normal launch entry: claim, persist the launching phase, provider."""
    forge = FakeForge()
    if issue is not None:
        forge.issues[ISSUE] = issue
    if forge_error is not None:

        def failing(_number):
            raise forge_error

        forge.get_issue = failing  # type: ignore[method-assign]
    config = make_test_dispatcher_config(tmp_path, forge=forge, apply=True)
    run_state = RunState(
        active_worktrees={str(ISSUE): reservation or _reservation()},
    )
    task = make_test_task(ISSUE, status_labels=(StatusLabel.QUEUED,))
    plan = TaskLaunchPlan(task, BRANCH, "main", "main")
    outcome = ClaimOutcome(
        success=True,
        issue_number=ISSUE,
        branch=BRANCH,
        worktree_path=tmp_path / "w2",
        base_ref="main",
    )
    provider = _Launched()
    with patch("orchestune.dispatch.launch._launch_on_prepared_worktree", provider):
        result = _try_planned_launch(
            plan,
            cast(Any, object()),
            config,
            run_state,
            lambda *_: outcome,
            now=NOW,
        )
    return result, provider, run_state, config, plan, task


def _relaunched(result, run_state, config, plan, task):
    return _build_active_worktree_from_launch(
        task, plan, result, run_state, NOW, config
    )


@pytest.mark.parametrize(
    ("body", "labels", "expected"),
    [
        ((0, False), (), (0, False)),
        ((1, False), (), (1, False)),
        ((2, False), (), (2, False)),
        ((2, True), (FORCE_SERIAL,), (2, True)),
        ((0, False), (FORCE_SERIAL,), (0, True)),
        ((1, True), (), (1, True)),
    ],
)
def test_normal_launch_keeps_the_issue_body_counters(
    tmp_path, body, labels, expected
) -> None:
    issue = _issue(*body, *labels)
    result, provider, run_state, config, plan, task = _drive_launch(tmp_path, issue)

    assert provider.calls == 1
    saved = load_run_state(config.run_state_path).active_worktrees[str(ISSUE)]
    assert (saved.launch.recompute_count, saved.launch.forced_serial) == expected
    assert saved.launch.launch_phase == "launching"
    active = _relaunched(result, run_state, config, plan, task)
    assert (active.launch.recompute_count, active.launch.forced_serial) == expected
    assert active.claim.claim_id == "claim-41"
    assert active.launch.launch_phase == "launched"


def test_normal_launch_does_not_lower_a_larger_reservation_counter(tmp_path) -> None:
    result, _, run_state, config, plan, task = _drive_launch(
        tmp_path, _issue(1, False), reservation=_reservation(2, True)
    )

    active = _relaunched(result, run_state, config, plan, task)
    assert (active.launch.recompute_count, active.launch.forced_serial) == (2, True)


def test_the_provider_is_not_called_when_the_issue_cannot_be_read(tmp_path) -> None:
    for kwargs in (
        {"issue": None},
        {"issue": _issue(1, False), "forge_error": OSError("down")},
    ):
        result, provider, run_state, config, *_ = _drive_launch(tmp_path, **kwargs)

        assert provider.calls == 0
        assert result is not None
        assert result.held is True
        assert result.launched is False
        assert "recovery counters" in (result.error_message or "")
        entry = run_state.active_worktrees[str(ISSUE)]
        assert entry.launch.launch_phase is None
        assert entry.claim.claim_id == "claim-41"


def test_the_provider_is_not_called_when_the_counters_cannot_be_saved(
    tmp_path,
) -> None:
    with patch(
        "orchestune.dispatch.launch.save_run_state", side_effect=OSError("disk")
    ):
        result, provider, run_state, *_ = _drive_launch(tmp_path, _issue(2, True))

    assert provider.calls == 0
    assert result is not None and result.held is True
    entry = run_state.active_worktrees[str(ISSUE)]
    assert (entry.launch.recompute_count, entry.launch.forced_serial) == (0, False)
    assert entry.launch.launch_phase is None
