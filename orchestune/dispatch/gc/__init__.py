"""Active-worktree GC rule-chain orchestration.

Implementation helpers are re-exported for backward-compatible imports.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from orchestune.bounded_limit import exceeds_limit
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.escalation import apply_human_review_escalation
from orchestune.dispatch.gc.completion import (
    CompletedWorktreeDecision,
    ForgeFailure,
    _active_dispatch_handle,
    _apply_completed_worktree_outcome,
    _call_is_complete,
    _cloud_worktree_completion_status,
    _decide_completed_worktree_outcome,
    _decide_not_needed_dirty_worktree,
    _finalize_abandoned_cloud_worktree,
    _finalize_completed_worktree,
    _finalize_not_needed_worktree,
    _is_stale_pr_for_active,
    _is_worktree_complete,
    _local_pr_completion_status,
    _parse_github_timestamp,
    failed_operations,
    failure_descriptions,
    is_completion_hold_event,
    warn_forge_failure,
)
from orchestune.dispatch.gc.git import (
    backup_wip_commit,
    remote_branch_commit_sha_if_ahead,
    remove_worktree,
    worktree_has_new_commits,
    worktree_has_uncommitted_changes,
)
from orchestune.dispatch.gc.zombies import (
    ZombieOrTimeoutReclaim,
    _apply_zombie_or_timeout_reclaim,
)
from orchestune.dispatch.rules import ActiveWorktreeRuleOutcome, CycleContext
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import (
    ActiveWorktree,
    CompletedWorktree,
    RunState,
    TaskReclaimRecord,
    save_run_state,
)
from orchestune.infra.process_utils import is_process_alive
from orchestune.labels import StatusLabel
from orchestune.models import PrRecord, Usage
from orchestune.outcome_record import RESULT_NOT_NEEDED, parse_from_comments

__all__ = [
    "CompletedWorktreeDecision",
    "ZombieOrTimeoutReclaim",
    "_active_dispatch_handle",
    "_apply_completed_worktree_outcome",
    "_apply_zombie_or_timeout_reclaim",
    "_call_is_complete",
    "_cloud_worktree_completion_status",
    "_decide_completed_worktree_outcome",
    "_decide_not_needed_dirty_worktree",
    "_finalize_abandoned_cloud_worktree",
    "_finalize_completed_worktree",
    "_finalize_not_needed_worktree",
    "_is_stale_pr_for_active",
    "_is_worktree_complete",
    "_local_pr_completion_status",
    "_parse_github_timestamp",
    "is_completion_hold_event",
    "backup_wip_commit",
    "is_process_alive",
    "remote_branch_commit_sha_if_ahead",
    "remove_worktree",
    "worktree_has_new_commits",
    "worktree_has_uncommitted_changes",
]


def _rule_not_needed(
    ctx: CycleContext, key: str, active: ActiveWorktree, active_task: Task | None
) -> ActiveWorktreeRuleOutcome | None:
    """#280/#552: status:not-neededラベルまたはoutcome(not-needed)検知による即時完了処理。

    セッションが「対応不要」と判断した場合、コミット・PRを作らないため
    closingIssuesReferences等の完了シグナルが発生せず、`_rule_completed`
    （PID/PR存在ベース）は永遠にマッチしない。ラベルまたはoutcome検知を最優先の
    完了シグナルとして扱い、stale判定より先に評価する。
    """
    has_not_needed_label = (
        active_task is not None and StatusLabel.NOT_NEEDED in active_task.status_labels
    )
    has_not_needed_outcome = False
    if not has_not_needed_label:
        try:
            comments = ctx.config.resolved_forge.list_comments(active.issue_number)
            outcome = parse_from_comments(comments, since=active.started_at)
            has_not_needed_outcome = (
                outcome is not None and outcome.result == RESULT_NOT_NEEDED
            )
        except Exception:
            pass

    if not has_not_needed_label and not has_not_needed_outcome:
        return None
    completion_event = _finalize_not_needed_worktree(
        active, active_task, ctx.config, ctx.not_needed_review_dispatcher
    )
    completed_subtask_id = None
    if completion_event["action"] in ("not_needed", "not_needed_review_dispatched"):
        if active_task and active_task.subtask_id:
            completed_subtask_id = active_task.subtask_id
        if ctx.config.apply:
            del ctx.run_state.active_worktrees[key]
    return ActiveWorktreeRuleOutcome(
        completion_event=completion_event,
        completed_subtask_id=completed_subtask_id,
        terminal=True,
    )


def _rule_stale_entry_hold(
    ctx: CycleContext, key: str, active: ActiveWorktree, active_task: Task | None
) -> ActiveWorktreeRuleOutcome | None:
    """Leave cached stale entries untouched until Supervisor-owned GC runs.

    The early reconciliation chain still has to stop completion/rebase rules
    from acting on an entry whose Issue is no longer in progress.  This rule is
    deliberately non-mutating: the typed ``execution.reclaim`` handler owns
    live Forge revalidation and every cleanup side effect later in the cycle.
    """
    del ctx, key, active
    if active_task is None or StatusLabel.IN_PROGRESS in active_task.status_labels:
        return None
    return ActiveWorktreeRuleOutcome(terminal=True)


def _persist_run_state_best_effort(ctx: CycleContext, what: str) -> None:
    """run_stateをその場で永続化する（失敗はサイクル終端の保存に委ねて警告のみ）。"""
    try:
        save_run_state(
            ctx.run_state,
            ctx.config.run_state_path,
            launch_window_seconds=ctx.config.window_seconds,
            open_prs=ctx.prs,
        )
    except Exception as e:  # noqa: BLE001 - ベストエフォートの永続化
        print(f"Warning: failed to persist {what}: {e}", file=sys.stderr)


def _update_hold_record(ctx: CycleContext, active: ActiveWorktree) -> int:
    """dirty worktreeの保留回数を記録・永続化して返す。"""
    previous = ctx.run_state.task_reclaim_counts.get(active.issue_number)
    hold_count = (previous.count if previous else 0) + 1
    ctx.run_state.task_reclaim_counts[active.issue_number] = TaskReclaimRecord(
        count=hold_count, last_reclaimed_at=time.time()
    )
    _persist_run_state_best_effort(
        ctx, f"the dirty-worktree hold count for issue #{active.issue_number}"
    )
    return hold_count


def _escalate_held_dirty_worktree(
    ctx: CycleContext,
    key: str,
    active: ActiveWorktree,
    active_task: Task | None,
    hold_count: int,
) -> str:
    """保留上限を超えたdirty worktreeをエスカレーションする。"""
    released = False

    def _release_entry() -> None:
        nonlocal released
        released = True
        ctx.run_state.active_worktrees.pop(key, None)
        _persist_run_state_best_effort(
            ctx, f"the released ledger entry for issue #{active.issue_number}"
        )

    status_labels = (
        active_task.status_labels
        if active_task is not None
        else (StatusLabel.IN_PROGRESS,)
    )
    try:
        apply_human_review_escalation(
            active.issue_number,
            status_labels,
            "エージェントプロセスの終了を検知しましたが、worktreeに未コミットの変更が"
            "残っているため、完了処理を保留しました。\n"
            f"保留・回収の累計回数が上限（max_task_reclaims="
            f"{ctx.config.max_task_reclaims}）を超えた（今回で{hold_count}回目）ため、"
            "自動処理を打ち切り、status:blocked-human-reviewへ遷移しました。\n"
            "未コミットの作業データを保全するため、worktreeは削除せずに残しています: "
            f"{active.worktree_path}",
            forge=ctx.config.resolved_forge,
            on_label_applied=_release_entry,
        )
    except Exception as e:  # noqa: BLE001 - 1タスクの失敗でサイクルを止めない
        print(
            f"Warning: failed to escalate the held dirty worktree of issue "
            f"#{active.issue_number}: {e}",
            file=sys.stderr,
        )
        if not released:
            return "completion_skipped_dirty_worktree"
    return "escalated_reclaim_limit_exceeded"


def _apply_dirty_worktree_hold(
    ctx: CycleContext, key: str, active: ActiveWorktree, active_task: Task | None
) -> str:
    """#212のdirty worktree保留にも`max_task_reclaims`の上限を効かせる。"""
    if not ctx.config.apply:
        return "completion_skipped_dirty_worktree"
    hold_count = _update_hold_record(ctx, active)
    if not exceeds_limit(hold_count, ctx.config.max_task_reclaims):
        return "completion_skipped_dirty_worktree"
    return _escalate_held_dirty_worktree(ctx, key, active, active_task, hold_count)


