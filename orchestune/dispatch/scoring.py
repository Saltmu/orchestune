"""ディスパッチ優先度の算出・選出ロジック。

#660以降、選出は2つのモードを持つ。

- `critical-path`（既定）: Precedence DAGのbottom levelと後続解放数を価値へ、
  完了履歴から推定したトークン量と手戻りリスクをコストへ組み込み、resource制約
  （同時実行数・起動レート・トークンウィンドウ・競合）の下で貪欲に選ぶ。
- `legacy`: #660以前の`compute_priority_score`（base priority×待ち時間bonus＋
  partial progress bonus）そのまま。段階導入・切り戻し用の互換モード。

いずれのモードでも、選出は`(candidate_tasks, run_state, now, 設定)`だけの純粋関数
であり、同じ入力からは常に同じ結果を返す。
"""

from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime

from orchestune.dag.models import ConflictGraph
from orchestune.dispatch.cost_model import (
    ESTIMATE_SOURCE_DEFAULT,
    CostEstimate,
    CostModel,
    build_cost_model,
)
from orchestune.dispatch.critical_path import (
    PrecedenceRanks,
    compute_precedence_ranks,
    pending_tasks,
)
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
    "remaining_token_budget",
    "compute_priority_score",
    "select_next_tasks",
    "select_tasks_with_decisions",
    "decision_to_dict",
    "ScoreComponents",
    "SchedulingDecision",
    "SchedulingResult",
    "SCHEDULING_MODES",
    "SCHEDULING_MODE_CRITICAL_PATH",
    "SCHEDULING_MODE_LEGACY",
    "reconcile_decisions_with_launches",
]

TIME_BONUS_WEIGHT = 0.5
PROGRESS_BONUS = 1.0

SCHEDULING_MODE_CRITICAL_PATH = "critical-path"
SCHEDULING_MODE_LEGACY = "legacy"
SCHEDULING_MODES = (SCHEDULING_MODE_CRITICAL_PATH, SCHEDULING_MODE_LEGACY)

# critical-pathモードの重み。critical path由来のbonusとcost由来のpenaltyは
# いずれも正規化済みの[0, 1]に重みを掛けたものなので、「critical path上にある」
# ことが優先度ラベルを上書きしないよう総和を制限する。
#
# PR#665レビュー指摘(Codex P2): ここで制限すべきは片方の候補が得られるbonusの
# 合計ではなく、2候補**間**で開き得る差＝`QUALITY_SPAN`である。低priority側が
# bonusを満額得ると同時に高priority側がpenaltyを満額被り得るため、bonusの合計
# だけを1.0未満に抑えても、候補間では最大`bonus + penalty`だけ差がついて
# priority 1段階を逆転できてしまう（例: low 1.0 + 0.9 = 1.9 > medium 2.0 - 0.5
# = 1.5）。4項の総和を`MIN_PRIORITY_GAP`未満に保つことで逆転を閉じる。
CRITICAL_PATH_WEIGHT = 0.3
UNLOCK_WEIGHT = 0.25
TOKEN_COST_WEIGHT = 0.25
REWORK_WEIGHT = 0.15

# 2候補間でcritical path/cost由来の項が開き得る差の上限。
QUALITY_SPAN = CRITICAL_PATH_WEIGHT + UNLOCK_WEIGHT + TOKEN_COST_WEIGHT + REWORK_WEIGHT

# 隣接するpriority段階の最小の差。`QUALITY_SPAN`がこれ未満である限り、待ち時間が
# 等しい候補同士でpriorityの順序が逆転することはない（テストで機械的に検証する）。
MIN_PRIORITY_GAP = min(
    higher - lower
    for lower, higher in itertools.pairwise(sorted(BASE_PRIORITY.values()))
)

# 待ち時間項の重み。候補集合内の最小待ち時間との差を「ウィンドウ何個分か」で
# 測るため非有界であり、これが飢餓回避の終端保証になる（BOUNDED_SCORE_SPAN参照）。
AGING_WEIGHT = 1.0

# aging以外の全項が取り得る幅の上限。候補集合内の最小待ち時間より
# `BOUNDED_SCORE_SPAN`ウィンドウ以上長く待っているタスクは、他のどの成分の
# 組み合わせよりも高いスコアになる。したがって、resourceが供給され続ける限り
# 「継続的にeligibleなのに永久に選ばれないタスク」は存在し得ない。
BOUNDED_SCORE_SPAN = (
    max(BASE_PRIORITY.values())
    - min(BASE_PRIORITY.values())
    + PROGRESS_BONUS
    + QUALITY_SPAN
)

