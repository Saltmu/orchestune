"""ディスパッチャーの実行状態（active/completed worktree）のモデル定義と永続化。"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestune.json_state import read_json_with_recovery, write_json_atomic
from orchestune.models import Usage


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
    usage: Usage | None = None


@dataclass
class TaskReclaimRecord:
    """#512: タスク（Issue）ごとのゾンビ／タイムアウト回収回数の台帳エントリ。

    `ActiveWorktree`ではなくRunState側の台帳に置く理由: 回収時に対象の
    `ActiveWorktree`は`run_state`から削除されるため、エントリと寿命を共有すると
    「回収 → 再投入 → 再起動」を跨いで回数を保持できず、上限判定が常に0から
    やり直しになってしまう。

    `last_reclaimed_at`は`prune_run_state`が古い台帳エントリを刈り込むための
    もので、上限判定そのものには使わない。
    """

    count: int = 0
    last_reclaimed_at: float = 0.0


@dataclass
class RunState:
    active_worktrees: dict[str, ActiveWorktree] = field(default_factory=dict)
    launch_history: list[float] = field(default_factory=list)
    completed_worktrees: list[CompletedWorktree] = field(default_factory=list)
    last_reconciled_at: float | None = None
    # #512: Issue番号 -> 回収回数。`max_task_reclaims`超過の判定に使う。
    task_reclaim_counts: dict[int, TaskReclaimRecord] = field(default_factory=dict)


def _parse_task_reclaim_counts(raw: object) -> dict[int, TaskReclaimRecord]:
    """#512: `run_state.json`の`task_reclaim_counts`を検証しつつ復元する。

    後方互換: キー自体が無い（本フィールド導入前に書かれた`run_state.json`）
    場合は空の台帳＝「まだ一度も回収していない」として扱う。

    壊れた値の扱いも同じ考え方で倒す——壊れた値で上限判定を誤らせ、まだ再投入
    できるはずのタスクを即座に人間確認待ちへ落とす方が害が大きいため:

    - Issue番号として読めないキー・`count`が整数でない/負数/boolのエントリは、
      エントリごと捨てる（＝回数0）。
    - `last_reclaimed_at`だけが壊れている（欠落・非数値・非有限）場合は回数を
      活かし、時刻のみ`0.0`（最古）へ倒す。この場合、当該エントリは次回の
      `prune_run_state`で保持期間切れとして刈り込まれ得る（activeなタスクは保護）。
    """
    if not isinstance(raw, dict):
        return {}
    records: dict[int, TaskReclaimRecord] = {}
    for key, value in raw.items():
        try:
            issue_number = int(key)
        except (TypeError, ValueError):
            # JSONのオブジェクトキーは文字列なので、Issue番号として読めない
            # キー（手で編集された等）はエントリごと捨てる。
            continue
        if not isinstance(value, dict):
            continue
        count = value.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            continue
        last_reclaimed_at = value.get("last_reclaimed_at")
        if (
            isinstance(last_reclaimed_at, bool)
            or not isinstance(last_reclaimed_at, int | float)
            or not math.isfinite(last_reclaimed_at)
        ):
            last_reclaimed_at = 0.0
        records[issue_number] = TaskReclaimRecord(
            count=count, last_reclaimed_at=float(last_reclaimed_at)
        )
    return records


def load_run_state(path: str | Path) -> RunState:
    data = read_json_with_recovery(path, label="run_state.json")
    if data is None:
        return RunState(active_worktrees={}, launch_history=[])
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
            usage=Usage(**value["usage"]) if value.get("usage") else None,
        )
        for value in data.get("completed_worktrees", [])
    ]
    return RunState(
        active_worktrees=active_worktrees,
        launch_history=list(data.get("launch_history", [])),
        completed_worktrees=completed_worktrees,
        last_reconciled_at=data.get("last_reconciled_at"),
        task_reclaim_counts=_parse_task_reclaim_counts(data.get("task_reclaim_counts")),
    )


def _prune_task_reclaim_counts(
    state: RunState,
    min_reclaimed_time: float,
    max_task_reclaim_records: int,
) -> dict[int, TaskReclaimRecord]:
    """#512: 回収回数の台帳を有界に保つ（`prune_run_state`のヘルパー）。

    active なタスクのエントリを最優先で保護し、残りの枠を「直近に回収された順」で
    埋める。刈り込まれたエントリはカウンタが0へ戻るが、それは
    `completed_retention_seconds`（既定30日）以上放置されたタスクか、
    台帳が `max_task_reclaim_records` 件を超えた場合に限られる。
    """
    active_issue_numbers = {
        active.issue_number for active in state.active_worktrees.values()
    }
    protected = {
        issue_number: record
        for issue_number, record in state.task_reclaim_counts.items()
        if issue_number in active_issue_numbers
    }
    remaining_capacity = max(max_task_reclaim_records - len(protected), 0)
    retained = sorted(
        (
            (issue_number, record)
            for issue_number, record in state.task_reclaim_counts.items()
            if issue_number not in protected
            and record.last_reclaimed_at >= min_reclaimed_time
        ),
        key=lambda item: item[1].last_reclaimed_at,
        reverse=True,
    )[:remaining_capacity]
    return {**protected, **dict(retained)}


def prune_run_state(
    state: RunState,
    now: float | None = None,
    launch_window_seconds: float = 86400.0,
    completed_retention_seconds: float = 30 * 86400.0,
    open_prs: Sequence[Any] | None = None,
    max_completed_worktrees: int = 500,
    max_task_reclaim_records: int = 500,
) -> RunState:
    """#214: 長期運用による run_state.json の単調肥大化を防止するための有界刈り込み処理。

    保持ポリシー:
    - `launch_history`: `launch_window_seconds`（デフォルト24時間 / 設定の `window_seconds`）以内の起動タイムスタンプのみ保持。
    - `completed_worktrees`: 直近30日間（デフォルト 2592000秒）以内の完了履歴のみ保持。
      ただし、現在 open 状態にある PR (`open_prs`) の重複判定に必要な `last_completed` (commit_sha) を保護するため、
      open PR に紐づく Issue / ブランチの最新 1 件の `CompletedWorktree` は経過時間に関わらず保護する。
      さらに、全完了履歴の件数は `max_completed_worktrees` 件を超えないよう有界に保持する。
    - `task_reclaim_counts` (#512): `completed_retention_seconds` 以内に回収された台帳エントリのみを、
      最大 `max_task_reclaim_records` 件まで保持する。ただし現在 active なタスクのエントリは、
      回収からの経過時間・件数上限に関わらず保護する（実行中のタスクの回収回数を失うと、
      `max_task_reclaims` による上限判定が0からやり直しになるため）。
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

    protected_cw = [cw for cw in state.completed_worktrees if id(cw) in protected_ids]
    unprotected_cw = [
        cw
        for cw in state.completed_worktrees
        if cw.completed_at >= min_completed_time and id(cw) not in protected_ids
    ]

    if len(protected_cw) >= max_completed_worktrees:
        protected_selected = sorted(
            protected_cw, key=lambda x: x.completed_at, reverse=True
        )[:max_completed_worktrees]
        unprotected_selected = []
    else:
        protected_selected = protected_cw
        remaining_capacity = max_completed_worktrees - len(protected_selected)
        unprotected_selected = sorted(
            unprotected_cw, key=lambda x: x.completed_at, reverse=True
        )[:remaining_capacity]

    pruned_completed = sorted(
        protected_selected + unprotected_selected, key=lambda x: x.completed_at
    )

    pruned_task_reclaim_counts = _prune_task_reclaim_counts(
        state,
        min_reclaimed_time=min_completed_time,
        max_task_reclaim_records=max_task_reclaim_records,
    )

    return RunState(
        active_worktrees=state.active_worktrees,
        launch_history=pruned_launch_history,
        completed_worktrees=pruned_completed,
        last_reconciled_at=state.last_reconciled_at,
        task_reclaim_counts=pruned_task_reclaim_counts,
    )


def save_run_state(
    state: RunState,
    path: str | Path,
    now: float | None = None,
    launch_window_seconds: float = 86400.0,
    completed_retention_seconds: float = 30 * 86400.0,
    open_prs: Sequence[Any] | None = None,
    max_completed_worktrees: int = 500,
    max_task_reclaim_records: int = 500,
) -> None:
    state = prune_run_state(
        state,
        now=now,
        launch_window_seconds=launch_window_seconds,
        completed_retention_seconds=completed_retention_seconds,
        open_prs=open_prs,
        max_completed_worktrees=max_completed_worktrees,
        max_task_reclaim_records=max_task_reclaim_records,
    )
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
        # JSONのオブジェクトキーは文字列のみのため、Issue番号は明示的に
        # str()で書き出す（読み戻し時に_parse_task_reclaim_countsがintへ戻す）。
        "task_reclaim_counts": {
            str(issue_number): dataclasses.asdict(record)
            for issue_number, record in state.task_reclaim_counts.items()
        },
    }
    write_json_atomic(path, data)
