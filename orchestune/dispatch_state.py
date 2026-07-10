"""ディスパッチャーの実行状態（active/completed worktree）のモデル定義と永続化。"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ActiveWorktree:
    issue_number: int
    branch: str
    worktree_path: str
    pid: int | None
    started_at: float
    declared_footprint: tuple[str, ...]
    recompute_count: int = 0
    forced_serial: bool = False
    external_id: str | None = None
    external_url: str | None = None


@dataclass
class CompletedWorktree:
    """#239: KPI B1/B2/D1（並列度・所要時間・稼働時間）算出に必要な完了履歴。
    ActiveWorktreeは完了時にrun_stateから削除されるため、開始・完了時刻を
    ここに退避しないと事後集計できない。"""

    issue_number: int
    subtask_id: str
    branch: str
    started_at: float
    completed_at: float
    recompute_count: int = 0
    forced_serial: bool = False


@dataclass
class RunState:
    active_worktrees: dict[str, ActiveWorktree] = field(default_factory=dict)
    launch_history: list[float] = field(default_factory=list)
    completed_worktrees: list[CompletedWorktree] = field(default_factory=list)


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
        )
        for value in data.get("completed_worktrees", [])
    ]
    return RunState(
        active_worktrees=active_worktrees,
        launch_history=list(data.get("launch_history", [])),
        completed_worktrees=completed_worktrees,
    )


def save_run_state(state: RunState, path: str | Path) -> None:
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
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
