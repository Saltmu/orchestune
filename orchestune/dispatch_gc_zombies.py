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
            )
        )
    return reclaims


def _apply_zombie_or_timeout_reclaim(
    run_state: RunState,
    reclaim: ZombieOrTimeoutReclaim,
    config: DispatcherConfig,
) -> dict | None:
    """Apply one previously decided reclaim while protecting dirty work."""
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
        config.resolved_forge.remove_label(active.issue_number, "status:in-progress")
        config.resolved_forge.add_label(active.issue_number, "status:queued")
        worktree_note = (
            "作業ブランチにWIPコミットを退避した上で、"
            if worktree_exists
            else "物理worktreeが見つからなかったため、"
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
