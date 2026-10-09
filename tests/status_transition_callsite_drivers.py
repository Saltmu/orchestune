"""Drivers that execute each production `transition_status_label` call site (#1217).

Every driver runs the real production function against an in-memory `FakeForge`
holding the case's actual label set. Collaborators that only matter for the
surrounding side effects (worktree removal, run-state persistence, ...) are
stubbed; the label arguments handed to the adapter are always computed by the
production code under test.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from orchestune.claim import service as claim_service
from orchestune.consistency.models import ConsistencyScope, RepairCommand
from orchestune.consistency.repairs.execution import COMMAND_REQUEUE
from orchestune.consistency.repairs.status import (
    COMMAND_TRANSITION_LABEL,
    plan_status_repairs,
)
from orchestune.dispatch import (
    launch,
    launch_attempts,
    prior_parent_merge,
    rebase,
    reconciliation,
    recovery,
    status_repair,
)
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_events import CompletionEvent
from orchestune.dispatch.gc import cloud_completion, completion, zombies
from orchestune.integrator.steps import AutoMergeChildIntegrationStep
from orchestune.ledger.escalation import apply_human_review_escalation
from orchestune.ledger.run_state import RunState
from tests.conftest import FakeForge, make_issue
from tests.consistency_status_test_support import (
    _desired,
    _desired_task,
    _evaluate,
    _observed,
    _task_scope,
)
from tests.dispatch_test_support import (
    make_test_active_worktree,
    make_test_cycle_context,
    make_test_task,
)

ISSUE = 705
_NOOP = lambda *args, **kwargs: None  # noqa: E731


def _fake(**values: Any) -> Any:
    """A duck-typed stand-in for objects the driven function only reads."""
    return SimpleNamespace(**values)


@dataclass
class Env:
    """What a driver may use: the forge, config and the case's parameters."""

    forge: FakeForge
    config: DispatcherConfig
    monkeypatch: pytest.MonkeyPatch
    tmp_path: Path
    held: tuple[str, ...]
    params: Mapping[str, Any]


Driver = Callable[[Env], None]


def _task(env: Env, labels: tuple[str, ...] | None = None, **overrides: Any) -> Any:
    snapshot = env.params.get("snapshot", env.held) if labels is None else labels
    return make_test_task(ISSUE, status_labels=snapshot, **overrides)


def _active() -> Any:
    return make_test_active_worktree(ISSUE, pid=None)


def claim_apply_status_label(env: Env) -> None:
    assert claim_service._apply_status_label(env.forge, ISSUE, env.held) is None


def launch_yaml_error(env: Env) -> None:
    launch._apply_yaml_error_blocking([_task(env, yaml_error="bad")], env.config)


def launch_invalid_footprint(env: Env) -> None:
    task = _task(env, footprint_error="bad")
    launch._apply_invalid_footprint_blocking([task], env.config)


def launch_failure(env: Env) -> None:
    result = _fake(
        claim_id="claim" if env.params.get("claimed") else None,
        validation_error="bad" if env.params.get("validation") else None,
        error_message="boom",
    )
    launch._handle_launch_failure(_task(env), result, env.config)


def launch_success(env: Env) -> None:
    env.monkeypatch.setattr(
        launch, "_build_active_worktree_from_launch", lambda *a: _active()
    )
    env.monkeypatch.setattr(launch, "save_run_state", _NOOP)
    state = RunState(active_worktrees={})
    launch._record_successful_launch(
        _task(env), _fake(), _fake(), state, 1.0, env.config, None
    )


def attempt_reconcile(env: Env) -> None:
    module = launch_attempts
    existing = _fake(launch=_fake(launch_attempt_id="a1"))
    env.monkeypatch.setattr(module, "attempt_was_released", lambda *a: False)
    env.monkeypatch.setattr(module, "_recovery_allowed", lambda *a: True)
    env.monkeypatch.setattr(module, "_lookup_attempt", lambda attempt, *a: attempt)
    env.monkeypatch.setattr(module, "_load_or_recover_active", lambda *a: existing)
    env.monkeypatch.setattr(module, "save_run_state", _NOOP)
    env.config.dispatch_target = _fake(target_name="t")
    attempt = _fake(phase="launched", target="t", attempt_id="a1")
    task = _task(env)
    assert module.reconcile_attempt(attempt, task, RunState(), env.config) is True


