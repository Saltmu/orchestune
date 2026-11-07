"""active worktreeのRuleと、それを評価する`CycleContext`境界。

Ruleは優先順位付きの`RuleChain`で合成する。`CycleContext`は意味付きqueryと
action portを提供し、個々の判定・副作用は対応する責務モジュールが実装する。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from orchestune.consistency.models import RepairCommand, RepairResult
from orchestune.dag.models import SubTask
from orchestune.dependencies.assessment import DependencyAssessment
from orchestune.dispatch.config.model import DispatcherConfig
from orchestune.dispatch.cycle.action_contracts import (
    ActivePhaseResult,
    CycleActions,
    CycleQueries,
    GcPhaseResult,
    StackBase,
)
from orchestune.dispatch.cycle.context_state import (
    CycleObservations,
    RecordResult,
    _CycleState,
    _resolve_cycle_observations,
)
from orchestune.dispatch.cycle.events import (
    CompletionEvent,
    DeviationEvent,
    PromotionEvent,
)
from orchestune.dispatch.scoring import SchedulingResult
from orchestune.ledger.completion_reservations import (
    completion_handoff_matches_active,
    completion_mutation_blocked_fresh,
    dependency_completion_blocked,
    dependency_completion_blocked_fresh,
)
from orchestune.ledger.run_state import (
    ActiveWorktree,
    RunState,
    load_run_state_readonly,
)
from orchestune.models import IssueRecord, PrRecord
from orchestune.models import Task as Task
from orchestune.task_metadata import TaskMetadata

if TYPE_CHECKING:
    from orchestune.lock_contracts import ExternalLockScanResult

NotNeededReviewDispatcher = Callable[[int, str, DispatcherConfig], None]


class CycleContext(_CycleState):
    """One cycle's semantic query/record/action boundary.

    The constructor still accepts the observation containers produced by
    ``cycle_context.py`` so the ownership boundary stays explicit, but none of
    them is retained as a public attribute.  Phases can observe state only via
    semantic queries and perform effects only through delegated action ports.
    """

    def __init__(
        self,
        run_state: RunState,
        observations: CycleObservations | None = None,
        *,
        config: DispatcherConfig | None = None,
        not_needed_review_dispatcher: NotNeededReviewDispatcher | None = None,
        actions: CycleActions | None = None,
        **kwargs: Any,
    ) -> None:
        resolved = _resolve_cycle_observations(
            observations,
            kwargs,
            "CycleContext.__init__",
            default_active_worktrees=run_state.active_worktrees,
        )
        super().__init__(resolved, completion_blocked=self.is_completion_blocked)
        self.config: DispatcherConfig = cast(DispatcherConfig, config)
        self._completion_reservation_state = run_state
        self.not_needed_review_dispatcher = not_needed_review_dispatcher
        self._actions = actions

    # ---- action API ---------------------------------------------------------

    def is_completion_blocked(self, issue_number: int) -> bool:
        observed = self._labels(issue_number)
        if self.config is None:
            return dependency_completion_blocked(
                self._completion_reservation_state, issue_number, observed
            )
        return dependency_completion_blocked_fresh(
            self._completion_reservation_state,
            issue_number,
            self.config.run_state_path,
            observed,
        )

    def _action_port(self) -> CycleActions:
        if self._actions is None:
            raise ValueError("CycleContext has no bound action adapter")
        return self._actions

    def process_active_worktrees(self) -> ActivePhaseResult:
        return self._action_port().process_active_worktrees()

    def run_gc(self, events: tuple[CompletionEvent, ...]) -> GcPhaseResult:
        return self._action_port().run_gc(events)

    def reconcile_recovery(self) -> tuple[PromotionEvent, ...]:
        return self._action_port().reconcile_recovery()

    def scan_external_locks(self) -> ExternalLockScanResult:
        return self._action_port().scan_external_locks()

    def select_tasks(self, candidates: tuple[TaskMetadata, ...]) -> SchedulingResult:
        return self._action_port().select_tasks(candidates)

    def launch_tasks(
        self,
        selected: tuple[TaskMetadata, ...],
        bases: tuple[StackBase, ...],
        candidates: tuple[TaskMetadata, ...],
    ) -> tuple[TaskMetadata, ...]:
        return self._action_port().launch_tasks(selected, bases, candidates)

    def execute_repair(self, command: RepairCommand) -> RepairResult:
        return self._action_port().execute_repair(command)


@dataclass(frozen=True, slots=True)
class _RuleExecutionContext:
    """active-worktree RuleとGC actの非公開入力adapter。

    Ruleの判定は`queries`だけを通じて状態を読み、actは`run_state`を更新する。
    追加fieldは既存Ruleの局所的な入力であり、公開query APIではない。

    Rule実装がL2にあり、`gc`は`escalation`をimportするため、このadapterも
    `rules.py`に置く。`gc`、`escalation`、`rebase`のいずれかへ置くと、Ruleが
    importするadapterを通じてL2内の循環依存を作る。
    """

    run_state: RunState
    queries: CycleQueries
    config: DispatcherConfig
    prs: tuple[PrRecord, ...] = ()
    not_needed_review_dispatcher: NotNeededReviewDispatcher | None = None
    issue_records_by_number: dict[int, IssueRecord] = field(default_factory=dict)
    dag_inputs: tuple[SubTask, ...] = ()

    def completion_reserved(self, issue_number: int) -> bool:
        return completion_mutation_blocked_fresh(
            RunState(), issue_number, self.config.run_state_path
        )

    def handoff_matches(self, active: ActiveWorktree) -> bool:
        return completion_handoff_matches_active(
            load_run_state_readonly(self.config.run_state_path), active
        )

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

    completion_event: CompletionEvent | None = None
    deviation_event: DeviationEvent | None = None
    forced_serial: bool = False
    terminal: bool = True


Rule = Callable[
    ["_RuleExecutionContext", str, ActiveWorktree, "TaskMetadata | None"],
    "ActiveWorktreeRuleOutcome | None",
]


@dataclass
class _ActiveWorktreeAggregates:
    completion_events: list[CompletionEvent] = field(default_factory=list)
    deviation_events: list[DeviationEvent] = field(default_factory=list)
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

    新しい判断パターンは対応する責務モジュールにRuleとして実装し、該当する
    `RuleChain`へ追加する。
    """

    rules: list[Rule]

    def run(
        self,
        ctx: _RuleExecutionContext,
        key: str,
        active: ActiveWorktree,
        active_task: TaskMetadata | None,
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


__all__ = [
    "ActiveWorktreeRuleOutcome",
    "CycleContext",
    "CycleObservations",
    "NotNeededReviewDispatcher",
    "Rule",
    "RuleChain",
    "_ActiveWorktreeAggregates",
    "_RuleExecutionContext",
]
