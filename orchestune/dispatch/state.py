"""ディスパッチャーの実行状態（active/completed worktree）のモデル定義と永続化。"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestune.infra.json_state import read_json_with_recovery, write_json_atomic
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
    # 起動時点のcost modelで見積もったトークン量。実行中にfleet中央値が変化しても
    # 予約量を変動させず、複数サイクルを跨ぐ予算判定を安定させる。
    estimated_tokens: int | None = None
    # `estimated_tokens=None`が「起動時に不明として記録済み」なのか、本フィールド
    # 導入前の状態で未記録なのかを区別する。後者だけ現行cost modelへ縮退する。
    token_estimate_recorded: bool = False


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

    `last_reclaimed_at`は診断用（`run_state.json`を人が読むとき、そのタスクが
    最後にいつ回収されたかを示す）。上限判定には使わず、刈り込みにも使わない
    （PR#520レビュー5巡目対応: 経過時間による刈り込みは未完了タスクの回数を
    落とし得るため廃止した。`_retained_task_reclaim_counts`参照）。
    """

    count: int = 0
    last_reclaimed_at: float = 0.0
    # #512/PR#520レビュー9巡目対応(Codex P2): この回数が「まだGitHubへ反映できて
    # いない回収」の予約分であることを示す。反映（ラベル遷移・コメント）に失敗した
    # 回収を次サイクルで再試行する際、同じ回数を再利用して枠を二重に消費しない
    # ようにするために使う。反映が確定した時点で`False`へ戻す。
    pending: bool = False


@dataclass
class RunState:
    active_worktrees: dict[str, ActiveWorktree] = field(default_factory=dict)
    launch_history: list[float] = field(default_factory=list)
    completed_worktrees: list[CompletedWorktree] = field(default_factory=list)
    last_reconciled_at: float | None = None
    # #512: Issue番号 -> 回収回数。`max_task_reclaims`超過の判定に使う。
    task_reclaim_counts: dict[int, TaskReclaimRecord] = field(default_factory=dict)
    # #512/PR#520レビュー16巡目対応(Codex P2): 台帳のクローズ確認を1サイクルあたり
    # 一定件数に絞る際の走査位置。サイクルをまたいで進めることで、どの記録も
    # いずれ確認される（壁時計に基づくローテーションでは、一定周期で起動される
    # ディスパッチャーが同じ位置ばかり見てしまう組み合わせがあるため）。
    task_reclaim_lookup_cursor: int = 0


def _parse_task_reclaim_counts(raw: object) -> dict[int, TaskReclaimRecord]:
    """#512: `run_state.json`の`task_reclaim_counts`を検証しつつ復元する。

    後方互換: キー自体が無い（本フィールド導入前に書かれた`run_state.json`）
    場合は空の台帳＝「まだ一度も回収していない」として扱う。

    壊れた値の扱いも同じ考え方で倒す——壊れた値で上限判定を誤らせ、まだ再投入
    できるはずのタスクを即座に人間確認待ちへ落とす方が害が大きいため:

    - Issue番号として読めないキー・`count`が整数でない/負数/boolのエントリは、
      エントリごと捨てる（＝回数0）。
    - `last_reclaimed_at`だけが壊れている（欠落・非数値・非有限）場合は回数を
      活かし、時刻のみ`0.0`へ倒す（診断用の値であり、上限判定には影響しない）。
    - `pending`は`true`のときのみ真として扱う（欠落＝本フィールド導入前の
      `run_state.json`は「予約中ではない」＝次の回収で回数を1つ進める）。
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
            count=count,
            last_reclaimed_at=float(last_reclaimed_at),
            pending=value.get("pending") is True,
        )
    return records


def _parse_lookup_cursor(value: object) -> int:
    """#512: `task_reclaim_lookup_cursor`を検証しつつ復元する。

    欠落（本フィールド導入前の`run_state.json`）や壊れた値は`0`へ倒す。
    走査位置は毎サイクル記録数で丸められるため、0から始めても取りこぼしはない。
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


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
            estimated_tokens=value.get("estimated_tokens"),
            token_estimate_recorded=value.get(
                "token_estimate_recorded", "estimated_tokens" in value
            ),
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
        task_reclaim_lookup_cursor=_parse_lookup_cursor(
            data.get("task_reclaim_lookup_cursor")
        ),
    )


