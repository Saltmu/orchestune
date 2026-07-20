"""ディスパッチャーの実行状態（active/completed worktree）のモデル定義と永続化。"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ActiveWorktree:
    issue_number: int
    branch: str
    worktree_path: str
    pid: int | None
    started_at: float | None
    declared_footprint: tuple[str, ...]
    recompute_count: int = 0
    forced_serial: bool = False
    external_id: str | None = None
    external_url: str | None = None
    base_branch: str = "origin/main"


@dataclass
class CompletedWorktree:
    """#239: KPI B1/B2/D1（並列度・所要時間・稼働時間）算出に必要な完了履歴。
    ActiveWorktreeは完了時にrun_stateから削除されるため、開始・完了時刻を
    ここに退避しないと事後集計できない。"""

    issue_number: int
    subtask_id: str
    branch: str
    started_at: float | None
    completed_at: float
    recompute_count: int = 0
    forced_serial: bool = False
    commit_sha: str | None = None
    base_branch: str = "origin/main"


@dataclass
class RunState:
    active_worktrees: dict[str, ActiveWorktree] = field(default_factory=dict)
    launch_history: list[float] = field(default_factory=list)
    completed_worktrees: list[CompletedWorktree] = field(default_factory=list)
    last_reconciled_at: float | None = None


def load_run_state(path: str | Path) -> RunState:
    path = Path(path)
    if not path.exists():
        return RunState(active_worktrees={}, launch_history=[])
    data = json.loads(path.read_text(encoding="utf-8"))
    active_worktrees = {
        key: ActiveWorktree(
            issue_number=value["issue_number"],
            branch=value["branch"],
            worktree_path=value["worktree_path"],
            pid=value["pid"],
            started_at=value["started_at"],
            declared_footprint=tuple(value["declared_footprint"]),
            recompute_count=value.get("recompute_count", 0),
            forced_serial=value.get("forced_serial", False),
            external_id=value.get("external_id"),
            external_url=value.get("external_url"),
            base_branch=value.get("base_branch", "origin/main"),
        )
        for key, value in data.get("active_worktrees", {}).items()
    }
    completed_worktrees = [
        CompletedWorktree(
            issue_number=value["issue_number"],
            subtask_id=value["subtask_id"],
            branch=value["branch"],
            started_at=value["started_at"],
            completed_at=value["completed_at"],
            recompute_count=value.get("recompute_count", 0),
            forced_serial=value.get("forced_serial", False),
            commit_sha=value.get("commit_sha"),
            base_branch=value.get("base_branch", "origin/main"),
        )
        for value in data.get("completed_worktrees", [])
    ]
    return RunState(
        active_worktrees=active_worktrees,
        launch_history=list(data.get("launch_history", [])),
        completed_worktrees=completed_worktrees,
        last_reconciled_at=data.get("last_reconciled_at"),
    )


def prune_run_state(
    state: RunState,
    now: float | None = None,
    launch_window_seconds: float = 86400.0,
    completed_retention_seconds: float = 30 * 86400.0,
    open_prs: Sequence[Any] | None = None,
    max_completed_worktrees: int = 500,
) -> RunState:
    """#214: 長期運用による run_state.json の単調肥大化を防止するための有界刈り込み処理。

    保持ポリシー:
    - `launch_history`: `launch_window_seconds`（デフォルト24時間 / 設定の `window_seconds`）以内の起動タイムスタンプのみ保持。
    - `completed_worktrees`: 直近30日間（デフォルト 2592000秒）以内の完了履歴のみ保持。
      ただし、現在 open 状態にある PR (`open_prs`) の重複判定に必要な `last_completed` (commit_sha) を保護するため、
      open PR に紐づく Issue / ブランチの最新 1 件の `CompletedWorktree` は経過時間に関わらず保護する。
      さらに、全完了履歴の件数は `max_completed_worktrees` 件を超えないよう有界に保持する。
    """
    import time

    current_time = time.time() if now is None else now
    min_launch_time = current_time - launch_window_seconds
    min_completed_time = current_time - completed_retention_seconds

    pruned_launch_history = [t for t in state.launch_history if t >= min_launch_time]

    open_pr_issues: set[int] = set()
    open_pr_branches: set[str] = set()
    if open_prs:
        for pr in open_prs:
            closes = getattr(pr, "closes_issue_numbers", ())
            if closes:
                open_pr_issues.update(closes)
            head_ref = getattr(pr, "head_ref", None)
            if head_ref:
                open_pr_branches.add(head_ref)

    protected_latest: dict[int | str, CompletedWorktree] = {}
    if open_prs:
        for cw in state.completed_worktrees:
            is_open_target = (
                cw.issue_number in open_pr_issues or cw.branch in open_pr_branches
            )
            if is_open_target:
                key = cw.issue_number
                existing = protected_latest.get(key)
                if existing is None or cw.completed_at >= existing.completed_at:
                    protected_latest[key] = cw

    protected_ids = set(id(cw) for cw in protected_latest.values())

    pruned_completed = [
        cw
        for cw in state.completed_worktrees
        if cw.completed_at >= min_completed_time or id(cw) in protected_ids
    ]

    if len(pruned_completed) > max_completed_worktrees:
        pruned_completed = pruned_completed[-max_completed_worktrees:]

    return RunState(
        active_worktrees=state.active_worktrees,
        launch_history=pruned_launch_history,
        completed_worktrees=pruned_completed,
        last_reconciled_at=state.last_reconciled_at,
    )


def save_run_state(
    state: RunState,
    path: str | Path,
    now: float | None = None,
    launch_window_seconds: float = 86400.0,
    completed_retention_seconds: float = 30 * 86400.0,
    open_prs: Sequence[Any] | None = None,
    max_completed_worktrees: int = 500,
) -> None:
    state = prune_run_state(
        state,
        now=now,
        launch_window_seconds=launch_window_seconds,
        completed_retention_seconds=completed_retention_seconds,
        open_prs=open_prs,
        max_completed_worktrees=max_completed_worktrees,
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "active_worktrees": {
            key: dataclasses.asdict(value)
            for key, value in state.active_worktrees.items()
        },
        "launch_history": state.launch_history,
        "completed_worktrees": [
            dataclasses.asdict(value) for value in state.completed_worktrees
        ],
        "last_reconciled_at": state.last_reconciled_at,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