def _record_completed_worktree(
    ctx: CycleContext,
    key: str,
    completion_active: ActiveWorktree,
    active_task: Task | None,
    completion_event: dict,
) -> ActiveWorktreeRuleOutcome:
    """完了（またはトークン上限超過）で終端したworktreeを完了履歴へ退避する。"""
    action = completion_event["action"]
    completed_subtask_id = None
    if action == "completed" and active_task is not None and active_task.subtask_id:
        completed_subtask_id = active_task.subtask_id
    if ctx.config.apply:
        raw_usage = completion_event.get("usage")
        usage_obj = Usage(**raw_usage) if raw_usage else None
        ctx.run_state.completed_worktrees.append(
            CompletedWorktree(
                issue_number=completion_active.issue_number,
                subtask_id=active_task.subtask_id if active_task else "",
                branch=completion_active.branch,
                started_at=completion_active.started_at,
                completed_at=time.time(),
                recompute_count=completion_active.recompute_count,
                forced_serial=completion_active.forced_serial,
                commit_sha=completion_event.get("commit_sha"),
                base_branch=completion_active.base_branch,
                usage=usage_obj,
                profile=completion_active.profile,
                model=completion_active.model,
                reasoning_effort=completion_active.reasoning_effort,
                selection_reason=completion_active.selection_reason,
            )
        )
        del ctx.run_state.active_worktrees[key]

    return ActiveWorktreeRuleOutcome(
        completion_event=completion_event,
        completed_subtask_id=completed_subtask_id,
        terminal=True,
    )