def _reconciliation_ctx(env: Env) -> tuple[Any, Any, Any]:
    task = _task(env)
    env.monkeypatch.setattr(reconciliation, "_confirm_queued_recovery", _NOOP)
    env.monkeypatch.setattr(
        reconciliation, "completion_mutation_blocked_fresh", lambda *a: False
    )
    env.monkeypatch.setattr(reconciliation, "_has_pending_dependencies", _NOOP)
    ctx = make_test_cycle_context(tasks_by_issue={ISSUE: task}, config=env.config)
    return task, ctx, RunState(active_worktrees={})


def reconcile_blocked_recompute(env: Env) -> None:
    task, ctx, state = _reconciliation_ctx(env)
    issue = make_issue(ISSUE, labels=env.held)
    result = reconciliation._resolve_one_blocked_recompute_issue(
        issue, task, set(), ctx, state, env.config
    )
    assert result is not None


def reconcile_base_branch_red(env: Env) -> None:
    _, ctx, state = _reconciliation_ctx(env)
    decision = _fake(issue_number=ISSUE, subtask_id="task-a")
    reconciliation._apply_base_branch_red_requeue(
        decision, ctx, state, env.config, "old", "new"
    )


def rebase_notify_recompute(env: Env) -> None:
    conflict = _fake(
        subtask_id="a",
        other_subtask_id="b",
        reason="r",
        resources=(),
        similarity=0.5,
        blocked_subtask_id="c",
    )
    rebase.notify_recompute(conflict, "w", 100, True, {"c": ISSUE}, forge=env.forge)


def _rebase_ctx() -> Any:
    return _fake(run_state=_fake(active_worktrees={"k": 1}), key="k")


def rebase_wip_backup_failure(env: Env) -> None:
    env.monkeypatch.setattr(rebase.dispatch_gc, "backup_wip_commit", lambda *a: "err")
    assert not rebase._prepare_wip_backup_for_rebase(
        _active(), env.config, _rebase_ctx()
    )


def rebase_failure(env: Env) -> None:
    env.monkeypatch.setattr(rebase, "run_git", _NOOP)
    rebase._handle_rebase_failure(
        _active(), "parent", Exception("x"), env.config, _rebase_ctx()
    )


def zombie_requeue(env: Env) -> None:
    reclaim = _fake(
        active=_active(),
        reason="timeout",
        status_labels=env.params.get("snapshot", env.held),
        reclaim_count=1,
    )
    zombies._notify_requeued_reclaim(reclaim, env.config, "", lambda: None)


def _evidence() -> Any:
    return _fake(pr_number=9, base_ref="main")


def prior_parent_repair(env: Env) -> None:
    issue = make_issue(ISSUE, labels=env.params.get("snapshot", env.held))
    prior_parent_merge._apply_verified_repair(env.forge, issue, _evidence())


def prior_parent_normalize_closed(env: Env) -> None:
    issue = make_issue(ISSUE, labels=env.held, state="CLOSED")
    prior_parent_merge._normalize_closed_issue_label(env.forge, issue)


def _stub_completion(env: Env) -> None:
    module = completion
    env.monkeypatch.setattr(module, "fresh_external_hold", lambda *a, **k: None)
    for name in ("remove_worktree", "require_external_stop", "save_run_state"):
        env.monkeypatch.setattr(module, name, _NOOP)
    env.monkeypatch.setattr(module, "run_git", lambda *a, **k: _fake(stdout=""))


def _completion_task(env: Env) -> Any:
    snapshot = env.params.get("snapshot")
    return None if snapshot is None else _task(env, snapshot)


def completion_blocked_hold(env: Env) -> None:
    _stub_completion(env)
    completion._apply_blocked_hold(
        _active(),
        env.config,
        _completion_task(env),
        "comment",
        extra_label=env.params.get("extra_label"),
    )


def completion_requeue(env: Env) -> None:
    _stub_completion(env)
    completion._publish_requeue(
        _active(),
        _completion_task(env),
        env.config,
        RunState(active_worktrees={}),
        1.0,
        "comment",
        on_requeue_applied=lambda: None,
    )


def usage_limit_requeue(env: Env) -> None:
    from orchestune.dispatch.cycle_events import UsageLimitCompletion
    from orchestune.dispatch.gc import usage_limit

    for name in ("remove_worktree", "backup_wip_commit", "save_run_state"):
        env.monkeypatch.setattr(usage_limit, name, _NOOP)
    event = UsageLimitCompletion(
        issue_number=ISSUE,
        action="usage_limit_requeued",
        target="claude-cli",
        reset_known=False,
        retries_remaining=1,
        retry_at=1.0,
    )
    usage_limit._requeue(
        RunState(active_worktrees={}),
        str(ISSUE),
        _active(),
        _completion_task(env),
        env.config,
        1.0,
        None,
        event,
    )


