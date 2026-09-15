"""active worktreeごとの判定(Rule)と、優先順位付きで合成するComposite(RuleChain)。

cycle側(dispatch_cycle.py)は、ここで定義される`CycleContext`で条件データを渡し、
`RuleChain`にどのRuleをどの優先順位で並べるかだけを決める。個々のRuleの中身
(条件判定そのもの)は、対応するact側モジュール(dispatch_gc/dispatch_escalation/
dispatch_rebase)に定義される。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from orchestune.consistency.models import RepairCommand, RepairResult
from orchestune.dag.models import SubTask
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_action_contracts import (
    ActivePhaseResult,
    CycleActions,
    CycleQueries,
    GcPhaseResult,
    StackBase,
)
from orchestune.dispatch.cycle_context_state import (
    LaunchFact,
    RecordResult,
    _CycleState,
)
from orchestune.dispatch.dependency_assessment import DependencyAssessment
from orchestune.dispatch.dependency_resolution import TaskDependencies
from orchestune.dispatch.scoring import SchedulingResult, Task
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.models import IssueRecord, PrRecord

NotNeededReviewDispatcher = Callable[[int, str, DispatcherConfig], None]


class CycleContext:
    """One cycle's semantic query/record/action boundary.

    The constructor still accepts the observation containers produced by
    ``cycle_context.py`` so the ownership boundary stays explicit, but none of
    them is retained as a public attribute.  Phases can observe state only via
    semantic queries and perform effects only through the seven delegated
    action ports.
    """

    def __init__(
        self,
        run_state: RunState,
        tasks_by_issue: dict[int, Task],
        issue_number_by_subtask_id: dict[str, int],
        dependency_resolution: dict[int, TaskDependencies],
        done_issue_numbers: set[int],
        ci_passed_pr_issue_numbers: set[int],
        changes_requested_issue_numbers: set[int],
        branch_by_issue_number: dict[int, str],
        prs: list[PrRecord],
        pr_by_branch: dict[str, PrRecord],
        config: DispatcherConfig,
        not_needed_review_dispatcher: NotNeededReviewDispatcher | None = None,
        issue_records_by_number: dict[int, IssueRecord] | None = None,
        prior_parent_merge_hold_issue_numbers: frozenset[int] = frozenset(),
        prior_parent_merge_completed_issue_numbers: frozenset[int] = frozenset(),
        actions: CycleActions | None = None,
    ) -> None:
        # Compatibility constructor inputs that are now derivable or owned by
        # the state/action boundaries are intentionally not retained.
        del issue_number_by_subtask_id, done_issue_numbers, pr_by_branch
        self.config = config
        self.not_needed_review_dispatcher = not_needed_review_dispatcher
        self._actions = actions
        self._state = _CycleState.from_observations(
            tasks_by_issue=tasks_by_issue,
            dependency_resolution=dependency_resolution,
            ci_passed_pr_issue_numbers=ci_passed_pr_issue_numbers,
            changes_requested_issue_numbers=changes_requested_issue_numbers,
            branch_by_issue_number=branch_by_issue_number,
            active_worktrees=run_state.active_worktrees,
            prior_parent_merge_completed_issue_numbers=prior_parent_merge_completed_issue_numbers,
            prior_parent_merge_hold_issue_numbers=prior_parent_merge_hold_issue_numbers,
            issue_records_by_number=issue_records_by_number or {},
            prs=prs,
        )

    # ---- semantic query API (#868) ------------------------------------------
    #
    # 実効状態を返す。取得済みの戻り値(Task/tuple/Assessment)は後からContextが
    # 更新されても変化しない——再問い合わせで最新状態を反映する。

    def task(self, issue_number: int) -> Task | None:
        return self._state.task(issue_number)

    def dependencies_of(self, issue_number: int) -> TaskDependencies | None:
        return self._state.dependencies_of(issue_number)

    def assess_dependencies(self, issue_number: int) -> DependencyAssessment | None:
        return self._state.assess_dependencies(issue_number)

    def is_effectively_done(self, issue_number: int) -> bool:
        return self._state.is_effectively_done(issue_number)

    def is_completion_confirmed(self, issue_number: int) -> bool:
        return self._state.is_completion_confirmed(issue_number)

    def has_changes_requested(self, issue_number: int) -> bool:
        return self._state.has_changes_requested(issue_number)

    def is_ci_passed(self, issue_number: int) -> bool:
        return self._state.is_ci_passed(issue_number)

    def canonical_branch(self, issue_number: int) -> str | None:
        return self._state.canonical_branch(issue_number)

    def launch_fact(self, issue_number: int) -> LaunchFact | None:
        return self._state.launch_fact(issue_number)

    def queued_tasks(self) -> tuple[Task, ...]:
        return self._state.queued_tasks()

    def blocked_tasks(self) -> tuple[Task, ...]:
        return self._state.blocked_tasks()

    # ---- all-task / observation queries (#881) ------------------------------
    #
    # `tasks`はrecord反映後の実効値、`issue_records`/`pull_requests`は初期Forge
    # 観測。いずれもIssue/PR番号昇順で、取得済みのtupleは後から変化しない。

    def tasks(self) -> tuple[Task, ...]:
        return self._state.tasks()

    def issue_records(self) -> tuple[IssueRecord, ...]:
        return self._state.issue_records()

    def pull_requests(self) -> tuple[PrRecord, ...]:
        return self._state.pull_requests()

    def is_prior_merge_held(self, issue_number: int) -> bool:
        return self._state.is_prior_merge_held(issue_number)

    def dag_inputs(self, issue_numbers: tuple[int, ...]) -> tuple[SubTask, ...]:
        return self._state.dag_inputs(issue_numbers)

    # ---- record API (#868) --------------------------------------------------
    #
    # 外部I/Oを行わない。呼出側が既に成功を確認した事実だけを反映する。

    def record_completion(self, issue_number: int) -> RecordResult:
        return self._state.record_completion(issue_number)

    def record_launch(self, active: ActiveWorktree) -> RecordResult:
        return self._state.record_launch(active)

    def record_transition(
        self,
        issue_number: int,
        *,
        expected_labels: tuple[str, ...],
        verified_labels: tuple[str, ...],
        execution_active: bool,
    ) -> RecordResult:
        return self._state.record_transition(
            issue_number,
            expected_labels=expected_labels,
            verified_labels=verified_labels,
            execution_active=execution_active,
        )

    # ---- action API (#873) -------------------------------------------------

    def _action_port(self) -> CycleActions:
        if self._actions is None:
            raise ValueError("CycleContext has no bound action adapter")
        return self._actions

    def process_active_worktrees(self) -> ActivePhaseResult:
        return self._action_port().process_active_worktrees()

    def run_gc(self, events: tuple[dict[str, object], ...]) -> GcPhaseResult:
        return self._action_port().run_gc(events)

    def reconcile_recovery(self) -> tuple[dict[str, object], ...]:
        return self._action_port().reconcile_recovery()

    def scan_external_locks(self):
        return self._action_port().scan_external_locks()

    def select_tasks(self, candidates: tuple[Task, ...]) -> SchedulingResult:
        return self._action_port().select_tasks(candidates)

    def launch_tasks(
        self,
        selected: tuple[Task, ...],
        bases: tuple[StackBase, ...],
        candidates: tuple[Task, ...],
    ) -> tuple[Task, ...]:
        return self._action_port().launch_tasks(selected, bases, candidates)

    def execute_repair(self, command: RepairCommand) -> RepairResult:
        return self._action_port().execute_repair(command)


@dataclass(frozen=True, slots=True)
class _RuleExecutionContext:
    """#884: private input adapter for the active-worktree Rules and GC acts.

    Not a public state window (`CycleActionAdapter` never returns it, and
    neither does anything else) -- it exists only to hand Rule
    decisions/acts exactly the three things they need: `run_state` (acts
    mutate this directly), `queries` (a `CycleQueries`-conforming view;
    decisions read only through this), and `config`. `prs`,
    `not_needed_review_dispatcher`, `issue_records_by_number`,
    `tasks_by_issue`, and `issue_number_by_subtask_id` are narrowly-scoped
    extras existing Rule bodies already read directly that `CycleQueries` has
    no method for -- `not_needed_review_dispatcher` in particular is L3
    behavior injected into L2 Rule code specifically to avoid an L2->L3
    import, the same reason `CycleContext` already carries it as a plain
    field rather than a method. `issue_number_by_subtask_id` is never used
    for dependency resolution itself (per `CycleContext`'s own docstring),
    but it is not display-only either (#884 Codex review): footprint-deviation
    handling (`rebase.notify_recompute`) uses it to find and actually
    transition the blocked issue to `status:blocked`/`status:blocked-recompute`,
    not just to word a notification comment. `CycleQueries` has no equivalent
    query, so `CycleActionAdapter` reconstructs it the same way
    `cycle_context.py` builds it for `CycleContext` (from `view.tasks()`).

    Lives in `rules.py` rather than the new L3 `cycle_actions.py` because the
    Rule functions that take it as `ctx` (`gc/__init__.py`, `rebase.py`,
    `escalation.py`) are all L2 and cannot import from L3; `rules.py` is the
    one L2 module all three already import `CycleContext` from without
    creating an import cycle (`gc/__init__.py` already imports
    `escalation.py`, so defining this in either of those, or in `rebase.py`,
    would cycle back).
    """

    run_state: RunState
    queries: CycleQueries
    config: DispatcherConfig
    prs: tuple[PrRecord, ...] = ()
    not_needed_review_dispatcher: NotNeededReviewDispatcher | None = None
    issue_records_by_number: dict[int, IssueRecord] = field(default_factory=dict)
    tasks_by_issue: dict[int, Task] = field(default_factory=dict)
    issue_number_by_subtask_id: dict[str, int] = field(default_factory=dict)

    def record_completion(self, issue_number: int) -> RecordResult:
        return self.queries.record_completion(issue_number)

    def assess_dependencies(self, issue_number: int) -> DependencyAssessment | None:
        return self.queries.assess_dependencies(issue_number)


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
    forced_serial: bool = False
    terminal: bool = True


Rule = Callable[
    ["_RuleExecutionContext", str, ActiveWorktree, "Task | None"],
    "ActiveWorktreeRuleOutcome | None",
]


@dataclass
class _ActiveWorktreeAggregates:
    completion_events: list[dict] = field(default_factory=list)
    deviation_events: list[dict] = field(default_factory=list)
    any_forced_serial: bool = False


def _merge_active_worktree_outcome(
    aggregates: _ActiveWorktreeAggregates,
    outcome: ActiveWorktreeRuleOutcome,
) -> None:
    if outcome.completion_event is not None:
        aggregates.completion_events.append(outcome.completion_event)
    if outcome.deviation_event is not None:
        aggregates.deviation_events.append(outcome.deviation_event)
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
        ctx: _RuleExecutionContext,
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
