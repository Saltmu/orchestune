"""Issueからのタスク定義パースと、ディスパッチ優先度の算出・選出ロジック。"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime

import yaml

from orchestune.dispatch_state import RunState
from orchestune.github import IssueRecord

BASE_PRIORITY = {"low": 1.0, "medium": 2.0, "high": 3.0}
TIME_BONUS_WEIGHT = 0.5
PROGRESS_BONUS = 1.0

_FOOTPRINT_BLOCK_PATTERN = re.compile(r"```yaml\s*\n(.*?)```", re.DOTALL)


@dataclass(frozen=True)
class Task:
    issue_number: int
    subtask_id: str
    footprint: tuple[str, ...]
    symbols: tuple[str, ...]
    risk: bool
    priority: str
    progress_partial: bool
    status_labels: tuple[str, ...]
    created_at: str
    depends_on: tuple[str, ...] = ()
    yaml_error: bool = False
    parent_number: int | None = None
    issue_state: str = "OPEN"
    parent_state: str | None = None


def parse_task_from_issue(
    issue: IssueRecord,
    issue_to_subtask_id: dict[int, str] | None = None,
) -> Task:
    subtask_id = ""
    footprint: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    yaml_error = False

    match = _FOOTPRINT_BLOCK_PATTERN.search(issue.body)
    if match:
        try:
            data = yaml.safe_load(match.group(1))
            if isinstance(data, dict):
                subtask_id = str(data.get("subtask_id", ""))
                footprint = tuple(str(f) for f in (data.get("footprint") or []))
                symbols = tuple(str(s) for s in (data.get("symbols") or []))
        except yaml.YAMLError as e:
            print(
                f"Warning: Failed to parse YAML from issue #{issue.number}: {e}",
                file=sys.stderr,
            )
            yaml_error = True

    if issue_to_subtask_id is not None and issue.blocked_by:
        depends_on = tuple(
            issue_to_subtask_id[num]
            for num in issue.blocked_by
            if num in issue_to_subtask_id
        )
    else:
        if match and not yaml_error:
            try:
                data = yaml.safe_load(match.group(1))
                if isinstance(data, dict):
                    depends_on = tuple(str(d) for d in (data.get("depends_on") or []))
            except Exception:
                pass

    priority = "medium"
    risk = False
    progress_partial = False
    for label in issue.labels:
        if label.startswith("priority:"):
            priority = label.split(":", 1)[1]
        elif label == "risk:flagged":
            risk = True
        elif label == "progress:partial":
            progress_partial = True

    parent_number = None
    parent_state = None
    if issue.parent:
        parent_number = issue.parent.get("number")
        parent_state = issue.parent.get("state")

    return Task(
        issue_number=issue.number,
        subtask_id=subtask_id,
        footprint=footprint,
        symbols=symbols,
        risk=risk,
        priority=priority,
        progress_partial=progress_partial,
        status_labels=tuple(issue.labels),
        created_at=issue.created_at,
        depends_on=depends_on,
        yaml_error=yaml_error,
        parent_number=parent_number,
        issue_state=issue.state,
        parent_state=parent_state,
    )


def quota_available(
    run_state: RunState,
    now: float,
    max_concurrent: int,
    max_launches_per_window: int,
    window_seconds: int,
) -> int:
    concurrent_remaining = max(0, max_concurrent - len(run_state.active_worktrees))
    recent_launches = [t for t in run_state.launch_history if now - t < window_seconds]
    rate_remaining = max(0, max_launches_per_window - len(recent_launches))
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
    base_priority = BASE_PRIORITY[task.priority]
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
) -> list[Task]:
    active_issue_numbers = {int(k) for k in run_state.active_worktrees}
    eligible = [
        t
        for t in candidate_tasks
        if not t.yaml_error
        and "status:external-lock" not in t.status_labels
        and t.issue_number not in active_issue_numbers
    ]
    slots = quota_available(
        run_state, now, max_concurrent, max_launches_per_window, window_seconds
    )
    scored = sorted(
        eligible,
        key=lambda t: (
            -compute_priority_score(t, eligible, run_state, now),
            t.issue_number,
        ),
    )
    return scored[:slots]