def _cleanup_stale_active_worktree(
    active: ActiveWorktree, reason: str, config: DispatcherConfig
) -> bool:
    """古い帳簿エントリに対応するworktreeのWIPバックアップとクリーンアップを行う。"""
    worktree_exists = os.path.exists(active.worktree_path)
    if worktree_exists:
        backup_error = backup_wip_commit(
            active.worktree_path, "WIP: backup by Orchestune GC (stale active entry)"
        )
        if backup_error is not None:
            config.resolved_forge.add_comment(
                active.issue_number,
                "run_stateの古い帳簿エントリを検知しました"
                f"（{reason}）。対象プロセスの後始末を試みましたが、WIP"
                "バックアップコミットの作成に失敗しました。\n"
                "未コミットの作業データ消失を防ぐため、今回の帳簿破棄処理を"
                "一時スキップしました。次サイクルで再試行します。\n"
                f"エラー詳細:\n```\n{backup_error}\n```",
            )
            return False

    if active.pid and is_process_alive(active.pid):
        try:
            os.kill(active.pid, 9)
        except Exception:
            pass

    if worktree_exists:
        remove_worktree(active.worktree_path)
    return True


def _apply_stale_active_entry_discard(
    run_state: RunState,
    key: str,
    active: ActiveWorktree,
    reason: str,
    config: DispatcherConfig,
) -> bool:
    """#382: 帳簿(run_state)を破棄する前に、対応する物理worktree・プロセスの
    状態を確認し、必要な後始末を行う。
    """
    if not config.apply:
        return True
    if not _cleanup_stale_active_worktree(active, reason, config):
        return False
    del run_state.active_worktrees[key]
    record = run_state.task_reclaim_counts.get(active.issue_number)
    if record is not None and record.pending:
        record.pending = False
    return True


def _create_abandonment_callbacks(
    ctx: CycleContext, key: str, active: ActiveWorktree
) -> tuple[Callable[[], None], Callable[[], None], Callable[[], bool]]:
    """放棄worktree処理時の永続化・解放コールバック群を生成する。"""
    released = False

    def _release_entry() -> None:
        nonlocal released
        ctx.run_state.active_worktrees.pop(key, None)
        rec = ctx.run_state.task_reclaim_counts.get(active.issue_number)
        if rec is not None:
            rec.pending = False
        save_run_state(
            ctx.run_state,
            ctx.config.run_state_path,
            launch_window_seconds=ctx.config.window_seconds,
            open_prs=ctx.prs,
        )
        released = True

    def _reserve_reclaim() -> None:
        save_run_state(
            ctx.run_state,
            ctx.config.run_state_path,
            launch_window_seconds=ctx.config.window_seconds,
            open_prs=ctx.prs,
        )

    def _is_released() -> bool:
        return released

    return _release_entry, _reserve_reclaim, _is_released