def _retained_task_reclaim_counts(state: RunState) -> dict[int, TaskReclaimRecord]:
    """#512: 回収回数の台帳は`prune_run_state`では一切刈り込まない。

    PR#520レビュー4〜5巡目対応(Codex P2): 件数上限でも経過時間でも、
    「まだ終わっていないタスクのカウンタ」を消し得る刈り込みは、本Issueが
    塞ごうとしている終端の無い経路を作り直してしまう。

    - 件数上限で古い順に追い出すと、失敗を繰り返す未完了タスクが上限を超えて
      存在する場合に、次の試行の前にそのタスクのカウンタが追い出される。
    - 経過時間で落とすと、既定の起動レート（`--max-launches-per-window`）に対して
      バックログが大きい場合など、回収から次の起動までが保持期間を超えるタスクの
      カウンタが落ちる。

    いずれも次の回収が1回目からやり直しになり、`max_task_reclaims`を素通り
    できてしまう。したがって記録の削除は「タスクが終わったことが分かる経路」——
    完了・`status:not-needed`によるクローズ（`dispatch_gc`）、独立検証レビュー
    合格によるクローズ（`dispatch_postcycle`）——にのみ委ねる。

    台帳の規模: エントリはGC回収が起きたIssueにしか作られず、完了時に削除される。
    残り得るのは「GC回収されたのち、上記以外の経路で終わったIssue」（人手での
    クローズ等）だけで、1件あたり数十バイトの `{"count": int,
    "last_reclaimed_at": float}` にすぎない。将来これを刈り込む必要が出た場合も、
    経過時間や件数ではなく「GitHub上でIssueが閉じているか」を根拠にすること。
    """
    return dict(state.task_reclaim_counts)


def _collect_protected_completed(
    completed_worktrees: list[CompletedWorktree],
    open_prs: Sequence[Any] | None,
) -> set[int]:
    if not open_prs:
        return set()
    open_pr_issues: set[int] = set()
    open_pr_branches: set[str] = set()
    for pr in open_prs:
        closes = getattr(pr, "closes_issue_numbers", ())
        if closes:
            open_pr_issues.update(closes)
        head_ref = getattr(pr, "head_ref", None)
        if head_ref:
            open_pr_branches.add(head_ref)

    protected_latest: dict[int | str, CompletedWorktree] = {}
    for cw in completed_worktrees:
        is_open = cw.issue_number in open_pr_issues or cw.branch in open_pr_branches
        if is_open:
            key = cw.issue_number
            existing = protected_latest.get(key)
            if existing is None or cw.completed_at >= existing.completed_at:
                protected_latest[key] = cw
    return {id(cw) for cw in protected_latest.values()}


def _prune_completed_worktrees(
    completed_worktrees: list[CompletedWorktree],
    min_completed_time: float,
    open_prs: Sequence[Any] | None,
    max_completed: int,
) -> list[CompletedWorktree]:
    protected_ids = _collect_protected_completed(completed_worktrees, open_prs)
    protected_cw = [cw for cw in completed_worktrees if id(cw) in protected_ids]
    unprotected_cw = [
        cw
        for cw in completed_worktrees
        if cw.completed_at >= min_completed_time and id(cw) not in protected_ids
    ]
    if len(protected_cw) >= max_completed:
        protected_selected = sorted(
            protected_cw, key=lambda x: x.completed_at, reverse=True
        )[:max_completed]
        unprotected_selected = []
    else:
        protected_selected = protected_cw
        remaining = max_completed - len(protected_selected)
        unprotected_selected = sorted(
            unprotected_cw, key=lambda x: x.completed_at, reverse=True
        )[:remaining]

    return sorted(
        protected_selected + unprotected_selected, key=lambda x: x.completed_at
    )


def prune_run_state(
    state: RunState,
    now: float | None = None,
    launch_window_seconds: float = 86400.0,
    completed_retention_seconds: float = 30 * 86400.0,
    open_prs: Sequence[Any] | None = None,
    max_completed_worktrees: int = 500,
) -> RunState:
    """#214: 長期運用による run_state.json の単調肥大化を防止するための有界刈り込み処理。"""
    import time

    current_time = time.time() if now is None else now
    min_launch_time = current_time - launch_window_seconds
    min_completed_time = current_time - completed_retention_seconds

    pruned_launch_history = [t for t in state.launch_history if t >= min_launch_time]
    pruned_completed = _prune_completed_worktrees(
        state.completed_worktrees,
        min_completed_time,
        open_prs,
        max_completed_worktrees,
    )

    return RunState(
        active_worktrees=state.active_worktrees,
        launch_history=pruned_launch_history,
        completed_worktrees=pruned_completed,
        last_reconciled_at=state.last_reconciled_at,
        task_reclaim_counts=_retained_task_reclaim_counts(state),
        task_reclaim_lookup_cursor=state.task_reclaim_lookup_cursor,
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
        "task_reclaim_lookup_cursor": state.task_reclaim_lookup_cursor,
    }
    write_json_atomic(path, data)
