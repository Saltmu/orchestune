"""Apply a claude-cli session-limit exit without spending the reclaim budget (#1270).

The pure parts (what the message looks like, when the limit lifts, how many extra
launches are allowed) live in ``orchestune.dispatch.usage_limit`` and
``orchestune.dispatch.retry_policy``. This adapter owns the effects, in this order:

1. read a bounded tail of *this run's* log range (recorded at launch);
2. persist the dedicated retry reservation and the target cooldown;
3. only then back up the work, retire the worktree and publish ``status:queued``.

A failure before step 3 leaves the label, the worktree and the active entry untouched.
A failure inside step 3 keeps the reservation ``pending`` under the same run identity, so
the next cycle resumes it instead of counting the same failed run twice.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from orchestune.consistency.invariants.execution import LOCAL_PROCESS_DEAD
from orchestune.consistency.models import RepairCommand, RepairResult, RepairStatus
from orchestune.consistency.repairs.execution import COMMAND_RECLAIM
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_events import (
    CompletionEvent,
    UsageLimitAction,
    UsageLimitCompletion,
)
from orchestune.dispatch.execution_repair import command_finding_codes
from orchestune.dispatch.gc.completion import _local_pr_completion_status
from orchestune.dispatch.gc.external_guard import fresh_external_hold
from orchestune.dispatch.gc.git import backup_wip_commit, remove_worktree
from orchestune.dispatch.gc.outcome_decision import (
    _is_handoff_ready,
    read_retry_state,
    write_retry_state,
)
from orchestune.dispatch.retry_policy import (
    RetryDisposition,
    RetryPlan,
    RetryState,
    usage_limit_policy,
)
from orchestune.dispatch.usage_limit import (
    LOG_TAIL_MAX_BYTES,
    ResetResolution,
    detect_usage_limit,
    plan_usage_limit_retry,
    resolve_reset,
    sanitize_log_tail,
)
from orchestune.infra.process_utils import is_process_alive
from orchestune.labels import StatusLabel
from orchestune.ledger.active_lifecycle import has_completion_reservation
from orchestune.ledger.active_records import ActiveWorktree
from orchestune.ledger.escalation import apply_human_review_escalation
from orchestune.ledger.run_state import RunState, TaskReclaimRecord, save_run_state
from orchestune.ledger.status_labels import (
    PRIMARY_STATUS_LABELS,
    transition_status_label,
)
from orchestune.models import PrRecord
from orchestune.task_metadata import TaskMetadata

#: The only target whose termination is classified. Other targets keep their handling.
CLAUDE_CLI_TARGET = "claude-cli"
_BACKUP_MESSAGE = "WIP: backup by Orchestune GC (claude-cli session usage limit)"


@dataclass(frozen=True)
class _Finding:
    reset: ResetResolution


def run_identity(active: ActiveWorktree) -> str:
    """Identify one execution (claim + process + start) of a task."""
    launch = active.launch
    return f"{active.claim.claim_id or ''}:{launch.pid}:{launch.started_at}"


def _is_candidate(active: ActiveWorktree) -> bool:
    launch = active.launch
    return bool(
        active.claim.owner_kind != "interactive"
        and launch.external_id is None
        and launch.pid is not None
        and launch.launch_target == CLAUDE_CLI_TARGET
        and launch.launch_log_path
        and launch.launch_log_offset is not None
        and launch.launch_log_offset >= 0
    )


def _read_run_log(active: ActiveWorktree) -> tuple[str, float] | None:
    """Bounded, sanitized tail of this run's log range, or ``None`` when unknowable."""
    launch = active.launch
    if launch.launch_log_path is None or launch.launch_log_offset is None:
        return None
    path = Path(launch.launch_log_path)
    try:
        info = os.stat(path)
        # A FIFO or device would block or lie; only a regular file is a run log.
        if not stat.S_ISREG(info.st_mode) or info.st_size < launch.launch_log_offset:
            return None
        start = max(launch.launch_log_offset, info.st_size - LOG_TAIL_MAX_BYTES)
        with open(path, "rb") as handle:
            handle.seek(start)
            raw = handle.read(info.st_size - start)
    except OSError:
        return None
    return sanitize_log_tail(raw), info.st_mtime


def _classify(
    active: ActiveWorktree,
    record: TaskReclaimRecord | None,
    config: DispatcherConfig,
    now: float,
) -> _Finding | None:
    if not _is_candidate(active) or is_process_alive(active.launch.pid):
        return None
    log = _read_run_log(active)
    if log is None:
        # A reservation already saved for this very run is resumed even if the log
        # has since been rotated: the earlier read is what the ledger remembers.
        if (
            record is not None
            and record.usage_limit_retry_pending
            and record.usage_limit_retry_run == run_identity(active)
        ):
            return _Finding(ResetResolution(None, None, "resumed from the ledger"))
        return None
    text, written_at = log
    signal = detect_usage_limit(text)
    if signal is None:
        return None
    reset = resolve_reset(
        signal,
        now=now,
        anchor=min(written_at, now),
        timezone=config.usage_limit_timezone,
    )
    return _Finding(reset)


