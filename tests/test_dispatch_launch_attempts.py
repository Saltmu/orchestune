"""Fault injection across the remote launch / local state boundary (#818)."""

from unittest.mock import patch

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.launch import TaskLaunchPlan, _apply_task_launches
from orchestune.dispatch.state import RunState, load_run_state
from orchestune.dispatch.targets import CodexCloudDispatchTarget, DispatchHandle
from tests.conftest import FakeForge, make_issue, make_task


@pytest.fixture
def launch_env(tmp_path):
    forge = FakeForge()
    forge.issues[1] = make_issue()
    target = CodexCloudDispatchTarget("env-test")
    config = DispatcherConfig(
        forge=forge,
        dispatch_target=target,
        apply=True,
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "state.json",
        worktree_root=tmp_path / "worktrees",
    )
    plan = TaskLaunchPlan(make_task(), "claude/issue-1-task-1", None, "origin/main")
    with (
        patch.object(target, "completion_status", return_value="pending"),
        patch("orchestune.dispatch.worktree._create_worktree", autospec=True),
        patch(
            "orchestune.dispatch.worktree._cleanup_existing_worktree",
            autospec=True,
            return_value=None,
        ),
        patch.object(
            target,
            "launch",
            return_value=DispatchHandle(
                external_id="task_remote",
                external_url="https://example.test/task_remote",
            ),
        ) as launch,
    ):
        yield forge, config, plan, launch


@pytest.mark.parametrize("stop", ["local-save", "label-transition"])
def test_saved_handle_restored_after_crash_without_pr(launch_env, stop):
    forge, config, plan, launch = launch_env
    boundary = (
        patch("orchestune.dispatch.launch.save_run_state", side_effect=OSError("crash"))
        if stop == "local-save"
        else patch.object(forge, "add_label", side_effect=OSError("crash"))
    )
    with boundary, pytest.raises(OSError, match="crash"):
        _apply_task_launches([plan], RunState(), 100.0, config)
    fresh = RunState()
    _apply_task_launches([plan], fresh, 110.0, config)
    assert launch.call_count == 1
    active = fresh.active_worktrees["1"]
    assert active.external_id == "task_remote"
    assert active.launch_attempt_id
    assert active.launch_phase == "launched"
    assert load_run_state(config.run_state_path).active_worktrees["1"] == active


def test_unknown_launch_is_not_retried_after_state_loss(launch_env):
    forge, config, plan, launch = launch_env
    launch.side_effect = OSError("response lost after acceptance")
    assert _apply_task_launches([plan], RunState(), 100.0, config) == []
    _apply_task_launches([plan], RunState(), 110.0, config)
    assert launch.call_count == 1
    assert "status:blocked-human-review" in forge.get_issue_labels(1)
    assert "unknown" in forge.issues[1].body


def test_failed_prelaunch_persistence_never_calls_provider(launch_env):
    forge, config, plan, launch = launch_env
    with patch.object(forge, "update_issue_body", side_effect=OSError("write failed")):
        with pytest.raises(OSError, match="write failed"):
            _apply_task_launches([plan], RunState(), 100.0, config)
    launch.assert_not_called()


@pytest.mark.parametrize("phase", ["prepared", "unknown", "launched"])
def test_remote_write_response_loss_is_safe(launch_env, phase):
    from orchestune.dispatch.attempt_record import attempt_from_body

    forge, config, plan, launch = launch_env
    update = forge.update_issue_body

    def lose_response(number, body):
        update(number, body)
        if attempt_from_body(body).phase == phase:
            raise OSError("journal response lost")

    with patch.object(forge, "update_issue_body", side_effect=lose_response):
        if phase == "prepared":
            with pytest.raises(OSError, match="journal response lost"):
                _apply_task_launches([plan], RunState(), 100.0, config)
        else:
            assert _apply_task_launches([plan], RunState(), 100.0, config) == []
    original = attempt_from_body(forge.issues[1].body)
    _apply_task_launches([plan], RunState(), 110.0, config)
    assert launch.call_count == (0 if phase == "unknown" else 1)
    assert attempt_from_body(forge.issues[1].body).attempt_id == original.attempt_id


