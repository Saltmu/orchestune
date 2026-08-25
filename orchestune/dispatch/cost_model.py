"""完了履歴からタスクの所要時間・トークン量・手戻りリスクを推定する（#660）。

スケジューラが「クオータ1単位あたりの完成した成果物」を最大化するには、価値
（critical path上の位置・後続解放数）だけでなくコストも必要になる。ここでは
`RunState.completed_worktrees`——ディスパッチャーが既にKPI集計用に保持している
完了履歴——だけを根拠に、外部サービスを呼ばずに決定論的な推定を行う。

推定の優先順位は3段階で、履歴が薄いほど安全側の既定値へ縮退する。

1. **そのタスク自身の履歴**（同一Issue番号の完了記録）の中央値
2. **全体の履歴**（fleet全体）の中央値
3. **決定論的な既定値**（所要時間は`DEFAULT_DURATION_SECONDS`、トークンは`None`）

中央値を使うのは、1件の異常に長い試行（タイムアウト・ゾンビ回収）が平均を
引きずってしまうため。トークンだけは「不明」を`None`のまま残す: 0と推定すると
無料のタスクとして扱われ、逆に既定値を捏造するとトークン上限の判定が根拠の無い
数値で動いてしまう。呼び出し側は`None`を「予算判定の対象外」として扱う。
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median

from orchestune.dispatch.state import CompletedWorktree, RunState
from orchestune.models import Task

# 履歴が全く無いときの推定所要時間（30分）。bottom levelは相対比較にしか使わない
# ため、全タスクが同じ既定値になる状況では順位に影響しない。
DEFAULT_DURATION_SECONDS = 1800.0

ESTIMATE_SOURCE_TASK = "task-history"
ESTIMATE_SOURCE_FLEET = "fleet-history"
ESTIMATE_SOURCE_DEFAULT = "default"


@dataclass(frozen=True)
class CostEstimate:
    """1タスク分の推定コスト。`tokens is None`は「不明」を意味する。"""

    duration_seconds: float
    tokens: int | None
    rework_risk: float
    source: str


@dataclass(frozen=True)
class CostModel:
    """完了履歴から組み立てた推定器。同じ履歴からは常に同じ推定を返す。"""

    durations: dict[int, float] = field(default_factory=dict)
    tokens: dict[int, int] = field(default_factory=dict)
    attempts: dict[int, int] = field(default_factory=dict)
    fleet_duration: float | None = None
    fleet_tokens: int | None = None

    def estimate(self, task: Task) -> CostEstimate:
        duration = self.durations.get(task.issue_number)
        tokens = self.tokens.get(task.issue_number)
        return CostEstimate(
            duration_seconds=self._resolve_duration(duration),
            tokens=self.tokens_for_issue(task.issue_number),
            rework_risk=self._rework_risk(task.issue_number),
            source=self._source(duration, tokens),
        )

    def tokens_for_issue(self, issue_number: int) -> int | None:
        """Issue番号だけで引ける推定トークン量。不明なら`None`。

        実行中（`ActiveWorktree`）のタスクは`Task`が手元に無いこともあるため、
        `estimate`と同じ縮退（そのタスクの履歴 → fleet全体の中央値）をIssue番号
        だけで引けるようにしておく（#660 / PR#665レビュー指摘）。
        """
        tokens = self.tokens.get(issue_number)
        return tokens if tokens is not None else self.fleet_tokens

    def _resolve_duration(self, duration: float | None) -> float:
        if duration is not None:
            return duration
        if self.fleet_duration is not None:
            return self.fleet_duration
        return DEFAULT_DURATION_SECONDS

    def _source(self, duration: float | None, tokens: int | None) -> str:
        if duration is not None or tokens is not None:
            return ESTIMATE_SOURCE_TASK
        if self.fleet_duration is not None or self.fleet_tokens is not None:
            return ESTIMATE_SOURCE_FLEET
        return ESTIMATE_SOURCE_DEFAULT

    def _rework_risk(self, issue_number: int) -> float:
        """まだキューに居るタスクの過去試行回数を[0, 1)の手戻りリスクへ写す。

        既に`n`回試行されたのに再びキューへ戻っているタスクは、`n`回の手戻りを
        実測している。`n / (n + 1)`は単調増加で1に達しないため、履歴が積もっても
        「絶対に選ばれないタスク」を作らない（＝終端の無い経路を作らない）。
        """
        attempts = self.attempts.get(issue_number, 0)
        return attempts / (attempts + 1)


def _sample_duration(completed: CompletedWorktree) -> float | None:
    if completed.started_at is None:
        return None
    duration = completed.completed_at - completed.started_at
    if not math.isfinite(duration) or duration <= 0:
        return None
    return duration


def _sample_tokens(completed: CompletedWorktree) -> int | None:
    usage = completed.usage
    if usage is None or usage.total_tokens is None or usage.total_tokens < 0:
        return None
    return int(usage.total_tokens)


def build_cost_model(run_state: RunState) -> CostModel:
    """完了履歴からコスト推定器を組み立てる。"""
    duration_samples: dict[int, list[float]] = defaultdict(list)
    token_samples: dict[int, list[int]] = defaultdict(list)
    attempts: dict[int, int] = defaultdict(int)

    for completed in run_state.completed_worktrees:
        attempts[completed.issue_number] += 1
        duration = _sample_duration(completed)
        if duration is not None:
            duration_samples[completed.issue_number].append(duration)
        tokens = _sample_tokens(completed)
        if tokens is not None:
            token_samples[completed.issue_number].append(tokens)

    all_durations = [value for values in duration_samples.values() for value in values]
    all_tokens = [value for values in token_samples.values() for value in values]
    return CostModel(
        durations={
            issue: float(median(values)) for issue, values in duration_samples.items()
        },
        tokens={issue: int(median(values)) for issue, values in token_samples.items()},
        attempts=dict(attempts),
        fleet_duration=float(median(all_durations)) if all_durations else None,
        fleet_tokens=int(median(all_tokens)) if all_tokens else None,
    )