def _stale_labels(active_task: TaskMetadata | None) -> tuple[str, ...]:
    if active_task is not None:
        return tuple(
            label
            for label in PRIMARY_STATUS_LABELS
            if label in active_task.status_labels
        )
    return (StatusLabel.IN_PROGRESS,)


def _cooldown_until(
    plan: RetryPlan,
    reset: ResetResolution,
    state: RetryState,
    config: DispatcherConfig,
    now: float,
) -> float:
    if plan.disposition is not RetryDisposition.EXHAUSTED:
        return plan.state.retry_at
    if reset.reset_at is not None:
        return reset.reset_at + config.usage_limit_reset_grace_seconds
    return now + float(config.usage_limit_backoff_seconds * (2**state.count))


def _event(
    action: UsageLimitAction,
    active: ActiveWorktree,
    active_task: TaskMetadata | None,
    reset: ResetResolution,
    plan: RetryPlan,
    config: DispatcherConfig,
    reason: str | None = None,
) -> UsageLimitCompletion:
    state = plan.state
    remaining = (
        0
        if plan.disposition is RetryDisposition.EXHAUSTED
        else max(config.max_usage_limit_retries - state.count, 0)
    )
    return UsageLimitCompletion(
        issue_number=active.core.issue_number,
        subtask_id=active_task.subtask_id if active_task else None,
        action=action,
        target=CLAUDE_CLI_TARGET,
        reset_known=reset.known,
        reset_at=reset.reset_at,
        timezone=reset.timezone,
        retry_at=(
            None if plan.disposition is RetryDisposition.EXHAUSTED else state.retry_at
        ),
        retries_remaining=remaining,
        reason=reason,
    )


def _persist(
    run_state: RunState,
    config: DispatcherConfig,
    now: float,
    open_prs: Sequence[PrRecord] | None,
) -> bool:
    try:
        save_run_state(
            run_state,
            config.run_state_path,
            now=now,
            launch_window_seconds=config.window_seconds,
            open_prs=open_prs,
        )
    except Exception as error:  # noqa: BLE001 - fail closed: reconsidered next cycle
        print(
            f"Warning: could not persist the session-limit state to "
            f"{config.run_state_path}: {error}",
            file=sys.stderr,
        )
        return False
    return True


def _describe_wait(event: UsageLimitCompletion) -> str:
    if event.retry_at is None:
        return ""
    when = f"Unix時刻 {event.retry_at:.0f} 以降"
    if event.reset_known:
        zone = f"（{event.timezone}で解釈）" if event.timezone else ""
        return f"リセット時刻が確定したため、{when}に再投入します{zone}。"
    return f"リセット時刻を特定できなかったため、有限の指数バックオフ後（{when}）に再投入します。"


def _requeue_comment(event: UsageLimitCompletion) -> str:
    return (
        "claude-cli がセッション使用上限（session usage limit）に達して終了しました。"
        "通常の回収・早期終了・レビュータイムアウトの回数は消費していません。\n"
        f"{_describe_wait(event)}\n"
        f"自動再投入の残り回数: {event.retries_remaining}回。"
        "作業ブランチ上の未コミット変更はWIPコミットとして退避済みです。"
    )


def _escalation_comment(event: UsageLimitCompletion, config: DispatcherConfig) -> str:
    return (
        "claude-cli がセッション使用上限（session usage limit）で終了し、"
        f"自動再投入の上限（max_usage_limit_retries={config.max_usage_limit_retries}）を"
        "使い切ったため、`status:blocked-human-review`へ遷移しました。\n"
        "通常の回収回数（max_task_reclaims）は消費していません。"
        "作業中の変更を失わないよう、worktreeは削除せずに残しています。\n"
        "上限が解除された後に`status:queued`へ再設定してください。"
    )


@dataclass
class _Reservation:
    """The in-memory change of one reservation, restorable if it cannot be saved."""

    ledger: RunState
    issue: int
    target: str
    record: TaskReclaimRecord | None
    cooldown: float | None

    @classmethod
    def snapshot(cls, run_state: RunState, issue: int, target: str) -> _Reservation:
        record = run_state.task_reclaim_counts.get(issue)
        return cls(
            run_state,
            issue,
            target,
            replace(record) if record is not None else None,
            run_state.usage_limit_cooldowns.get(target),
        )

    def restore(self) -> None:
        if self.record is None:
            self.ledger.task_reclaim_counts.pop(self.issue, None)
        else:
            self.ledger.task_reclaim_counts[self.issue] = self.record
        if self.cooldown is None:
            self.ledger.usage_limit_cooldowns.pop(self.target, None)
        else:
            self.ledger.usage_limit_cooldowns[self.target] = self.cooldown