def test_handle_save_failure_holds_and_preserves_quota_and_worktree(launch_env):
    from orchestune.dispatch.attempt_record import attempt_from_body
    from orchestune.issue_parsing import launch_history_from_body

    forge, config, plan, launch = launch_env
    config.parent_issue_number = 100
    forge.issues[100] = make_issue(100, body="Parent")
    update = forge.update_issue_body

    def fail_handle_write(number, body):
        attempt = attempt_from_body(body)
        if attempt and attempt.phase == "launched":
            raise OSError("handle write failed")
        update(number, body)

    with (
        patch.object(forge, "update_issue_body", side_effect=fail_handle_write),
        patch(
            "orchestune.dispatch.worktree._cleanup_failed_worktree", autospec=True
        ) as cleanup,
    ):
        _apply_task_launches([plan], RunState(), 100.0, config)
    cleanup.assert_not_called()
    assert launch_history_from_body(forge.issues[100].body) == [100.0]
    _apply_task_launches([plan], RunState(), 110.0, config)
    assert launch.call_count == 1


@pytest.mark.parametrize("known", [True, False])
@pytest.mark.parametrize("status", ["status:queued", "status:in-progress"])
def test_startup_recovery_without_pr_never_requeues_a_possible_launch(
    launch_env, known, status
):
    from orchestune.dispatch.cycle import _run_recovery_bookkeeping_boundary

    forge, config, plan, launch = launch_env
    if known:
        _apply_task_launches([plan], RunState(), 100.0, config)
    else:
        launch.side_effect = OSError("response lost")
        _apply_task_launches([plan], RunState(), 100.0, config)
    forge.remove_label(1, "status:in-progress")
    forge.remove_label(1, "status:queued")
    forge.add_label(1, status)
    fresh = RunState()
    _run_recovery_bookkeeping_boundary(fresh, config, now=110.0)
    assert "status:queued" not in forge.get_issue_labels(1)
    assert launch.call_count == 1
    if known:
        assert fresh.active_worktrees["1"].external_id == "task_remote"
    else:
        assert "status:blocked-human-review" in forge.get_issue_labels(1)


@pytest.mark.parametrize("result", ["matched", "missing", "error"])
def test_lookup_never_implies_permission_to_relaunch(launch_env, result):
    from orchestune.dispatch.targets import LaunchCapabilities

    forge, config, plan, launch = launch_env
    launch.side_effect = OSError("response lost")
    _apply_task_launches([plan], RunState(), 100.0, config)
    config.dispatch_target.launch_capabilities = LaunchCapabilities(
        durable_attempt=True, lookup_by_attempt=True
    )
    with patch.object(config.dispatch_target, "lookup_launch_attempt") as lookup:
        lookup.return_value = (
            DispatchHandle(external_id="task_matched") if result == "matched" else None
        )
        if result == "error":
            lookup.side_effect = OSError("lookup unavailable")
        fresh = RunState()
        _apply_task_launches([plan], fresh, 110.0, config)
    lookup.assert_called_once()
    assert launch.call_count == 1
    if result == "matched":
        assert fresh.active_worktrees["1"].external_id == "task_matched"
    else:
        assert "status:blocked-human-review" in forge.get_issue_labels(1)


def test_dry_run_recovery_has_no_writes_or_launches(launch_env):
    from orchestune.dispatch.cycle import _run_recovery_bookkeeping_boundary

    forge, config, plan, launch = launch_env
    _apply_task_launches([plan], RunState(), 100.0, config)
    config.apply = False
    with (
        patch.object(forge, "update_issue_body") as write,
        patch.object(forge, "add_label") as label,
    ):
        fresh = RunState()
        _run_recovery_bookkeeping_boundary(fresh, config, now=110.0)
    write.assert_not_called()
    label.assert_not_called()
    assert fresh.active_worktrees == {}
    assert launch.call_count == 1


