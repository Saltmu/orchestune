"""Active-worktree GC rule-chain orchestration.

Implementation helpers are re-exported for backward-compatible imports.
"""

from __future__ import annotations

import time
from dataclasses import replace

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_gc_completion import (
    CompletedWorktreeDecision,
    _active_dispatch_handle,
    _apply_completed_worktree_outcome,
    _call_is_complete,
    _cloud_worktree_completion_status,
    _decide_completed_worktree_outcome,
    _decide_not_needed_dirty_worktree,
    _finalize_abandoned_cloud_worktree,
    _finalize_completed_worktree,
    _finalize_not_needed_worktree,
    _is_stale_closed_pr_for_active,
    _is_worktree_complete,
    _local_pr_completion_status,
    _parse_github_timestamp,
)
from orchestune.dispatch_gc_git import (
    backup_wip_commit,
    remote_branch_commit_sha_if_ahead,
    remove_worktree,
    worktree_has_new_commits,
    worktree_has_uncommitted_changes,
)
from orchestune.dispatch_gc_zombies import (
    ZombieOrTimeoutReclaim,
    _apply_zombie_or_timeout_reclaim,
    _check_zombie_and_timeout,
    _collect_zombies_and_timeouts,
    _decide_zombie_or_timeout_reclaims,
)
from orchestune.dispatch_rules import ActiveWorktreeRuleOutcome, CycleContext
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree, CompletedWorktree, RunState
from orchestune.models import PrRecord
from orchestune.process_utils import is_process_alive