def _planned_state(record: TaskReclaimRecord | None, run: str) -> RetryState:
    """The persisted state, ignoring a reservation that belongs to another run."""
    state = read_retry_state(record, "usage_limit")
    own = record is not None and record.usage_limit_retry_run == run
    if state.pending and not own:
        state = replace(state, pending=False)
    return state


def _reserve(
    run_state: RunState,
    issue: int,
    run: str,
    plan: RetryPlan,
    cooldown_until: float,
    target: str,
) -> None:
    if plan.disposition is not RetryDisposition.EXHAUSTED:
        record = run_state.task_reclaim_counts.get(issue) or TaskReclaimRecord()
        write_retry_state(record, "usage_limit", plan.state)
        record.usage_limit_retry_run = run
        run_state.task_reclaim_counts[issue] = record
    current = run_state.usage_limit_cooldowns.get(target, 0.0)
    run_state.usage_limit_cooldowns[target] = max(current, cooldown_until)


def handle_usage_limit_exit(
    run_state: RunState,
    key: str,
    active: ActiveWorktree,
    active_task: TaskMetadata | None,
    config: DispatcherConfig,
    *,
    now: float,
    open_prs: Sequence[PrRecord] | None = None,
) -> CompletionEvent | None:
    """Handle a dead claude-cli process that ended on its session limit.

    Returns ``None`` when the exit is not (provably) a session limit, in which case the
    caller keeps its existing handling. The caller has already ruled out a completion
    reservation, a hand-off, a recorded Outcome and a merged PR.
    """
    issue = active.core.issue_number
    record = run_state.task_reclaim_counts.get(issue)
    finding = _classify(active, record, config, now)
    if finding is None:
        return None
    run = run_identity(active)
    state = _planned_state(record, run)
    policy = usage_limit_policy(
        config.max_usage_limit_retries, config.usage_limit_backoff_seconds
    )
    plan = plan_usage_limit_retry(
        policy,
        state,
        finding.reset,
        grace_seconds=config.usage_limit_reset_grace_seconds,
        now=now,
    )
    cooldown_until = _cooldown_until(plan, finding.reset, state, config, now)
    exhausted = plan.disposition is RetryDisposition.EXHAUSTED
    action: UsageLimitAction = (
        "usage_limit_escalated" if exhausted else "usage_limit_requeued"
    )
    event = _event(action, active, active_task, finding.reset, plan, config)
    if not config.apply:
        return event

    held = _hold_if_external(active, config, run_state, event)
    if held is not None:
        return held
    snapshot = _Reservation.snapshot(run_state, issue, CLAUDE_CLI_TARGET)
    _reserve(run_state, issue, run, plan, cooldown_until, CLAUDE_CLI_TARGET)
    if not _persist(run_state, config, now, open_prs):
        snapshot.restore()
        return replace(event, action="usage_limit_held", reason="ledger_save_failed")
    if exhausted:
        return _escalate(
            run_state, key, active, active_task, config, now, open_prs, event
        )
    return _requeue(run_state, key, active, active_task, config, now, open_prs, event)


def _hold_if_external(
    active: ActiveWorktree,
    config: DispatcherConfig,
    run_state: RunState,
    event: UsageLimitCompletion,
) -> CompletionEvent | None:
    hold = fresh_external_hold(active, config, "completion", run_state)
    return None if hold is None else hold.event()


def _settle(
    run_state: RunState,
    key: str,
    issue: int,
    config: DispatcherConfig,
    now: float,
    open_prs: Sequence[PrRecord] | None,
) -> None:
    record = run_state.task_reclaim_counts.get(issue)
    if record is not None:
        record.usage_limit_retry_pending = False
    run_state.active_worktrees.pop(key, None)
    _persist(run_state, config, now, open_prs)


def _retire_worktree(active: ActiveWorktree) -> bool:
    """Back the work up as a WIP commit, then remove the worktree directory.

    ``False`` keeps everything in place (the backup could not be made).
    """
    path = active.core.worktree_path
    if not os.path.exists(path):
        return True
    backup_error = backup_wip_commit(path, _BACKUP_MESSAGE)
    if backup_error is not None:
        print(
            f"Warning: kept issue #{active.core.issue_number}'s worktree: WIP backup "
            f"failed: {backup_error}",
            file=sys.stderr,
        )
        return False
    remove_worktree(path)
    return True


