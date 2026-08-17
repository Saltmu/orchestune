"""Maintenance GC Phase コーディネーター。

ゾンビ・タイムアウトしたactive worktreeの回収を行う。dirty worktreeを人間
確認まで保留した完了判定(#212)を、同一サイクル内のゾンビGCが上書きしない
よう、該当worktreeをGC対象から明示的に除外する。
"""

from __future__ import annotations

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_gc import _collect_zombies_and_timeouts
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import RunState


def run_gc_phase(
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
    completion_events: list[dict],
) -> list[dict]:
    """ゾンビ/タイムアウトGCを実行し、マージ済みcompletion_eventsを返す。

    #212: dirty worktreeを人間確認まで保留する完了判定を、直後の
    ゾンビGCが同一サイクル内で上書きしないよう、該当worktreeを明示的に除外する。
    """
    held_worktree_paths = {
        event["worktree_path"]
        for event in completion_events
        if event.get("action") == "completion_skipped_dirty_worktree"
        and event.get("worktree_path")
    }
    gc_events = _collect_zombies_and_timeouts(
        run_state,
        tasks_by_issue,
        config,
        held_worktree_paths=held_worktree_paths,
    )
    return [*completion_events, *gc_events]
