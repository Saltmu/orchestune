"""Zombie and timeout reclamation for active dispatcher worktrees."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_gc_git import (
    backup_wip_commit,
    remove_worktree,
    worktree_has_uncommitted_changes,
)
from orchestune.dispatch_labels import (
    TERMINAL_ESCALATION_LABELS,
    transition_status_label,
)
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree, RunState
from orchestune.process_utils import is_process_alive


def _check_zombie_and_timeout(
    active: ActiveWorktree,
    zombie_enabled: bool,
    timeout_limit: int,
    now: float,
) -> tuple[bool, bool, bool]:
    """Return ``(is_zombie, is_timeout, process_alive)``."""
    is_zombie = False
    is_timeout = False
    process_alive = is_process_alive(active.pid)

    if zombie_enabled and not process_alive:
        if os.path.exists(active.worktree_path) and worktree_has_uncommitted_changes(
            active.worktree_path
        ):
            is_zombie = True

    if not is_zombie and active.started_at is not None:
        if timeout_limit > 0 and now - active.started_at > timeout_limit:
            is_timeout = True

    return is_zombie, is_timeout, process_alive


@dataclass
class ZombieOrTimeoutReclaim:
    key: str
    active: ActiveWorktree
    subtask_id: str
    reason: str
    is_timeout: bool
    process_alive: bool
    status_labels: tuple[str, ...] = ("status:in-progress",)


def _decide_zombie_or_timeout_reclaims(
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
    held_worktree_paths: set[str] | None,
    now: float,
) -> list[ZombieOrTimeoutReclaim]:
    """Decide all reclaim candidates without applying side effects."""
    zombie_enabled = getattr(config, "zombie_gc", True)
    timeout_limit = getattr(config, "task_timeout_seconds", 0)
    held_worktree_paths = held_worktree_paths or set()

    if not zombie_enabled and timeout_limit <= 0:
        return []

    reclaims: list[ZombieOrTimeoutReclaim] = []
    for key, active in run_state.active_worktrees.items():
        if active.worktree_path in held_worktree_paths:
            continue
        is_zombie, is_timeout, process_alive = _check_zombie_and_timeout(
            active, zombie_enabled, timeout_limit, now
        )
        if not (is_zombie or is_timeout):
            continue
        active_task = tasks_by_issue.get(active.issue_number)
        reclaims.append(
            ZombieOrTimeoutReclaim(
                key=key,
                active=active,
                subtask_id=active_task.subtask_id if active_task else "",
                reason="process disappeared" if is_zombie else "timeout exceeded",
                is_timeout=is_timeout,
                process_alive=process_alive,
                status_labels=(
                    active_task.status_labels
                    if active_task is not None
                    else ("status:in-progress",)
                ),
            )
        )
    return reclaims


def _apply_zombie_or_timeout_reclaim(
    run_state: RunState,
    reclaim: ZombieOrTimeoutReclaim,
    config: DispatcherConfig,
) -> dict | None:
    """decide層が判定した回収対象に基づき、安全に副作用を適用する。

    worktreeの存在確認は、decide時点のスナップショットを信用せず、副作用を
    実行する直前にこの関数内で再評価する。全回収対象の判定（decide）を先に
    まとめて行ってから1件ずつapplyする都合上、decideからこの関数の実行までの
    間にworktreeの状態（削除・再作成）が変化し得るため、古いスナップショットを
    そのまま使うとバックアップ・削除・orphan worktree残存に関する安全策を
    迂回しかねない。

    WIPバックアップコミットの作成に失敗した場合は、未コミットの作業データ
    消失を防ぐため今回のGC回収処理全体をスキップし、Noneを返す
    （run_stateは変更せず、次サイクルでの再試行に委ねる）。
    """
    active = reclaim.active
    reason = reclaim.reason
    if config.apply:
        worktree_exists = os.path.exists(active.worktree_path)
        if worktree_exists:
            backup_error = backup_wip_commit(
                active.worktree_path, f"WIP: backup by Orchestune GC ({reason})"
            )
            if backup_error is not None:
                config.resolved_forge.add_comment(
                    active.issue_number,
                    f"タスク実行が {reason} のためGCによる回収を試みましたが、WIPバックアップコミットの作成に失敗しました。\n"
                    "未コミットの作業データ消失を防ぐため、今回のGC回収およびworktree削除処理を一時スキップしました。\n"
                    f"エラー詳細:\n```\n{backup_error}\n```",
                )
                return None
        if reclaim.is_timeout and active.pid and reclaim.process_alive:
            try:
                os.kill(active.pid, 9)
            except Exception:
                pass
        if worktree_exists:
            remove_worktree(active.worktree_path)
        worktree_note = (
            "作業ブランチにWIPコミットを退避した上で、"
            if worktree_exists
            else "物理worktreeが見つからなかったため、"
        )
        already_escalated = any(
            label in reclaim.status_labels for label in TERMINAL_ESCALATION_LABELS
        )
        if already_escalated:
            # 中断した以前の遷移でstatus:blocked-human-review/
            # status:manual-merge-requiredが既に付与されている場合、
            # status:queuedへ書き換えると人間の確認要求を握りつぶして
            # 自動的に再起動してしまう。物理的な後始末のみ行い、
            # ラベルには一切触れない。
            config.resolved_forge.add_comment(
                active.issue_number,
                f"タスク実行が {reason} のため、GCにより{worktree_note}"
                "後始末しました。既に人間の確認が必要な状態のため、"
                "status:*ラベルは変更していません。",
            )
        else:
            # #381レビュー対応(Codex P2): stacked launch等の中断した遷移で
            # status:blockedが取り残されている場合も併せて除去し、
            # status:queuedへ確実に収束させる。
            stale_labels = tuple(
                label
                for label in ("status:in-progress", "status:blocked")
                if label in reclaim.status_labels
            )
            transition_status_label(
                config.resolved_forge,
                active.issue_number,
                "status:queued",
                stale_labels,
            )
            config.resolved_forge.add_comment(
                active.issue_number,
                f"タスク実行が {reason} のため、GCにより{worktree_note}"
                "タスクを再キューイング（status:queued）しました。",
            )
        del run_state.active_worktrees[reclaim.key]

    return {
        "issue_number": active.issue_number,
        "subtask_id": reclaim.subtask_id,
        "action": "gc_reclaimed",
        "reason": reason,
    }


def _collect_zombies_and_timeouts(
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
    held_worktree_paths: set[str] | None = None,
) -> list[dict]:
    """Decide and apply zombie and timeout reclamations."""
    reclaims = _decide_zombie_or_timeout_reclaims(
        run_state, tasks_by_issue, config, held_worktree_paths, time.time()
    )
    events: list[dict] = []
    for reclaim in reclaims:
        event = _apply_zombie_or_timeout_reclaim(run_state, reclaim, config)
        if event is not None:
            events.append(event)
    return events