REASON_SELECTED = "selected"
REASON_CONFLICT = "conflict"
REASON_QUOTA_EXHAUSTED = "quota-exhausted"
REASON_TOKEN_BUDGET = "token-budget"
# PR#665レビュー指摘(Codex P2): 選出されたが実起動に失敗した（起動枠の予約が
# 取れなかった／`create_worktree_and_launch`が失敗した）タスクの理由。
REASON_LAUNCH_FAILED = "launch-failed"


@dataclass(frozen=True)
class ScoreComponents:
    """1タスク分のスコア内訳（レポートで選定理由を説明するために保持する）。"""

    base_priority: float = 0.0
    aging: float = 0.0
    critical_path: float = 0.0
    unlock: float = 0.0
    progress: float = 0.0
    token_penalty: float = 0.0
    rework_penalty: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.base_priority
            + self.aging
            + self.critical_path
            + self.unlock
            + self.progress
            - self.token_penalty
            - self.rework_penalty
        )


@dataclass(frozen=True)
class SchedulingDecision:
    """候補1件に対する選定結果。選ばれなかった候補にも必ず`reason`が付く。"""

    issue_number: int
    subtask_id: str
    mode: str
    score: float
    components: ScoreComponents
    bottom_level: float = 0.0
    unlocked_count: int = 0
    downstream_count: int = 0
    estimated_tokens: int | None = None
    estimated_duration_seconds: float = 0.0
    estimate_source: str = ESTIMATE_SOURCE_DEFAULT
    selected: bool = False
    reason: str = ""


@dataclass(frozen=True)
class SchedulingResult:
    selected: list[Task]
    decisions: list[SchedulingDecision]


def decision_to_dict(decision: SchedulingDecision) -> dict:
    """イベントログ／JSONレポート用のプレーンなdictへ変換する。"""
    return dataclasses.asdict(decision)


def reconcile_decisions_with_launches(
    decisions: list[SchedulingDecision], launched: Iterable[Task]
) -> list[SchedulingDecision]:
    """実起動に失敗したタスクの判定を`launch-failed`へ落とす。

    PR#665レビュー指摘(Codex P2): 選出（scheduling）と実起動（launch）は別物で、
    `_apply_task_launches`は起動枠の予約が取れなかったタスクや
    `create_worktree_and_launch`が失敗したタスクを飛ばして部分集合を返す。
    選出時点の判定をそのまま出すと、`CycleReport.selected`に居ないタスクまで
    レポートに`✅ 起動`と表示され、診断情報として信用できなくなる。
    """
    launched_issues = {task.issue_number for task in launched}
    return [
        decision
        if not decision.selected or decision.issue_number in launched_issues
        else replace(decision, selected=False, reason=REASON_LAUNCH_FAILED)
        for decision in decisions
    ]


def remaining_token_budget(
    run_state: RunState,
    now: float,
    window_seconds: int,
    max_tokens_per_window: int | None,
) -> int | None:
    """ウィンドウ内に残っているトークン予算。上限未設定なら`None`（無制限）。"""
    if max_tokens_per_window is None:
        return None
    consumed = sum(
        w.usage.total_tokens
        for w in run_state.completed_worktrees
        if now - w.completed_at < window_seconds
        and w.usage is not None
        and w.usage.total_tokens is not None
    )
    return max(0, max_tokens_per_window - consumed)


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
    budget = remaining_token_budget(
        run_state, now, window_seconds, max_tokens_per_window
    )
    if budget is not None and budget <= 0:
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
    """#660以前のスコアリング（`legacy`モードおよび互換切り戻し用）。"""
    base_priority = BASE_PRIORITY.get(task.priority, BASE_PRIORITY["medium"])
    waits = [_wait_seconds(t, run_state, now) for t in all_candidate_tasks]
    avg_wait = sum(waits) / len(waits) if waits else 0.0

    time_bonus = 0.0
    if avg_wait > 0:
        wait = _wait_seconds(task, run_state, now)
        time_bonus = max(0.0, (wait / avg_wait) - 1.0) * TIME_BONUS_WEIGHT

    progress_factor = PROGRESS_BONUS if task.progress_partial else 0.0
    return base_priority * (1.0 + time_bonus) + progress_factor


@dataclass(frozen=True)
class _ScoringInputs:
    """1サイクル分の候補集合に対して一度だけ求めれば足りる共通項。"""

    ranks: PrecedenceRanks
    cost_model: CostModel
    estimates: dict[str, CostEstimate]
    min_wait: float
    max_bottom_level: float
    max_downstream: float
    max_tokens: float
    window_seconds: float


