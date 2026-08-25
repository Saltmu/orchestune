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
# PR#665レビュー指摘(Codex P2): スコアリング以前に候補から外れる理由。
REASON_YAML_ERROR = "yaml-error"
REASON_EXTERNAL_LOCK = "external-lock"
REASON_BLOCKED_RECOMPUTE = "blocked-recompute"
REASON_ALREADY_ACTIVE = "already-active"


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
    exact_bottom_level: bool = True
    exact_downstream: bool = True
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


@dataclass(frozen=True)
class _TokenBudget:
    """1サイクル分のバッチが使ってよいトークン量と、免除枠の可否。"""

    # 残予算。`None`は上限未設定（無制限）。
    remaining: int | None
    # 先頭1件を予算判定から除外してよいか。
    exempt_first: bool


def _reserved_token_estimate(run_state: RunState, cost_model: CostModel) -> int:
    """まだ完了していない起動が、このウィンドウで消費すると見込まれるトークン量。"""
    return sum(
        tokens
        for active in run_state.active_worktrees.values()
        if (
            tokens := (
                active.estimated_tokens
                if active.token_estimate_recorded
                else cost_model.tokens_for_issue(active.issue_number)
            )
        )
        is not None
    )


def _token_budget(
    run_state: RunState,
    now: float,
    window_seconds: int,
    max_tokens_per_window: int | None,
    cost_model: CostModel,
) -> _TokenBudget:
    """バッチのトークン予算を、実行中の起動の見込み消費を差し引いて求める。

    PR#665レビュー指摘(Codex P2): `remaining_token_budget`が数えるのは**完了した**
    worktreeの実測消費だけである（#438からの既存仕様で、`quota_available`の
    ハードゲートもこれに基づく）。そのため、同じウィンドウ内で
    ディスパッチャーが再実行されると、前サイクルで起動してまだ動いている
    タスクの消費が丸ごと忘れられ、しかも毎回新しい「先頭1件の免除」が
    発行されてしまう。上限500・見積り400なら、サイクル1でA、サイクル2でBを
    起動して見込み800とウィンドウ上限を超え得る。

    そこで、実行中タスクの推定消費を予約分として差し引き、予約がある間は
    免除枠を発行しない。免除枠は「単体で残予算を超える見積りのタスクしか
    無いときにキューが止まる」ことを防ぐためのものであり、既に何かが動いて
    いるならその完了自体が前進を保証するため、ここで免除する必要はない。
    """
    remaining = remaining_token_budget(
        run_state, now, window_seconds, max_tokens_per_window
    )
    if remaining is None:
        return _TokenBudget(remaining=None, exempt_first=True)
    reserved = _reserved_token_estimate(run_state, cost_model)
    return _TokenBudget(
        remaining=max(0, remaining - reserved), exempt_first=reserved == 0
    )


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


def _legacy_components(
    task: Task, all_candidate_tasks: list[Task], run_state: RunState, now: float
) -> ScoreComponents:
    """#660以前のスコアを、そのまま内訳へ分解して返す。

    PR#665レビュー指摘(Codex P2): `legacy`モードで合算値を丸ごと
    `base_priority`へ入れると、cycle JSON／`events.jsonl`の内訳が実態と食い違う
    （例: partial progress付きのmediumが「base 2.0 + progress 1.0」ではなく
    「base 3.0」と報告される）。元の式は
    `base * (1 + time_bonus) + progress`なので、待ち時間bonusの寄与
    `base * time_bonus`をaging成分として取り出せば、合計を変えずに分解できる。
    """
    base_priority = BASE_PRIORITY.get(task.priority, BASE_PRIORITY["medium"])
    waits = [_wait_seconds(t, run_state, now) for t in all_candidate_tasks]
    avg_wait = sum(waits) / len(waits) if waits else 0.0

    time_bonus = 0.0
    if avg_wait > 0:
        wait = _wait_seconds(task, run_state, now)
        time_bonus = max(0.0, (wait / avg_wait) - 1.0) * TIME_BONUS_WEIGHT

    return ScoreComponents(
        base_priority=base_priority,
        aging=base_priority * time_bonus,
        progress=PROGRESS_BONUS if task.progress_partial else 0.0,
    )


