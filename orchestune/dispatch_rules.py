"""active worktreeごとの判定(Rule)と、優先順位付きで合成するComposite(RuleChain)。

cycle側(dispatch_cycle.py)は、ここで定義される`CycleContext`で条件データを渡し、
`RuleChain`にどのRuleをどの優先順位で並べるかだけを決める。個々のRuleの中身
(条件判定そのもの)は、対応するact側モジュール(dispatch_gc/dispatch_escalation/
dispatch_rebase)に定義される。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree, RunState
from orchestune.github import PrRecord


@dataclass
class CycleContext:
    """1サイクル分の読み取り専用データをまとめたコンテキスト。

    decide/act関数の引数を位置引数の羅列にせず、新しい判断パターンが追加の
    データを必要とする場合の引数伝播を、このコンテキストへの1フィールド追加に
    閉じ込めることを目的とする（#86）。
    """

    run_state: RunState
    tasks_by_issue: dict[int, Task]
    issue_number_by_subtask_id: dict[str, int]
    done_subtask_ids: set[str]
    ci_passed_pr_subtask_ids: set[str]
    changes_requested_subtask_ids: set[str]
    subtask_branch_map: dict[str, str]
    prs: list[PrRecord]
    pr_by_branch: dict[str, PrRecord]
    config: DispatcherConfig


@dataclass
class ActiveWorktreeRuleOutcome:
    """1つの判定ルールがactive worktreeに対して下した結果。

    `terminal=True`の場合、このactive worktreeに対する以降のルール評価を
    打ち切り次のactive worktreeへ進む。`terminal=False`の場合は次のルールを
    引き続き試す（例: dirty worktreeのため完了判定を見送った場合でも、
    CHANGES_REQUESTEDや自動リベースのチェックは継続する必要がある）。
    """

    completion_event: dict | None = None
    deviation_event: dict | None = None
    completed_subtask_id: str | None = None
    forced_serial: bool = False
    terminal: bool = True


Rule = Callable[
    [CycleContext, str, ActiveWorktree, "Task | None"],
    "ActiveWorktreeRuleOutcome | None",
]


@dataclass
class _ActiveWorktreeAggregates:
    completion_events: list[dict] = field(default_factory=list)
    deviation_events: list[dict] = field(default_factory=list)
    any_forced_serial: bool = False
    completed_subtask_ids: set[str] = field(default_factory=set)


def _merge_active_worktree_outcome(
    aggregates: _ActiveWorktreeAggregates, outcome: ActiveWorktreeRuleOutcome
) -> None:
    if outcome.completion_event is not None:
        aggregates.completion_events.append(outcome.completion_event)
    if outcome.deviation_event is not None:
        aggregates.deviation_events.append(outcome.deviation_event)
    if outcome.completed_subtask_id is not None:
        aggregates.completed_subtask_ids.add(outcome.completed_subtask_id)
    if outcome.forced_serial:
        aggregates.any_forced_serial = True


@dataclass
class RuleChain:
    """優先順位付きのRule群を1つのComponentとしてカプセル化するComposite。

    先頭から順にruleを評価し、`terminal=True`の結果を得たら直ちに打ち切って
    Trueを返す。`terminal=False`の場合は結果をaggregatesへ反映した上で次の
    ruleを試し続ける。どのruleも該当しなければFalseを返す。

    新しい判断パターンを追加する場合、このクラス自体は変更せず、対応する
    ruleを対応するact側モジュールに書いて、該当する`RuleChain`の`rules`に
    追加するだけでよい（#86）。
    """

    rules: list[Rule]

    def run(
        self,
        ctx: CycleContext,
        key: str,
        active: ActiveWorktree,
        active_task: Task | None,
        aggregates: _ActiveWorktreeAggregates,
    ) -> bool:
        for rule in self.rules:
            outcome = rule(ctx, key, active, active_task)
            if outcome is None:
                continue
            _merge_active_worktree_outcome(aggregates, outcome)
            if outcome.terminal:
                return True
        return False
