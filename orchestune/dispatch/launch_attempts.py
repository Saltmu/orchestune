"""Fail-closed launch and recovery for cloud implementation workers (#818)."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from orchestune.dispatch.attempt_record import (
    LaunchAttempt,
    read_attempt,
    write_attempt,
)
from orchestune.dispatch.escalation import apply_human_review_escalation
from orchestune.dispatch.execution_profiles import resolve_task_execution_selection
from orchestune.dispatch.labels import (
    PRIMARY_STATUS_LABELS,
    TERMINAL_ESCALATION_LABELS,
    transition_status_label,
)
from orchestune.dispatch.state import ActiveWorktree, RunState, save_run_state
from orchestune.dispatch.targets import DispatchHandle, DispatchTarget
from orchestune.issue_parsing import recovery_counters_from_body
from orchestune.labels import StatusLabel

if TYPE_CHECKING:
    from orchestune.dispatch.config import DispatcherConfig
    from orchestune.dispatch.execution_profiles import ExecutionSelection
    from orchestune.forge import Forge
    from orchestune.models import Task


class LaunchOutcomeUnknown(RuntimeError):
    """Do not clean up a worktree or release quota after a possible launch."""


class LaunchPlan(Protocol):
    task: Task
    branch_name: str
    base_branch_for_state: str


def _recovery_allowed(task: Task, config: DispatcherConfig) -> bool:
    issue = config.resolved_forge.get_issue(task.issue_number)
    terminal = (*TERMINAL_ESCALATION_LABELS, StatusLabel.DONE, StatusLabel.NOT_NEEDED)
    return (
        issue is not None
        and issue.state == "OPEN"
        and not any(label in issue.labels for label in terminal)
    )


def active_from_attempt(
    attempt: LaunchAttempt, task: Task, config: DispatcherConfig
) -> ActiveWorktree:
    issue = config.resolved_forge.get_issue(task.issue_number)
    count, serial = recovery_counters_from_body(issue.body) if issue else (0, False)
    selection = resolve_task_execution_selection(task, config)
    return ActiveWorktree(
        issue_number=task.issue_number,
        branch=attempt.branch,
        worktree_path=str(
            Path(config.worktree_root) / attempt.branch.replace("/", "-")
        ),
        pid=None,
        started_at=attempt.started_at,
        declared_footprint=task.footprint,
        external_id=attempt.external_id,
        external_url=attempt.external_url,
        base_branch=attempt.base_branch,
        launch_attempt_id=attempt.attempt_id,
        launch_phase=attempt.phase,
        recompute_count=count,
        forced_serial=serial or StatusLabel.FORCE_SERIAL in task.status_labels,
        profile=selection.profile,
        model=selection.model,
        reasoning_effort=selection.reasoning_effort,
        selection_reason=selection.reason,
    )


def _hold(task: Task, config: DispatcherConfig, reason: str) -> None:
    print(
        f"Holding cloud launch for issue #{task.issue_number}: {reason}",
        file=sys.stderr,
    )
    labels = config.resolved_forge.get_issue_labels(task.issue_number)
    if StatusLabel.BLOCKED_HUMAN_REVIEW not in labels:
        apply_human_review_escalation(
            task.issue_number,
            labels,
            f"クラウド起動試行を保留しました: {reason}。既存実行の照合・終了確認が必要です。"
            " queuedへの変更だけでは再起動しません。",
            forge=config.resolved_forge,
        )


def _lookup_attempt(
    attempt: LaunchAttempt, task: Task, config: DispatcherConfig
) -> LaunchAttempt:
    target = config.dispatch_target
    assert target is not None
    if attempt.phase == "unknown" and target.launch_capabilities.lookup_by_attempt:
        handle = target.lookup_launch_attempt(attempt.attempt_id)
        if handle is not None and handle.external_id:
            resolved = replace(
                attempt,
                phase="launched",
                external_id=handle.external_id,
                external_url=handle.external_url,
            )
            write_attempt(
                config.resolved_forge, task.issue_number, resolved, expected=attempt
            )
            return resolved
    return attempt


def reconcile_attempt(
    attempt: LaunchAttempt, task: Task, state: RunState, config: DispatcherConfig
) -> bool:
    """True means the journal consumed the task; never launch it as queued."""
    if not _recovery_allowed(task, config):
        return True
    target = config.dispatch_target
    if target is None or attempt.target != target.target_name:
        _hold(task, config, f"attempt {attempt.attempt_id}: provider changed")
        return True
    if attempt.phase == "prepared":
        return False
    try:
        attempt = _lookup_attempt(attempt, task, config)
    except Exception as exc:
        _hold(task, config, f"attempt {attempt.attempt_id}: lookup failed ({exc})")
        return True
    if attempt.phase != "launched":
        _hold(
            task,
            config,
            f"attempt {attempt.attempt_id}: unknown launch result; provider cannot reconcile",
        )
        return True
    key = str(task.issue_number)
    existing = state.active_worktrees.get(key)
    if existing is None:
        state.active_worktrees[key] = active_from_attempt(attempt, task, config)
    elif existing.launch_attempt_id != attempt.attempt_id:
        _hold(task, config, "local state belongs to a different launch attempt")
        return True
    save_run_state(
        state, config.run_state_path, launch_window_seconds=config.window_seconds
    )
    labels = config.resolved_forge.get_issue_labels(task.issue_number)
    transition_status_label(
        config.resolved_forge,
        task.issue_number,
        StatusLabel.IN_PROGRESS,
        (label for label in PRIMARY_STATUS_LABELS if label in labels),
    )
    return True


class JournaledDispatchTarget(DispatchTarget):
    """Journal immediately around the provider boundary, after worktree setup."""

    def __init__(
        self,
        target: DispatchTarget,
        attempt: LaunchAttempt,
        forge: Forge,
        commit: Callable[[], None],
    ):
        self.target = target
        self.attempt = attempt
        self.forge = forge
        self.commit = commit

    def launch(
        self,
        task: Task,
        branch_name: str,
        worktree_path: Path,
        *,
        force_push: bool = False,
        execution_selection: ExecutionSelection | None = None,
        base_branch: str | None = None,
    ) -> DispatchHandle:
        unknown = replace(self.attempt, phase="unknown", started_at=time.time())
        # A write may have committed even if its response was lost. Preserve quota.
        self.commit()
        try:
            write_attempt(self.forge, task.issue_number, unknown, expected=self.attempt)
            handle = self.target.launch_attempt(
                unknown.attempt_id,
                task,
                branch_name,
                worktree_path,
                force_push=force_push,
                execution_selection=execution_selection,
                base_branch=base_branch,
            )
            if not handle.external_id:
                raise ValueError("provider returned no execution handle")
            launched = replace(
                unknown,
                phase="launched",
                external_id=handle.external_id,
                external_url=handle.external_url,
            )
            write_attempt(self.forge, task.issue_number, launched, expected=unknown)
        except Exception as exc:
            raise LaunchOutcomeUnknown(str(exc)) from exc
        return replace(handle, launch_attempt_id=launched.attempt_id)

    def is_complete(self, handle: DispatchHandle, forge: Forge | None = None) -> bool:
        return self.target.is_complete(handle, forge=forge)


def prepare_journaled_target(
    plan: LaunchPlan,
    state: RunState,
    now: float,
    config: DispatcherConfig,
    commit: Callable[[], None],
) -> DispatchTarget | None:
    target = config.dispatch_target
    assert target is not None
    if not target.launch_capabilities.durable_attempt:
        return target
    if not _recovery_allowed(plan.task, config):
        return None
    try:
        attempt = read_attempt(config.resolved_forge, plan.task.issue_number)
    except ValueError as exc:
        _hold(plan.task, config, str(exc))
        return None
    if attempt is not None:
        if reconcile_attempt(attempt, plan.task, state, config):
            return None
        if (
            attempt.branch != plan.branch_name
            or attempt.base_branch != plan.base_branch_for_state
        ):
            _hold(plan.task, config, "prepared attempt branch changed")
            return None
    else:
        attempt = LaunchAttempt(
            str(uuid4()),
            "prepared",
            target.target_name or "unknown",
            plan.branch_name,
            plan.base_branch_for_state,
            now,
        )
        write_attempt(
            config.resolved_forge, plan.task.issue_number, attempt, expected=None
        )
    return JournaledDispatchTarget(target, attempt, config.resolved_forge, commit)