def compute_priority_score(
    task: Task, all_candidate_tasks: list[Task], run_state: RunState, now: float
) -> float:
    """#660以前のスコアリング（`legacy`モードおよび互換切り戻し用）。

    内訳（`_legacy_components`）の合計として求めることで、レポートへ出す成分と
    実際に順位付けへ使う値が定義上一致する。
    """
    return _legacy_components(task, all_candidate_tasks, run_state, now).total


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
    candidate_tasks: list[Task],
    run_state: RunState,
    now: float,
    window_seconds: int,
    known_tasks: Iterable[Task] | None,
    cost_model: CostModel,
) -> _ScoringInputs:
    graph_tasks_by_id = {
        task.subtask_id: task
        for task in [*(known_tasks or ()), *candidate_tasks]
        if task.subtask_id
    }
    graph_tasks = pending_tasks(graph_tasks_by_id.values())
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


def _token_penalty_factor(estimate: CostEstimate, max_tokens: float) -> float:
    """推定トークン量のpenalty係数。不明（`None`）なら`0.0`。

    PR#665レビュー指摘(Claude): `tokens or 0`という書き方は「不明＝無料」と
    読めてしまい、「0と推定して無料扱いにしない」という`cost_model`の設計意図と
    矛盾して見える。実際には矛盾しない——`CostModel.estimate`の縮退は
    「そのタスクの履歴 → fleet全体の履歴」であり、fleetに1件でもusage記録が
    あれば全候補が中央値を受け取る。つまり`tokens is None`は候補集合の全員で
    同時にしか成立せず（`max_tokens`も0になり）、penaltyは全員0で打ち消し合う
    ため順位に影響しない。「不明な候補だけが得をする」混在状態は起こらない。

    その前提が将来の変更で崩れないよう、`None`を明示的に分岐したうえで、
    混在が起きないことを`tests/test_dispatch_scheduling.py`で固定する。
    """
    if estimate.tokens is None:
        return 0.0
    return _normalized(float(estimate.tokens), max_tokens)


def _critical_path_decision(
    task: Task, run_state: RunState, now: float, inputs: _ScoringInputs
) -> SchedulingDecision:
    estimate = inputs.estimates.get(task.subtask_id) or inputs.cost_model.estimate(task)
    bottom_level = inputs.ranks.bottom_level_of(task.subtask_id)
    downstream = inputs.ranks.downstream_count(task.subtask_id)
    wait = _wait_seconds(task, run_state, now)
    rank_bonus_enabled = inputs.ranks.exact_bottom_level
    components = ScoreComponents(
        base_priority=BASE_PRIORITY.get(task.priority, BASE_PRIORITY["medium"]),
        aging=AGING_WEIGHT * (wait - inputs.min_wait) / inputs.window_seconds,
        critical_path=(
            CRITICAL_PATH_WEIGHT * _normalized(bottom_level, inputs.max_bottom_level)
            if rank_bonus_enabled
            else 0.0
        ),
        unlock=(
            UNLOCK_WEIGHT * _normalized(downstream, inputs.max_downstream)
            if rank_bonus_enabled
            else 0.0
        ),
        progress=PROGRESS_BONUS if task.progress_partial else 0.0,
        token_penalty=TOKEN_COST_WEIGHT
        * _token_penalty_factor(estimate, inputs.max_tokens),
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
        exact_bottom_level=inputs.ranks.exact_bottom_level,
        exact_downstream=inputs.ranks.exact_downstream,
    )


def _legacy_decision(
    task: Task,
    eligible: list[Task],
    run_state: RunState,
    now: float,
    inputs: _ScoringInputs,
) -> SchedulingDecision:
    components = _legacy_components(task, eligible, run_state, now)
    estimate = inputs.estimates.get(task.subtask_id) or inputs.cost_model.estimate(task)
    return SchedulingDecision(
        issue_number=task.issue_number,
        subtask_id=task.subtask_id,
        mode=SCHEDULING_MODE_LEGACY,
        score=components.total,
        components=components,
        bottom_level=inputs.ranks.bottom_level_of(task.subtask_id),
        unlocked_count=inputs.ranks.unlocked_count(task.subtask_id),
        downstream_count=inputs.ranks.downstream_count(task.subtask_id),
        estimated_tokens=estimate.tokens,
        estimated_duration_seconds=estimate.duration_seconds,
        estimate_source=estimate.source,
        exact_bottom_level=inputs.ranks.exact_bottom_level,
        exact_downstream=inputs.ranks.exact_downstream,
    )