def completion_done_cleanup(env: Env) -> None:
    _stub_completion(env)
    ctx = _fake(active=_active(), config=env.config, active_task=_completion_task(env))
    completion._apply_done_worktree_cleanup(ctx)


def completion_abandoned_reclaim(env: Env) -> None:
    _stub_completion(env)
    outcome = cloud_completion._handle_abandoned_cloud_reclaim(
        _active(), env.config, env.held, 1, lambda: None
    )
    assert outcome == "abandoned_pr_requeued"


def planned_transition_command(
    labels: tuple[str, ...], depends_on: tuple[str, ...], completed: tuple[str, ...]
) -> RepairCommand:
    """The transition command the real status-repair planner generates."""
    report = _evaluate(
        _observed(_task_scope(ISSUE, labels=labels)),
        _desired(
            _desired_task("status-policy", ISSUE, depends_on=depends_on),
            completed=completed,
        ),
    )
    commands = [
        command
        for command in plan_status_repairs(report)
        if command.code == COMMAND_TRANSITION_LABEL
    ]
    assert len(commands) == 1, commands
    return commands[0]


def status_repair_command(env: Env) -> None:
    command = planned_transition_command(
        env.held, env.params["depends_on"], env.params["completed"]
    )
    status_repair._apply_command(
        command,
        _task(env),
        _fake(intent_id="intent-1"),
        MagicMock(),
        env.config,
    )


def recovery_requeue(env: Env) -> None:
    module = recovery
    env.monkeypatch.setattr(
        module, "completion_subject_mutation_blocked", lambda *a: False
    )
    env.monkeypatch.setattr(
        module, "_reconcile_durable_attempt_for_requeue", lambda *a: False
    )
    env.monkeypatch.setattr(module, "_restorable", lambda *a: False)
    command = RepairCommand(
        code=COMMAND_REQUEUE,
        scope=ConsistencyScope.TASK,
        idempotency_key="requeue",
        subject_id=str(ISSUE),
    )
    snapshot = _fake(tasks_by_issue={}, restorations=[(str(ISSUE), None, _active())])
    result = module.execute_recovery_requeue_command(
        command,
        RunState(active_worktrees={}),
        snapshot,
        env.config,
    )
    assert result.status.value == env.params.get("expect", "applied"), result


def completion_abandoned_finalize(env: Env) -> CompletionEvent:
    """Guard path: an Issue that already needs a human is only reclaimed."""
    _stub_completion(env)
    env.monkeypatch.setattr(
        cloud_completion, "worktree_has_uncommitted_changes", lambda *a: False
    )
    event = cloud_completion._finalize_abandoned_cloud_worktree(
        _active(), _task(env), env.config
    )
    return event


def escalation(env: Env) -> None:
    apply_human_review_escalation(
        ISSUE,
        env.params["current"],
        "comment",
        forge=env.forge,
        on_label_applied=lambda: None,
    )


def integrator_restore_blocked_label(env: Env) -> None:
    AutoMergeChildIntegrationStep()._restore_blocked_label(
        _fake(forge=env.forge),
        ISSUE,
        env.params["current"],
    )


DRIVERS: dict[str, Driver] = {
    "claim_apply_status_label": claim_apply_status_label,
    "launch_yaml_error": launch_yaml_error,
    "launch_invalid_footprint": launch_invalid_footprint,
    "launch_failure": launch_failure,
    "launch_success": launch_success,
    "attempt_reconcile": attempt_reconcile,
    "reconcile_blocked_recompute": reconcile_blocked_recompute,
    "reconcile_base_branch_red": reconcile_base_branch_red,
    "rebase_notify_recompute": rebase_notify_recompute,
    "rebase_wip_backup_failure": rebase_wip_backup_failure,
    "rebase_failure": rebase_failure,
    "zombie_requeue": zombie_requeue,
    "usage_limit_requeue": usage_limit_requeue,
    "prior_parent_repair": prior_parent_repair,
    "prior_parent_normalize_closed": prior_parent_normalize_closed,
    "completion_blocked_hold": completion_blocked_hold,
    "completion_requeue": completion_requeue,
    "completion_done_cleanup": completion_done_cleanup,
    "completion_abandoned_reclaim": completion_abandoned_reclaim,
    "status_repair_command": status_repair_command,
    "recovery_requeue": recovery_requeue,
    "escalation": escalation,
    "integrator_restore_blocked_label": integrator_restore_blocked_label,
}