def test_hard_stop_before_provider_resumes_same_prepared_attempt(launch_env):
    from orchestune.dispatch.attempt_record import read_attempt

    forge, config, plan, launch = launch_env
    with patch(
        "orchestune.dispatch.worktree._create_worktree", side_effect=SystemExit("stop")
    ):
        with pytest.raises(SystemExit):
            _apply_task_launches([plan], RunState(), 100.0, config)
    prepared = read_attempt(forge, 1)
    assert prepared.phase == "prepared"
    launch.assert_not_called()
    fresh = RunState()
    _apply_task_launches([plan], fresh, 110.0, config)
    assert fresh.active_worktrees["1"].launch_attempt_id == prepared.attempt_id
    assert launch.call_count == 1


def test_ambiguous_provider_result_is_reported_only_once(launch_env):
    forge, config, plan, launch = launch_env
    launch.return_value = DispatchHandle()
    _apply_task_launches([plan], RunState(), 100.0, config)
    for _ in range(3):
        _apply_task_launches([plan], RunState(), 110.0, config)
    assert len(forge.comments[1]) == 1
    assert launch.call_count == 1


def test_recovery_does_not_take_over_another_parents_attempt(launch_env):
    from orchestune.dispatch.cycle import _run_recovery_bookkeeping_boundary

    forge, config, plan, launch = launch_env
    _apply_task_launches([plan], RunState(), 100.0, config)
    config.parent_issue_number = 200
    forge.issues[200] = make_issue(200, body="Other parent")
    fresh = RunState()
    _run_recovery_bookkeeping_boundary(fresh, config, now=110.0)
    assert fresh.active_worktrees == {}


def test_journal_does_not_override_recovery_counter_bookkeeping(launch_env):
    from orchestune.dispatch.cycle import _run_recovery_bookkeeping_boundary

    forge, config, plan, launch = launch_env
    state = RunState()
    _apply_task_launches([plan], state, 100.0, config)
    body = forge.issues[1].body.replace(
        "subtask_id: task-1",
        "subtask_id: task-1\nrecompute_count: 3\nforced_serial: true",
    )
    forge.update_issue_body(1, body)
    _run_recovery_bookkeeping_boundary(state, config, now=110.0)
    assert state.active_worktrees["1"].recompute_count == 3
    assert state.active_worktrees["1"].forced_serial


@pytest.mark.parametrize(
    "status",
    [
        "status:done",
        "status:not-needed",
        "status:manual-merge-required",
        "status:blocked-human-review",
    ],
)
def test_stale_launch_plan_does_not_override_terminal_status(launch_env, status):
    forge, config, plan, launch = launch_env
    _apply_task_launches([plan], RunState(), 100.0, config)
    forge.remove_label(1, "status:in-progress")
    forge.add_label(1, status)
    fresh = RunState()
    _apply_task_launches([plan], fresh, 110.0, config)
    assert fresh.active_worktrees == {}
    assert forge.get_issue_labels(1) == (status,)
    assert launch.call_count == 1


def test_unknown_launch_does_not_abort_other_selected_tasks(launch_env):
    from orchestune.dispatch.attempt_record import read_attempt

    forge, config, plan, launch = launch_env
    forge.issues[2] = make_issue(2)
    second = TaskLaunchPlan(make_task(2), "claude/issue-2-task-2", None, "origin/main")
    launch.side_effect = [
        OSError("response lost"),
        DispatchHandle(external_id="task_second"),
    ]
    state = RunState()
    selected = _apply_task_launches([plan, second], state, 100.0, config)
    assert selected == [second.task]
    assert read_attempt(forge, 1).phase == "unknown"
    assert state.active_worktrees["2"].external_id == "task_second"
    assert state.launch_history == [100.0, 100.0]
    assert launch.call_count == 2


def test_other_parent_counters_remain_monotonic(launch_env):
    from orchestune.dispatch.cycle import _run_recovery_bookkeeping_boundary

    forge, config, plan, launch = launch_env
    state = RunState()
    _apply_task_launches([plan], state, 100.0, config)
    config.parent_issue_number = 200
    forge.issues[200] = make_issue(200, body="Other parent")
    forge.update_issue_body(
        1,
        forge.issues[1].body.replace(
            "subtask_id: task-1",
            "subtask_id: task-1\nrecompute_count: 3\nforced_serial: true",
        ),
    )
    _run_recovery_bookkeeping_boundary(state, config, now=110.0)
    assert state.active_worktrees["1"].recompute_count == 3
    assert state.active_worktrees["1"].forced_serial