def _normalized_mode(scheduling_mode: str) -> str:
    """設定値をスコアリングモードへ正規化する。

    `legacy`以外は`critical-path`として扱う。設定ファイル・CLIの両方が
    `SCHEDULING_MODES`で値を検証済みのため、ここで未知の値のために追加の
    エラー経路を作らない。
    """
    if scheduling_mode == SCHEDULING_MODE_LEGACY:
        return SCHEDULING_MODE_LEGACY
    return SCHEDULING_MODE_CRITICAL_PATH


def _rank_candidates(
    eligible: list[Task],
    run_state: RunState,
    now: float,
    scheduling_mode: str,
    inputs: _ScoringInputs,
) -> list[tuple[Task, SchedulingDecision]]:
    """候補をスコア降順（同点はissue番号昇順）に並べる。

    `scheduling_mode`が`legacy`以外の値のときはcritical-pathモデルを使う。
    設定ファイル・CLIの両方が`SCHEDULING_MODES`で値を検証済みのため、ここで
    未知の値のために追加のエラー経路を作らない。
    """
    if _normalized_mode(scheduling_mode) == SCHEDULING_MODE_LEGACY:
        decisions = [
            _legacy_decision(t, eligible, run_state, now, inputs) for t in eligible
        ]
    else:
        decisions = [
            _critical_path_decision(t, run_state, now, inputs) for t in eligible
        ]
    return sorted(
        zip(eligible, decisions, strict=True),
        key=lambda pair: (-pair[1].score, pair[1].issue_number),
    )


def _ineligibility_reason(task: Task, active_issue_numbers: set[int]) -> str | None:
    """スコアリング以前に候補から外れる理由。外れないなら`None`。"""
    if task.yaml_error:
        return REASON_YAML_ERROR
    if "status:external-lock" in task.status_labels:
        return REASON_EXTERNAL_LOCK
    if "status:blocked-recompute" in task.status_labels:
        return REASON_BLOCKED_RECOMPUTE
    if task.issue_number in active_issue_numbers:
        return REASON_ALREADY_ACTIVE
    return None


def _partition_candidates(
    candidate_tasks: list[Task], run_state: RunState
) -> tuple[list[Task], list[tuple[Task, str]]]:
    """候補を「スコアリング対象」と「対象外＋その理由」に分ける。

    PR#665レビュー指摘(Codex P2): 対象外の候補を単に捨てると、`yaml_error`の
    タスクのようにapply時には実際に処理される（`_launch_selected_tasks`が
    `status:blocked-*`へ落とす）ものまでレポートから消え、「全候補の選定理由が
    観測できる」という本機能の主張が崩れる。運用者が「なぜ起動しなかったのか」を
    追えるよう、対象外も理由付きの未選出判定として残す。
    """
    active_issue_numbers = {int(k) for k in run_state.active_worktrees}
    eligible: list[Task] = []
    excluded: list[tuple[Task, str]] = []
    for task in candidate_tasks:
        reason = _ineligibility_reason(task, active_issue_numbers)
        if reason is None:
            eligible.append(task)
            continue
        excluded.append((task, reason))
    return eligible, sorted(excluded, key=lambda item: item[0].issue_number)


def _excluded_decision(
    task: Task, reason: str, scheduling_mode: str, inputs: _ScoringInputs
) -> SchedulingDecision:
    """スコア対象外でも、診断に使うrankとcostは実値を保持する。"""
    estimate = inputs.estimates.get(task.subtask_id) or inputs.cost_model.estimate(task)
    return SchedulingDecision(
        issue_number=task.issue_number,
        subtask_id=task.subtask_id,
        mode=_normalized_mode(scheduling_mode),
        score=0.0,
        components=ScoreComponents(),
        bottom_level=inputs.ranks.bottom_level_of(task.subtask_id),
        unlocked_count=inputs.ranks.unlocked_count(task.subtask_id),
        downstream_count=inputs.ranks.downstream_count(task.subtask_id),
        estimated_tokens=estimate.tokens,
        estimated_duration_seconds=estimate.duration_seconds,
        estimate_source=estimate.source,
        exact_bottom_level=inputs.ranks.exact_bottom_level,
        exact_downstream=inputs.ranks.exact_downstream,
        reason=reason,
    )