def _requeue(
    run_state: RunState,
    key: str,
    active: ActiveWorktree,
    active_task: TaskMetadata | None,
    config: DispatcherConfig,
    now: float,
    open_prs: Sequence[PrRecord] | None,
    event: UsageLimitCompletion,
) -> CompletionEvent:
    issue = active.core.issue_number
    settled = False

    def on_label_added() -> None:
        nonlocal settled
        _settle(run_state, key, issue, config, now, open_prs)
        settled = True

    try:
        if not _retire_worktree(active):
            return replace(event, action="usage_limit_held", reason="wip_backup_failed")
        transition_status_label(
            config.resolved_forge,
            issue,
            StatusLabel.QUEUED,
            _stale_labels(active_task),
            on_label_added=on_label_added,
        )
    except Exception as error:  # noqa: BLE001 - the reservation stays pending
        if not settled:
            print(
                f"Warning: could not requeue issue #{issue} after a session limit: "
                f"{error}",
                file=sys.stderr,
            )
            return replace(event, action="usage_limit_held", reason="requeue_failed")
        print(
            f"Warning: requeued issue #{issue} but a later step failed: {error}",
            file=sys.stderr,
        )
    _post_comment(config, issue, _requeue_comment(event))
    return event


def _escalate(
    run_state: RunState,
    key: str,
    active: ActiveWorktree,
    active_task: TaskMetadata | None,
    config: DispatcherConfig,
    now: float,
    open_prs: Sequence[PrRecord] | None,
    event: UsageLimitCompletion,
) -> CompletionEvent:
    issue = active.core.issue_number
    settled = False

    def on_label_applied() -> None:
        nonlocal settled
        _settle(run_state, key, issue, config, now, open_prs)
        settled = True

    path = active.core.worktree_path
    if os.path.exists(path):
        backup_error = backup_wip_commit(path, _BACKUP_MESSAGE)
        if backup_error is not None:
            print(
                f"Warning: WIP backup of issue #{issue} failed before escalation: "
                f"{backup_error}",
                file=sys.stderr,
            )
    try:
        apply_human_review_escalation(
            issue,
            _stale_labels(active_task),
            _escalation_comment(event, config),
            forge=config.resolved_forge,
            on_label_applied=on_label_applied,
        )
    except Exception as error:  # noqa: BLE001 - retried on the next cycle
        if not settled:
            print(
                f"Warning: could not escalate issue #{issue} after a session limit: "
                f"{error}",
                file=sys.stderr,
            )
            return replace(event, action="usage_limit_held", reason="escalation_failed")
    return event


def _post_comment(config: DispatcherConfig, issue: int, body: str) -> None:
    try:
        config.resolved_forge.add_comment(issue, body)
    except Exception as error:  # noqa: BLE001 - never undo a confirmed requeue
        print(
            f"Warning: requeued issue #{issue} but could not post the session-limit "
            f"notice: {error}",
            file=sys.stderr,
        )


def _active_for_subject(
    run_state: RunState, subject_id: str
) -> tuple[str, ActiveWorktree] | None:
    return next(
        (
            (key, active)
            for key, active in run_state.active_worktrees.items()
            if str(active.core.issue_number) == subject_id
        ),
        None,
    )


def handle_usage_limit_reclaim(
    command: RepairCommand,
    run_state: RunState,
    tasks_by_issue: Mapping[int, TaskMetadata],
    config: DispatcherConfig,
    events: list[CompletionEvent],
    open_prs: Sequence[PrRecord] | None,
    held_worktree_paths: frozenset[str],
    now: float,
) -> RepairResult | None:
    """Divert a typed ``LOCAL_PROCESS_DEAD`` reclaim that is really a session limit.

    The normal reclaim would spend ``max_task_reclaims`` on an exit that is not a
    crash. ``None`` means "not a session limit": the caller runs its normal reclaim.
    """
    if (
        command.code != COMMAND_RECLAIM
        or command.subject_id is None
        or LOCAL_PROCESS_DEAD not in command_finding_codes(command)
    ):
        return None
    found = _active_for_subject(run_state, command.subject_id)
    if found is None:
        return None
    key, active = found
    if (
        active.core.worktree_path in held_worktree_paths
        or has_completion_reservation(active)
        or _is_handoff_ready(active)
        or _local_pr_completion_status(active, config) != "pending"
    ):
        return None
    event = handle_usage_limit_exit(
        run_state,
        key,
        active,
        tasks_by_issue.get(active.core.issue_number),
        config,
        now=now,
        open_prs=open_prs,
    )
    if event is None:
        return None
    events.append(event)
    applied = config.apply and getattr(event, "action", "") != "usage_limit_held"
    return RepairResult(
        command=command,
        status=RepairStatus.APPLIED if applied else RepairStatus.SKIPPED,
        diagnostics=(f"session usage limit: {getattr(event, 'action', '')}",),
    )