def _normalized(value: float, maximum: float) -> float:
    return value / maximum if maximum > 0 else 0.0


def _build_scoring_inputs(
    eligible: list[Task],
    run_state: RunState,
    now: float,
    window_seconds: int,
    known_tasks: Iterable[Task] | None,
) -> _ScoringInputs:
    graph_tasks = pending_tasks(known_tasks if known_tasks is not None else eligible)
    cost_model = build_cost_model(run_state)
    estimates = {task.subtask_id: cost_model.estimate(task) for task in graph_tasks}
    ranks = compute_precedence_ranks(
        graph_tasks,
        {
            subtask_id: estimate.duration_seconds
            for subtask_id, estimate in estimates.items()
        },
    )
    token_values = [
        estimate.tokens
        for estimate in (estimates.get(task.subtask_id) for task in eligible)
        if estimate is not None and estimate.tokens is not None
    ]
    return _ScoringInputs(
        ranks=ranks,
        cost_model=cost_model,
        estimates=estimates,
        min_wait=min(
            (_wait_seconds(task, run_state, now) for task in eligible), default=0.0
        ),
        max_bottom_level=max(
            (ranks.bottom_level_of(task.subtask_id) for task in eligible), default=0.0
        ),
        max_downstream=float(
            max(
                (ranks.downstream_count(task.subtask_id) for task in eligible),
                default=0,
            )
        ),
        max_tokens=float(max(token_values, default=0)),
        window_seconds=max(1.0, float(window_seconds)),
    )


def _critical_path_decision(
    task: Task, run_state: RunState, now: float, inputs: _ScoringInputs
) -> SchedulingDecision:
    estimate = inputs.estimates.get(task.subtask_id) or inputs.cost_model.estimate(task)
    bottom_level = inputs.ranks.bottom_level_of(task.subtask_id)
    downstream = inputs.ranks.downstream_count(task.subtask_id)
    wait = _wait_seconds(task, run_state, now)
    components = ScoreComponents(
        base_priority=BASE_PRIORITY.get(task.priority, BASE_PRIORITY["medium"]),
        aging=AGING_WEIGHT * (wait - inputs.min_wait) / inputs.window_seconds,
        critical_path=CRITICAL_PATH_WEIGHT
        * _normalized(bottom_level, inputs.max_bottom_level),
        unlock=UNLOCK_WEIGHT * _normalized(downstream, inputs.max_downstream),
        progress=PROGRESS_BONUS if task.progress_partial else 0.0,
        token_penalty=TOKEN_COST_WEIGHT
        * _normalized(float(estimate.tokens or 0), inputs.max_tokens),
        rework_penalty=REWORK_WEIGHT * estimate.rework_risk,
    )
    return SchedulingDecision(
        issue_number=task.issue_number,
        subtask_id=task.subtask_id,
        mode=SCHEDULING_MODE_CRITICAL_PATH,
        score=components.total,
        components=components,
        bottom_level=bottom_level,
        unlocked_count=inputs.ranks.unlocked_count(task.subtask_id),
        downstream_count=downstream,
        estimated_tokens=estimate.tokens,
        estimated_duration_seconds=estimate.duration_seconds,
        estimate_source=estimate.source,
    )


def _legacy_decision(
    task: Task, eligible: list[Task], run_state: RunState, now: float
) -> SchedulingDecision:
    score = compute_priority_score(task, eligible, run_state, now)
    return SchedulingDecision(
        issue_number=task.issue_number,
        subtask_id=task.subtask_id,
        mode=SCHEDULING_MODE_LEGACY,
        score=score,
        components=ScoreComponents(base_priority=score),
    )


def _rank_candidates(
    eligible: list[Task],
    run_state: RunState,
    now: float,
    window_seconds: int,
    scheduling_mode: str,
    known_tasks: Iterable[Task] | None,
) -> list[tuple[Task, SchedulingDecision]]:
    """候補をスコア降順（同点はissue番号昇順）に並べる。

    `scheduling_mode`が`legacy`以外の値のときはcritical-pathモデルを使う。
    設定ファイル・CLIの両方が`SCHEDULING_MODES`で値を検証済みのため、ここで
    未知の値のために追加のエラー経路を作らない。
    """
    if scheduling_mode == SCHEDULING_MODE_LEGACY:
        decisions = [_legacy_decision(t, eligible, run_state, now) for t in eligible]
    else:
        inputs = _build_scoring_inputs(
            eligible, run_state, now, window_seconds, known_tasks
        )
        decisions = [
            _critical_path_decision(t, run_state, now, inputs) for t in eligible
        ]
    return sorted(
        zip(eligible, decisions, strict=True),
        key=lambda pair: (-pair[1].score, pair[1].issue_number),
    )