__all__ = [
    "CompletedWorktreeDecision",
    "ZombieOrTimeoutReclaim",
    "_active_dispatch_handle",
    "_apply_completed_worktree_outcome",
    "_apply_zombie_or_timeout_reclaim",
    "_call_is_complete",
    "_check_zombie_and_timeout",
    "_cloud_worktree_completion_status",
    "_collect_zombies_and_timeouts",
    "_decide_completed_worktree_outcome",
    "_decide_not_needed_dirty_worktree",
    "_decide_zombie_or_timeout_reclaims",
    "_finalize_abandoned_cloud_worktree",
    "_finalize_completed_worktree",
    "_finalize_not_needed_worktree",
    "_is_stale_closed_pr_for_active",
    "_is_worktree_complete",
    "_local_pr_completion_status",
    "_parse_github_timestamp",
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
    """#280: status:not-neededラベル検知による即時完了処理。

    セッションが「対応不要」と判断した場合、コミット・PRを作らないため
    closingIssuesReferences等の完了シグナルが発生せず、`_rule_completed`
    （PID/PR存在ベース）は永遠にマッチしない。ラベル検知を最優先の完了
    シグナルとして扱い、stale判定より先に評価する。
    """
    if active_task is None or "status:not-needed" not in active_task.status_labels:
        return None
    completion_event = _finalize_not_needed_worktree(
        active, active_task, ctx.config, ctx.not_needed_review_dispatcher
    )
    completed_subtask_id = None
    if completion_event["action"] in ("not_needed", "not_needed_review_dispatched"):
        if active_task.subtask_id:
            completed_subtask_id = active_task.subtask_id
        if ctx.config.apply:
            del ctx.run_state.active_worktrees[key]
    return ActiveWorktreeRuleOutcome(
        completion_event=completion_event,
        completed_subtask_id=completed_subtask_id,
        terminal=True,
    )


def _decide_stale_active_entry(
    active: ActiveWorktree, active_task: Task | None
) -> dict | None:
    """GitHubラベルを正として、run_state側のstaleエントリを判定する。"""
    if (
        active_task is not None
        and "status:in-progress" not in active_task.status_labels
    ):
        # run_stateへの登録(save_run_state)は起動成功直後に、GitHubラベルの
        # status:in-progress付与はその後に行う順序になっているため、この間で
        # クラッシュした場合（あるいは完了/エスカレーション処理でラベルだけ
        # 先に更新されてクラッシュした場合）、GitHub側のラベルは
        # status:in-progressでなくなっているのにrun_state側にだけ古い
        # エントリが残ることがある。GitHubラベルを正として、この古い帳簿
        # エントリを破棄する（ゾンビGCの拡張）。
        return {
            "issue_number": active.issue_number,
            "subtask_id": active_task.subtask_id,
            "action": "stale_active_entry_discarded",
            "reason": (
                "issue label is no longer status:in-progress "
                f"(labels={sorted(active_task.status_labels)})"
            ),
        }
    return None


def _apply_stale_active_entry_discard(
    run_state: RunState, key: str, config: DispatcherConfig
) -> None:
    if config.apply:
        del run_state.active_worktrees[key]


def _rule_stale_entry(
    ctx: CycleContext, key: str, active: ActiveWorktree, active_task: Task | None
) -> ActiveWorktreeRuleOutcome | None:
    stale_event = _decide_stale_active_entry(active, active_task)
    if stale_event is None:
        return None
    _apply_stale_active_entry_discard(ctx.run_state, key, ctx.config)
    return ActiveWorktreeRuleOutcome(completion_event=stale_event, terminal=True)


def _abandoned_worktree_outcome(
    ctx: CycleContext,
    key: str,
    active: ActiveWorktree,
    active_task: Task | None,
) -> ActiveWorktreeRuleOutcome:
    completion_event = _finalize_abandoned_cloud_worktree(
        active, active_task, ctx.config
    )
    if completion_event["action"] == "abandoned_pr_requeued" and ctx.config.apply:
        del ctx.run_state.active_worktrees[key]
    return ActiveWorktreeRuleOutcome(completion_event=completion_event, terminal=True)


def _find_recovery_pr(
    active: ActiveWorktree, config: DispatcherConfig
) -> PrRecord | None:
    try:
        all_prs = config.resolved_forge.list_prs(state="all")
    except Exception:
        return None
    matching_prs = [
        pr for pr in all_prs if active.issue_number in pr.closes_issue_numbers
    ]
    return next(
        (pr for pr in matching_prs if pr.state.upper() in {"OPEN", "MERGED"}),
        matching_prs[0] if matching_prs else None,
    )


def _rule_completed(
    ctx: CycleContext, key: str, active: ActiveWorktree, active_task: Task | None
) -> ActiveWorktreeRuleOutcome | None:
    completion_active = active
    if active.started_at is None and active.external_id is None:
        recovery_pr = _find_recovery_pr(active, ctx.config)
        if recovery_pr is None:
            return None
        if recovery_pr.state.upper() == "CLOSED":
            return _abandoned_worktree_outcome(ctx, key, active, active_task)
        completion_active = replace(
            active,
            branch=recovery_pr.head_ref,
            external_id=f"recovered-pr:{recovery_pr.number}",
            external_url=f"PR#{recovery_pr.number}",
        )
    elif active.external_id is not None:
        completion_status = _cloud_worktree_completion_status(active, ctx.config)
        if completion_status == "abandoned":
            return _abandoned_worktree_outcome(ctx, key, active, active_task)
        if completion_status != "completed":
            return None
    else:
        if not _is_worktree_complete(active, ctx.config):
            return None
        local_pr_status = _local_pr_completion_status(active, ctx.config)
        if local_pr_status == "abandoned":
            return _abandoned_worktree_outcome(ctx, key, active, active_task)
        if local_pr_status == "unknown":
            return None

    completion_event = _finalize_completed_worktree(
        completion_active, active_task, ctx.config
    )
    action = completion_event["action"]
    if action == "completed":
        completed_subtask_id = None
        if active_task is not None and active_task.subtask_id:
            completed_subtask_id = active_task.subtask_id
        if ctx.config.apply:
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
                )
            )
            del ctx.run_state.active_worktrees[key]
        return ActiveWorktreeRuleOutcome(
            completion_event=completion_event,
            completed_subtask_id=completed_subtask_id,
            terminal=True,
        )
    if action == "completed_no_commits":
        if ctx.config.apply:
            del ctx.run_state.active_worktrees[key]
        return ActiveWorktreeRuleOutcome(
            completion_event=completion_event, terminal=True
        )
    return ActiveWorktreeRuleOutcome(completion_event=completion_event, terminal=True)
