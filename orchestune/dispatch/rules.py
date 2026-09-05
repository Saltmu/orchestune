"""active worktreeごとの判定(Rule)と、優先順位付きで合成するComposite(RuleChain)。

cycle側(dispatch_cycle.py)は、ここで定義される`CycleContext`で条件データを渡し、
`RuleChain`にどのRuleをどの優先順位で並べるかだけを決める。個々のRuleの中身
(条件判定そのもの)は、対応するact側モジュール(dispatch_gc/dispatch_escalation/
dispatch_rebase)に定義される。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.dependency_resolution import TaskDependencies
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.models import PrRecord

NotNeededReviewDispatcher = Callable[[int, str, DispatcherConfig], None]


@dataclass
class CycleContext:
    """1サイクル分の読み取り専用データをまとめたコンテキスト。

    decide/act関数の引数を位置引数の羅列にせず、新しい判断パターンが追加の
    データを必要とする場合の引数伝播を、このコンテキストへの1フィールド追加に
    閉じ込めることを目的とする（#86）。

    #799: 依存解決に関わるフィールド（`dependency_resolution`
    `done_issue_numbers` `ci_passed_pr_issue_numbers`
    `changes_requested_issue_numbers` `branch_by_issue_number`）は、
    すべてIssue番号をキー・値の同一性とする。`subtask_id`は1つの分解計画
    （EPIC）内でしか一意性が保証されないため、`--parent-issue`を指定しない
    複数EPIC横断のサイクルでは同名subtask_idが衝突しうる。
    `issue_number_by_subtask_id`のみ、footprint逸脱によるConflict Graph
    再計算通知（`dispatch.rebase.notify_recompute`等）が使う別関心事の
    表示用マップとして維持する（依存解決には使わない）。
    """

    run_state: RunState
    tasks_by_issue: dict[int, Task]
    issue_number_by_subtask_id: dict[str, int]
    dependency_resolution: dict[int, TaskDependencies]
    done_issue_numbers: set[int]
    ci_passed_pr_issue_numbers: set[int]
    changes_requested_issue_numbers: set[int]
    branch_by_issue_number: dict[int, str]
    prs: list[PrRecord]
    pr_by_branch: dict[str, PrRecord]
    config: DispatcherConfig
    not_needed_review_dispatcher: NotNeededReviewDispatcher | None = None


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
    # #799: `completed_subtask_ids`は表示・後方互換用に維持しつつ、依存解決
    # （「このサイクル内で完了したタスクを、他タスクの依存先としてどう扱うか」）
    # にはこちらのIssue番号集合を使う。`active.issue_number`は個々のRuleが
    # 常に1つの確定したActiveWorktreeに対して動作した結果すでに分かっている値
    # なので、`ActiveWorktreeRuleOutcome`自体にIssue番号を持たせ直さなくても
    # ここで衝突なく集約できる。
    completed_issue_numbers: set[int] = field(default_factory=set)


def _merge_active_worktree_outcome(
    aggregates: _ActiveWorktreeAggregates,
    outcome: ActiveWorktreeRuleOutcome,
    issue_number: int,
) -> None:
    if outcome.completion_event is not None:
        aggregates.completion_events.append(outcome.completion_event)
    if outcome.deviation_event is not None:
        aggregates.deviation_events.append(outcome.deviation_event)
    if outcome.completed_subtask_id is not None:
        aggregates.completed_subtask_ids.add(outcome.completed_subtask_id)
        aggregates.completed_issue_numbers.add(issue_number)
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
            _merge_active_worktree_outcome(aggregates, outcome, active.issue_number)
            if outcome.terminal:
                return True
        return False
