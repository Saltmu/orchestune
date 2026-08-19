"""Active-worktree GC rule-chain orchestration.

Implementation helpers are re-exported for backward-compatible imports.
"""

from __future__ import annotations

import os
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
from orchestune.dispatch_state import (
    ActiveWorktree,
    CompletedWorktree,
    RunState,
)
from orchestune.models import PrRecord, Usage
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
    run_state: RunState,
    key: str,
    active: ActiveWorktree,
    reason: str,
    config: DispatcherConfig,
) -> bool:
    """#382: 帳簿(run_state)を破棄する前に、対応する物理worktree・プロセスの
    状態を確認し、必要な後始末を行う。

    GitHubラベルの更新がrun_state保存より後に行われる順序（#381の起動成功
    パスのコメント参照）のため、この間でクラッシュすると、実際には
    エージェントプロセスがまだ稼働しworktreeへ書き込みを続けているのに、
    GitHubラベルだけがstatus:in-progress以外に変わっている（あるいは戻って
    いる）ことがある。それを確認せず帳簿だけ破棄すると、後続サイクルで同じ
    Issueが再度起動候補に選ばれた際、`create_worktree_and_launch`が生存中
    プロセスの作業ディレクトリを無条件に強制削除・再作成してしまう。
    ゾンビ/タイムアウト回収（`_apply_zombie_or_timeout_reclaim`）と同じ
    安全策（WIPバックアップ→プロセス停止→worktree削除→帳簿削除の順）を
    ここでも適用する。

    WIPバックアップの作成に失敗した場合は、未コミットの作業データ消失を
    防ぐため今回の破棄処理全体をスキップし、`False`を返す（run_stateは
    変更せず、次サイクルでの再試行に委ねる）。
    """
    if not config.apply:
        return True

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

    del run_state.active_worktrees[key]
    return True


def _rule_stale_entry(
    ctx: CycleContext, key: str, active: ActiveWorktree, active_task: Task | None
) -> ActiveWorktreeRuleOutcome | None:
    stale_event = _decide_stale_active_entry(active, active_task)
    if stale_event is None:
        return None
    discarded = _apply_stale_active_entry_discard(
        ctx.run_state, key, active, stale_event["reason"], ctx.config
    )
    if not discarded:
        return None
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
    if action in ("completed", "escalated_token_limit_exceeded"):
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
