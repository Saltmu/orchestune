"""Maintenance GC Phase コーディネーター。

ゾンビ・タイムアウトしたactive worktreeの回収を行う。dirty worktreeを人間
確認まで保留した完了判定(#212)を、同一サイクル内のゾンビGCが上書きしない
よう、該当worktreeをGC対象から明示的に除外する。
"""

from __future__ import annotations

from collections.abc import Sequence

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.gc import _collect_zombies_and_timeouts
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import RunState
from orchestune.models import PrRecord


def run_gc_phase(
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
    completion_events: list[dict],
    open_prs: Sequence[PrRecord] | None = None,
) -> list[dict]:
    """ゾンビ/タイムアウトGCを実行し、マージ済みcompletion_eventsを返す。

    #212: dirty worktreeを人間確認まで保留する完了判定を、直後の
    ゾンビGCが同一サイクル内で上書きしないよう、該当worktreeを明示的に除外する。

    #512/PR#520レビュー2巡目対応: 回収回数の永続化はラベル遷移より先に行うため、
    GC内から`save_run_state`を呼ぶ。刈り込みでレート制限用の起動履歴や重複起動
    判定用の完了履歴を落とさないよう、サイクルが取得済みのPR一覧を渡す。
    """
    held_worktree_paths = {
        event["worktree_path"]
        for event in completion_events
        if event.get("action")
        in ("completion_skipped_dirty_worktree", "completion_skipped_forge_error")
        and event.get("worktree_path")
    }
    gc_events = _collect_zombies_and_timeouts(
        run_state,
        tasks_by_issue,
        config,
        held_worktree_paths=held_worktree_paths,
        open_prs=open_prs,
    )
    return [*completion_events, *gc_events]
