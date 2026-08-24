"""ディスパッチ優先度の算出・選出ロジック。"""

from __future__ import annotations

from datetime import datetime

from orchestune.dispatch.state import RunState
from orchestune.issue_parsing import BASE_PRIORITY, parse_task_from_issue
from orchestune.issue_parsing import FOOTPRINT_BLOCK_PATTERN as _FOOTPRINT_BLOCK_PATTERN
from orchestune.models import Task

# 以下3つは#286/#287(rewire-dispatch-imports/rewire-integrator-imports)で
# 呼び出し側の付け替えが完了するまでの後方互換再エクスポート。実体は
# orchestune.models / orchestune.issue_parsing に移設済み。
__all__ = [
    "Task",
    "_FOOTPRINT_BLOCK_PATTERN",
    "parse_task_from_issue",
    "quota_available",
    "compute_priority_score",
    "select_next_tasks",
]

TIME_BONUS_WEIGHT = 0.5
PROGRESS_BONUS = 1.0


def quota_available(
    run_state: RunState,
    now: float,
    max_concurrent: int,
    max_launches_per_window: int,
    window_seconds: int,
    max_tokens_per_window: int | None = None,
) -> int:
    concurrent_remaining = max(0, max_concurrent - len(run_state.active_worktrees))
    recent_launches = [t for t in run_state.launch_history if now - t < window_seconds]
    rate_remaining = max(0, max_launches_per_window - len(recent_launches))
    if max_tokens_per_window is not None:
        recent_completed = [
            w
            for w in run_state.completed_worktrees
            if now - w.completed_at < window_seconds
        ]
        tokens_consumed = sum(
            w.usage.total_tokens
            for w in recent_completed
            if w.usage is not None and w.usage.total_tokens is not None
        )
        if tokens_consumed >= max_tokens_per_window:
            return 0
    return min(concurrent_remaining, rate_remaining)


def _last_attempt_at(task: Task, run_state: RunState) -> float | None:
    """このタスクが直近に試行完了(成功/失敗問わず)した時刻。履歴が無ければNone。"""
    timestamps = [
        w.completed_at
        for w in run_state.completed_worktrees
        if w.issue_number == task.issue_number
    ]
    return max(timestamps) if timestamps else None


def _wait_seconds(task: Task, run_state: RunState, now: float) -> float:
    # #299: created_at（Issue作成時刻、不変値）だけを基準にすると、
    # ほぼ同時刻に作成された同priorityのタスク同士が恒常的に同点になり、
    # issue番号の小さい方がタイブレークで勝ち続けて番号の大きい方が
    # 「飢餓状態」になる。直近に試行済みのタスクは相対的に後回しに
    # なるよう、試行履歴があればそちらを基準にする。
    last_attempt = _last_attempt_at(task, run_state)
    if last_attempt is not None:
        return max(0.0, now - last_attempt)
    created = datetime.fromisoformat(task.created_at.replace("Z", "+00:00"))
    return max(0.0, now - created.timestamp())


def compute_priority_score(
    task: Task, all_candidate_tasks: list[Task], run_state: RunState, now: float
) -> float:
    base_priority = BASE_PRIORITY.get(task.priority, BASE_PRIORITY["medium"])
    waits = [_wait_seconds(t, run_state, now) for t in all_candidate_tasks]
    avg_wait = sum(waits) / len(waits) if waits else 0.0

    time_bonus = 0.0
    if avg_wait > 0:
        wait = _wait_seconds(task, run_state, now)
        time_bonus = max(0.0, (wait / avg_wait) - 1.0) * TIME_BONUS_WEIGHT

    progress_factor = PROGRESS_BONUS if task.progress_partial else 0.0
    return base_priority * (1.0 + time_bonus) + progress_factor


def select_next_tasks(
    candidate_tasks: list[Task],
    run_state: RunState,
    now: float,
    max_concurrent: int,
    max_launches_per_window: int,
    window_seconds: int,
    max_tokens_per_window: int | None = None,
) -> list[Task]:
    active_issue_numbers = {int(k) for k in run_state.active_worktrees}
    eligible = [
        t
        for t in candidate_tasks
        if not t.yaml_error
        and "status:external-lock" not in t.status_labels
        and "status:blocked-recompute" not in t.status_labels
        and t.issue_number not in active_issue_numbers
    ]
    slots = quota_available(
        run_state,
        now,
        max_concurrent,
        max_launches_per_window,
        window_seconds,
        max_tokens_per_window=max_tokens_per_window,
    )
    scored = sorted(
        eligible,
        key=lambda t: (
            -compute_priority_score(t, eligible, run_state, now),
            t.issue_number,
        ),
    )
    return scored[:slots]