def _abandoned_worktree_outcome(
    ctx: CycleContext,
    key: str,
    active: ActiveWorktree,
    active_task: Task | None,
) -> ActiveWorktreeRuleOutcome:
    release_entry, reserve_reclaim, is_released = _create_abandonment_callbacks(
        ctx, key, active
    )
    try:
        completion_event = _finalize_abandoned_cloud_worktree(
            active,
            active_task,
            ctx.config,
            ctx.run_state,
            on_label_applied=release_entry,
            on_reclaim_reserved=reserve_reclaim,
        )
    except Exception as e:
        print(
            f"Warning: skipping abandonment of issue #{active.issue_number}: "
            f"failed to persist the reclaim count: {e}",
            file=sys.stderr,
        )
        return ActiveWorktreeRuleOutcome(
            completion_event={
                "issue_number": active.issue_number,
                "subtask_id": active_task.subtask_id if active_task else "",
                "worktree_path": active.worktree_path,
                "action": "abandonment_skipped_persistence_failure",
            },
            terminal=True,
        )

    if (
        completion_event["action"]
        in ("abandoned_pr_requeued", "escalated_reclaim_limit_exceeded")
        and ctx.config.apply
        and not is_released()
    ):
        ctx.run_state.active_worktrees.pop(key, None)
        _persist_run_state_best_effort(
            ctx, f"the released ledger entry for issue #{active.issue_number}"
        )
    return ActiveWorktreeRuleOutcome(completion_event=completion_event, terminal=True)


def _find_recovery_pr(
    active: ActiveWorktree, config: DispatcherConfig
) -> PrRecord | None:
    all_prs = config.resolved_forge.list_prs(state="all")
    matching_prs = [
        pr for pr in all_prs if active.issue_number in pr.closes_issue_numbers
    ]
    return next(
        (pr for pr in matching_prs if pr.state.upper() in {"OPEN", "MERGED"}),
        matching_prs[0] if matching_prs else None,
    )


@dataclass(frozen=True, slots=True)
class CompletionResolution:
    """Explicit result of probing whether one active worktree can be completed."""

    state: Literal["pending", "ready", "resolved"]
    completion_active: ActiveWorktree | None = None
    rule_outcome: ActiveWorktreeRuleOutcome | None = None

    def __post_init__(self) -> None:
        valid = (
            (
                self.state == "pending"
                and self.completion_active is None
                and self.rule_outcome is None
            )
            or (
                self.state == "ready"
                and self.completion_active is not None
                and self.rule_outcome is None
            )
            or (
                self.state == "resolved"
                and self.completion_active is None
                and self.rule_outcome is not None
            )
        )
        if not valid:
            raise ValueError(f"invalid completion resolution: {self.state}")

    @classmethod
    def pending(cls) -> CompletionResolution:
        return cls(state="pending")

    @classmethod
    def ready(cls, active: ActiveWorktree) -> CompletionResolution:
        return cls(state="ready", completion_active=active)

    @classmethod
    def resolved(cls, outcome: ActiveWorktreeRuleOutcome) -> CompletionResolution:
        return cls(state="resolved", rule_outcome=outcome)


def _completion_forge_error_hold(
    active: ActiveWorktree, operation: str = "", error: str = ""
) -> ActiveWorktreeRuleOutcome:
    """Build a non-mutating, same-cycle GC hold for an indeterminate completion.

    #787: どのForge呼び出しがなぜ失敗して保留になったのかをイベントへ載せ、
    サイクルレポートの警告セクションから辿れるようにする。
    """
    event: dict[str, object] = {
        "issue_number": active.issue_number,
        "worktree_path": active.worktree_path,
        "action": "completion_skipped_forge_error",
    }
    if operation:
        event["operation"] = operation
    if error:
        event["error"] = error
    return ActiveWorktreeRuleOutcome(completion_event=event, terminal=True)


def _resolve_recovered_completion(
    ctx: CycleContext,
    key: str,
    active: ActiveWorktree,
    active_task: Task | None,
) -> CompletionResolution:
    """run_stateに起動時刻もexternal idも無い項目を、PRから復元して解決する。"""
    try:
        recovery_pr = _find_recovery_pr(active, ctx.config)
    except Exception as error:  # noqa: BLE001 - 判定を保留し、事実だけ表に出す
        return CompletionResolution.resolved(
            _completion_forge_error_hold(
                active,
                "find_recovery_pr",
                warn_forge_failure("find_recovery_pr", active.issue_number, error),
            )
        )
    if recovery_pr is None:
        return CompletionResolution.pending()
    if recovery_pr.state.upper() == "CLOSED":
        return CompletionResolution.resolved(
            _abandoned_worktree_outcome(ctx, key, active, active_task)
        )
    return CompletionResolution.ready(
        replace(
            active,
            branch=recovery_pr.head_ref,
            external_id=f"recovered-pr:{recovery_pr.number}",
            external_url=f"PR#{recovery_pr.number}",
        )
    )