def _apply_resource_constraints(
    ranked: list[tuple[Task, SchedulingDecision]],
    slots: int,
    token_budget: _TokenBudget,
    conflict_graph: ConflictGraph | None,
    active_subtask_ids: set[str] | None,
    slot_exhausted_reason: str = REASON_QUOTA_EXHAUSTED,
) -> SchedulingResult:
    """クオータ・競合・トークン予算の順に制約を当てて貪欲に選ぶ。"""
    unavailable = set(active_subtask_ids or ())
    selected: list[Task] = []
    decisions: list[SchedulingDecision] = []
    projected_tokens = 0

    for task, decision in ranked:
        if len(selected) >= slots:
            decisions.append(replace(decision, reason=slot_exhausted_reason))
            continue
        if conflict_graph is not None and any(
            conflict_graph.has_conflict(task.subtask_id, other) for other in unavailable
        ):
            decisions.append(replace(decision, reason=REASON_CONFLICT))
            continue
        # legacyは#660以前と同じ選出を維持し、バッチ内の推定token gateを使わない。
        cost = (
            None
            if decision.mode == SCHEDULING_MODE_LEGACY
            else decision.estimated_tokens
        )
        # 先頭1件はトークン予算で弾かない。単体で残予算を超える見積りのタスク
        # しか無いときにキューが永久に進まなくなる（終端の無い経路になる）ため。
        # ただし実行中の起動が既にある場合（`exempt_first`が偽）はその完了が
        # 前進を保証するので免除しない——サイクルごとに免除枠を発行すると、
        # ウィンドウ内の見込み消費が上限を超え得る（PR#665レビュー指摘）。
        # ウィンドウ上限そのものは`quota_available`のハードゲートが守る。
        if (
            token_budget.remaining is not None
            and (selected or not token_budget.exempt_first)
            and cost is not None
            and projected_tokens + cost > token_budget.remaining
        ):
            decisions.append(replace(decision, reason=REASON_TOKEN_BUDGET))
            continue
        selected.append(task)
        unavailable.add(task.subtask_id)
        projected_tokens += cost or 0
        decisions.append(replace(decision, selected=True, reason=REASON_SELECTED))

    return SchedulingResult(selected=selected, decisions=decisions)


def _prepare_ranked_candidates(
    eligible: list[Task],
    candidate_tasks: list[Task],
    run_state: RunState,
    now: float,
    window_seconds: int,
    scheduling_mode: str,
    known_tasks: Iterable[Task] | None,
) -> tuple[list[tuple[Task, SchedulingDecision]], _ScoringInputs, CostModel]:
    cost_model = build_cost_model(run_state)
    inputs = _build_scoring_inputs(
        eligible,
        candidate_tasks,
        run_state,
        now,
        window_seconds,
        known_tasks,
        cost_model,
    )
    ranked = _rank_candidates(eligible, run_state, now, scheduling_mode, inputs)
    return ranked, inputs, cost_model


def _slot_exhausted_reason(
    run_state: RunState,
    now: float,
    window_seconds: int,
    max_tokens_per_window: int | None,
) -> str:
    hard_token_budget = remaining_token_budget(
        run_state, now, window_seconds, max_tokens_per_window
    )
    if hard_token_budget is not None and hard_token_budget <= 0:
        return REASON_TOKEN_BUDGET
    return REASON_QUOTA_EXHAUSTED


def _append_excluded_decisions(
    result: SchedulingResult,
    excluded: list[tuple[Task, str]],
    scheduling_mode: str,
    inputs: _ScoringInputs,
) -> SchedulingResult:
    decisions = result.decisions + [
        _excluded_decision(task, reason, scheduling_mode, inputs)
        for task, reason in excluded
    ]
    return SchedulingResult(selected=result.selected, decisions=decisions)


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
    eligible, excluded = _partition_candidates(candidate_tasks, run_state)
    slots = quota_available(
        run_state,
        now,
        max_concurrent,
        max_launches_per_window,
        window_seconds,
        max_tokens_per_window=max_tokens_per_window,
    )
    ranked, inputs, cost_model = _prepare_ranked_candidates(
        eligible,
        candidate_tasks,
        run_state,
        now,
        window_seconds,
        scheduling_mode,
        known_tasks,
    )
    result = _apply_resource_constraints(
        ranked,
        slots,
        _token_budget(
            run_state, now, window_seconds, max_tokens_per_window, cost_model
        ),
        conflict_graph,
        active_subtask_ids,
        slot_exhausted_reason=_slot_exhausted_reason(
            run_state, now, window_seconds, max_tokens_per_window
        ),
    )
    # スコアリング対象外の候補は、ランク付き判定の後ろにissue番号順で並べる。
    return _append_excluded_decisions(result, excluded, scheduling_mode, inputs)


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