def _eligible_tasks(candidate_tasks: list[Task], run_state: RunState) -> list[Task]:
    active_issue_numbers = {int(k) for k in run_state.active_worktrees}
    return [
        t
        for t in candidate_tasks
        if not t.yaml_error
        and "status:external-lock" not in t.status_labels
        and "status:blocked-recompute" not in t.status_labels
        and t.issue_number not in active_issue_numbers
    ]


def _apply_resource_constraints(
    ranked: list[tuple[Task, SchedulingDecision]],
    slots: int,
    token_budget: int | None,
    conflict_graph: ConflictGraph | None,
    active_subtask_ids: set[str] | None,
) -> SchedulingResult:
    """クオータ・競合・トークン予算の順に制約を当てて貪欲に選ぶ。"""
    unavailable = set(active_subtask_ids or ())
    selected: list[Task] = []
    decisions: list[SchedulingDecision] = []
    projected_tokens = 0

    for task, decision in ranked:
        if len(selected) >= slots:
            decisions.append(replace(decision, reason=REASON_QUOTA_EXHAUSTED))
            continue
        if conflict_graph is not None and any(
            conflict_graph.has_conflict(task.subtask_id, other) for other in unavailable
        ):
            decisions.append(replace(decision, reason=REASON_CONFLICT))
            continue
        cost = decision.estimated_tokens
        # 先頭1件はトークン予算で弾かない。単体で残予算を超える見積りのタスク
        # しか無いときにキューが永久に進まなくなる（終端の無い経路になる）ため。
        # ウィンドウ上限そのものは`quota_available`のハードゲートが守る。
        if (
            token_budget is not None
            and selected
            and cost is not None
            and projected_tokens + cost > token_budget
        ):
            decisions.append(replace(decision, reason=REASON_TOKEN_BUDGET))
            continue
        selected.append(task)
        unavailable.add(task.subtask_id)
        projected_tokens += cost or 0
        decisions.append(replace(decision, selected=True, reason=REASON_SELECTED))

    return SchedulingResult(selected=selected, decisions=decisions)


def select_tasks_with_decisions(
    candidate_tasks: list[Task],
    run_state: RunState,
    now: float,
    max_concurrent: int,
    max_launches_per_window: int,
    window_seconds: int,
    max_tokens_per_window: int | None = None,
    conflict_graph: ConflictGraph | None = None,
    active_subtask_ids: set[str] | None = None,
    scheduling_mode: str = SCHEDULING_MODE_CRITICAL_PATH,
    known_tasks: Iterable[Task] | None = None,
) -> SchedulingResult:
    """起動するタスクを選び、全候補分の選定理由付き内訳を併せて返す。

    `known_tasks`はPrecedence DAGを組み立てる母集団（通常はサイクルが見ている
    全タスク）。省略時は候補集合そのものを使う。
    """
    eligible = _eligible_tasks(candidate_tasks, run_state)
    slots = quota_available(
        run_state,
        now,
        max_concurrent,
        max_launches_per_window,
        window_seconds,
        max_tokens_per_window=max_tokens_per_window,
    )
    ranked = _rank_candidates(
        eligible, run_state, now, window_seconds, scheduling_mode, known_tasks
    )
    return _apply_resource_constraints(
        ranked,
        slots,
        remaining_token_budget(run_state, now, window_seconds, max_tokens_per_window),
        conflict_graph,
        active_subtask_ids,
    )


def select_next_tasks(
    candidate_tasks: list[Task],
    run_state: RunState,
    now: float,
    max_concurrent: int,
    max_launches_per_window: int,
    window_seconds: int,
    max_tokens_per_window: int | None = None,
    conflict_graph: ConflictGraph | None = None,
    active_subtask_ids: set[str] | None = None,
    scheduling_mode: str = SCHEDULING_MODE_CRITICAL_PATH,
    known_tasks: Iterable[Task] | None = None,
) -> list[Task]:
    """選出されたタスクだけを返す薄いラッパー（選定理由が不要な呼び出し向け）。"""
    return select_tasks_with_decisions(
        candidate_tasks,
        run_state,
        now,
        max_concurrent,
        max_launches_per_window,
        window_seconds,
        max_tokens_per_window=max_tokens_per_window,
        conflict_graph=conflict_graph,
        active_subtask_ids=active_subtask_ids,
        scheduling_mode=scheduling_mode,
        known_tasks=known_tasks,
    ).selected