def _resolve_cloud_completion(
    ctx: CycleContext,
    key: str,
    active: ActiveWorktree,
    active_task: Task | None,
) -> CompletionResolution:
    failures: list[ForgeFailure] = []
    status = _cloud_worktree_completion_status(active, ctx.config, failures)
    if status == "abandoned":
        return CompletionResolution.resolved(
            _abandoned_worktree_outcome(ctx, key, active, active_task)
        )
    if status == "unknown":
        return CompletionResolution.resolved(
            _completion_forge_error_hold(
                active,
                failed_operations(failures) or "completion_status",
                failure_descriptions(failures),
            )
        )
    if status != "completed":
        return CompletionResolution.pending()
    return CompletionResolution.ready(active)


def _resolve_local_completion(
    ctx: CycleContext,
    key: str,
    active: ActiveWorktree,
    active_task: Task | None,
) -> CompletionResolution:
    if not _is_worktree_complete(active, ctx.config):
        return CompletionResolution.pending()
    failures: list[ForgeFailure] = []
    status = _local_pr_completion_status(active, ctx.config, failures)
    if status == "abandoned":
        return CompletionResolution.resolved(
            _abandoned_worktree_outcome(ctx, key, active, active_task)
        )
    if status == "unknown":
        return CompletionResolution.resolved(
            _completion_forge_error_hold(
                active,
                # PR#789レビュー対応(Codex P2): 実際に失敗した呼び出しを名乗る。
                # オープンPRのコメント取得が失敗しても`list_prs`と報告していた。
                failed_operations(failures) or "list_prs",
                failure_descriptions(failures),
            )
        )
    return CompletionResolution.ready(active)


def _resolve_completion(
    ctx: CycleContext,
    key: str,
    active: ActiveWorktree,
    active_task: Task | None,
) -> CompletionResolution:
    """完了候補・保留・早期終端を明示的な値として解決する。"""
    if active.started_at is None and active.external_id is None:
        return _resolve_recovered_completion(ctx, key, active, active_task)
    if active.external_id is not None:
        return _resolve_cloud_completion(ctx, key, active, active_task)
    return _resolve_local_completion(ctx, key, active, active_task)


def _handle_completed_event_outcome(
    ctx: CycleContext,
    key: str,
    completion_active: ActiveWorktree,
    active_task: Task | None,
    completion_event: dict,
) -> ActiveWorktreeRuleOutcome | None:
    """完了イベントのアクションに応じてクリーンアップまたは履歴保存を行う。"""
    action = completion_event["action"]
    if action == "completion_skipped_forge_error":
        return ActiveWorktreeRuleOutcome(
            completion_event=completion_event, terminal=True
        )
    if action in ("completed", "escalated_token_limit_exceeded"):
        return _record_completed_worktree(
            ctx, key, completion_active, active_task, completion_event
        )
    if action in (
        "completed_no_commits",
        "early_death_requeued",
        "completed_without_outcome",
        "not_needed",
        "not_needed_review_dispatched",
        "blocked_base_branch_red",
        "escalated_base_branch_red",
    ):
        if ctx.config.apply:
            ctx.run_state.active_worktrees.pop(key, None)
    elif action == "completion_skipped_dirty_worktree":
        completion_event["action"] = _apply_dirty_worktree_hold(
            ctx, key, completion_active, active_task
        )
    return ActiveWorktreeRuleOutcome(completion_event=completion_event, terminal=True)


def _rule_completed(
    ctx: CycleContext, key: str, active: ActiveWorktree, active_task: Task | None
) -> ActiveWorktreeRuleOutcome | None:
    resolution = _resolve_completion(ctx, key, active, active_task)
    if resolution.state == "pending":
        return None
    if resolution.state == "resolved":
        return resolution.rule_outcome
    completion_active = resolution.completion_active
    assert completion_active is not None

    def _settle_early_death_requeue() -> None:
        ctx.run_state.active_worktrees.pop(key, None)
        record = ctx.run_state.task_reclaim_counts.get(active.issue_number)
        if record is not None:
            record.early_death_retry_pending = False
        save_run_state(
            ctx.run_state,
            ctx.config.run_state_path,
            launch_window_seconds=ctx.config.window_seconds,
            open_prs=ctx.prs,
        )

    completion_event = _finalize_completed_worktree(
        completion_active,
        active_task,
        ctx.config,
        dispatch_not_needed_review=ctx.not_needed_review_dispatcher,
        run_state=ctx.run_state,
        now=time.time(),
        open_prs=ctx.prs,
        on_early_death_requeue=_settle_early_death_requeue,
    )
    return _handle_completed_event_outcome(
        ctx, key, completion_active, active_task, completion_event
    )
