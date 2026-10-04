"""Completion handling for abandoned cloud executions."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import Literal, cast

from orchestune.bounded_limit import exceeds_limit
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_events import (
    AbandonedExternalExecutionHeldCompletion,
    CompletionEvent,
    TaskWorktreeCompletion,
)
from orchestune.dispatch.gc.completion_contracts import ForgeFailure, warn_forge_failure
from orchestune.dispatch.gc.external_guard import fresh_external_hold
from orchestune.dispatch.gc.git import remove_worktree, worktree_has_uncommitted_changes
from orchestune.dispatch.targets import DispatchHandle
from orchestune.labels import StatusLabel
from orchestune.ledger.escalation import apply_human_review_escalation
from orchestune.ledger.run_state import ActiveWorktree, RunState, TaskReclaimRecord
from orchestune.ledger.status_labels import (
    PRIMARY_STATUS_LABELS,
    TERMINAL_ESCALATION_LABELS,
    transition_status_label,
)
from orchestune.task_metadata import TaskMetadata


def _active_dispatch_handle(active: ActiveWorktree) -> DispatchHandle:
    return DispatchHandle(
        pid=active.launch.pid,
        external_id=active.launch.external_id,
        external_url=active.launch.external_url,
        branch_name=active.core.branch,
        issue_number=active.core.issue_number,
        started_at=active.launch.started_at,
    )


def _call_is_complete(config: DispatcherConfig, handle: DispatchHandle) -> bool:
    assert config.dispatch_target is not None
    try:
        return config.dispatch_target.is_complete(handle, forge=config.resolved_forge)
    except TypeError:
        return config.dispatch_target.is_complete(handle)


def _cloud_worktree_completion_status(
    active: ActiveWorktree,
    config: DispatcherConfig,
    error_sink: list[ForgeFailure] | None = None,
) -> str:
    assert config.dispatch_target is not None
    handle = _active_dispatch_handle(active)
    try:
        status = config.dispatch_target.completion_status(
            handle, forge=config.resolved_forge
        )
    except Exception as error:  # noqa: BLE001 - 判定を保留し、事実だけ表に出す
        warn_forge_failure(
            "completion_status", active.core.issue_number, error, error_sink
        )
        return "unknown"
    if isinstance(status, str):
        return status
    return "completed" if _call_is_complete(config, handle) else "pending"


def _reserve_cloud_reclaim_record(
    issue_number: int,
    run_state: RunState | None,
    on_reclaim_reserved: Callable[[], None] | None = None,
) -> int:
    if run_state is None:
        return 1
    previous = run_state.task_reclaim_counts.get(issue_number)
    count = (
        1
        if previous is None
        else (previous.count if previous.pending else previous.count + 1)
    )
    run_state.task_reclaim_counts[issue_number] = TaskReclaimRecord(
        count=count, last_reclaimed_at=time.time(), pending=True
    )
    if on_reclaim_reserved is not None:
        try:
            on_reclaim_reserved()
        except Exception:
            if previous is None:
                run_state.task_reclaim_counts.pop(issue_number, None)
            else:
                run_state.task_reclaim_counts[issue_number] = previous
            raise
    return count


def _handle_abandoned_cloud_reclaim(
    active: ActiveWorktree,
    config: DispatcherConfig,
    status_labels: tuple[str, ...],
    reclaim_count: int,
    on_settle: Callable[[], None],
) -> str:
    if fresh_external_hold(active, config, "completion") is not None:
        return "external_execution_held"
    remove_worktree(active.core.worktree_path)
    if exceeds_limit(reclaim_count, config.max_task_reclaims):
        msg = (
            "タスクのPRがクローズされたか、Cloudタスクの失敗により回収を行いました。\n"
            f"回収・再投入の累計回数が上限（max_task_reclaims={config.max_task_reclaims}）を超えた"
            f"（今回で{reclaim_count}回目）ため、status:queuedへの再投入を打ち切り、"
            "status:blocked-human-reviewへ遷移しました。\nタスクの実装方針や実行環境を確認してください。"
        )
        apply_human_review_escalation(
            active.core.issue_number,
            status_labels,
            msg,
            forge=config.resolved_forge,
            on_label_applied=on_settle,
        )
        return "escalated_reclaim_limit_exceeded"
    stale_labels = tuple(
        label for label in PRIMARY_STATUS_LABELS if label in status_labels
    )
    transition_status_label(
        config.resolved_forge,
        active.core.issue_number,
        StatusLabel.QUEUED,
        stale_labels,
        on_label_added=on_settle,
    )
    try:
        config.resolved_forge.add_comment(
            active.core.issue_number,
            "タスクのPRがマージされずにクローズされたか、Cloudタスクが終了したため、完了扱いにはせず、"
            f"GCによりタスクを再キューイング（status:queued）しました（回収{reclaim_count}回目 / 上限{config.max_task_reclaims}回）。",
        )
    except Exception as error:
        print(
            f"Warning: requeued issue #{active.core.issue_number} but failed to post comment: {error}",
            file=sys.stderr,
        )
    return "abandoned_pr_requeued"


def _task_completion(
    active: ActiveWorktree, subtask_id: str, action: str
) -> TaskWorktreeCompletion:
    return TaskWorktreeCompletion(
        issue_number=active.core.issue_number,
        subtask_id=subtask_id,
        worktree_path=active.core.worktree_path,
        action=cast(
            Literal[
                "not_needed",
                "not_needed_review_dispatched",
                "abandoned_pr_requeued",
                "completion_skipped_dirty_worktree",
                "escalated_reclaim_limit_exceeded",
            ],
            action,
        ),
    )


def _requeue_abandoned_cloud_worktree(
    active: ActiveWorktree,
    subtask_id: str,
    config: DispatcherConfig,
    run_state: RunState | None,
    status_labels: tuple[str, ...],
    on_label_applied: Callable[[], None] | None,
    on_reclaim_reserved: Callable[[], None] | None,
) -> CompletionEvent:
    reclaim_count = _reserve_cloud_reclaim_record(
        active.core.issue_number, run_state, on_reclaim_reserved
    )

    def settle() -> None:
        if run_state is not None:
            record = run_state.task_reclaim_counts.get(active.core.issue_number)
            if record is not None:
                record.pending = False
        if on_label_applied is not None:
            on_label_applied()

    action = _handle_abandoned_cloud_reclaim(
        active, config, status_labels, reclaim_count, settle
    )
    if action == "external_execution_held":
        return AbandonedExternalExecutionHeldCompletion(
            issue_number=active.core.issue_number,
            subtask_id=subtask_id,
            worktree_path=active.core.worktree_path,
        )
    return _task_completion(active, subtask_id, action)


def _finalize_abandoned_cloud_worktree(
    active: ActiveWorktree,
    active_task: TaskMetadata | None,
    config: DispatcherConfig,
    run_state: RunState | None = None,
    on_label_applied: Callable[[], None] | None = None,
    on_reclaim_reserved: Callable[[], None] | None = None,
) -> CompletionEvent:
    if hold := fresh_external_hold(active, config, "completion", run_state):
        return hold.event()
    subtask_id = active_task.subtask_id if active_task else ""
    if worktree_has_uncommitted_changes(active.core.worktree_path):
        return TaskWorktreeCompletion(
            issue_number=active.core.issue_number,
            subtask_id=subtask_id,
            worktree_path=active.core.worktree_path,
            action="completion_skipped_dirty_worktree",
        )
    if not config.apply:
        return _task_completion(active, subtask_id, "abandoned_pr_requeued")
    status_labels = (
        active_task.status_labels if active_task else (StatusLabel.IN_PROGRESS,)
    )
    if any(label in status_labels for label in TERMINAL_ESCALATION_LABELS):
        if hold := fresh_external_hold(active, config, "completion", run_state):
            return hold.event()
        remove_worktree(active.core.worktree_path)
        config.resolved_forge.add_comment(
            active.core.issue_number,
            "タスクのPRがマージされずにクローズされたためworktreeを回収しました。"
            "既に人間の確認が必要な状態のため、status:*ラベルは変更していません。",
        )
        return _task_completion(active, subtask_id, "abandoned_pr_requeued")
    return _requeue_abandoned_cloud_worktree(
        active,
        subtask_id,
        config,
        run_state,
        status_labels,
        on_label_applied,
        on_reclaim_reserved,
    )
