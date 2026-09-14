"""L3 implementation of the `CycleActions` port (#823 v3 / #884).

`CycleActionAdapter` implements only `process_active_worktrees` and `run_gc`
from `cycle_action_contracts.CycleActions` at this stage; every other port
method is unused until #885/#886 add it. Wiring the adapter into the live
`cycle.py` pipeline is #873's job -- this module only adds the adapter and
relocates its implementation; `cycle.py`'s existing call sites keep calling
the pre-existing functions (`phase_reconciliation._process_active_worktrees`,
`phase_gc.run_gc_phase`) unchanged, now as thin wrappers over the same shared
logic this module owns.

The adapter owns exactly one `RunState`, loaded once at construction
(`RunStateは起動時にloadした1個だけをadapterが所有する`); it exposes no
getter that returns it to callers. `bind_context` may succeed only once --
a second call, or using either port method before any `bind_context` call,
raises `ValueError`.
"""

from __future__ import annotations

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.conflicts import build_task_conflict_graph
from orchestune.dispatch.cycle_action_contracts import (
    ActivePhaseResult,
    CycleQueries,
    GcPhaseResult,
    StackBase,
)
from orchestune.dispatch.cycle_context_state import RecordStatus
from orchestune.dispatch.escalation import _rule_changes_requested
from orchestune.dispatch.gc import (
    _rule_completed,
    _rule_not_needed,
    _rule_stale_entry_hold,
)
from orchestune.dispatch.launch import LaunchContext, _launch_selected_tasks
from orchestune.dispatch.locks import ExternalLockScanResult
from orchestune.dispatch.phase_gc import run_gc_phase
from orchestune.dispatch.phase_rebase import _sync_external_locks
from orchestune.dispatch.rebase import (
    _rule_auto_rebase,
    _rule_footprint_deviation,
)
from orchestune.dispatch.rules import (
    RuleChain,
    _ActiveWorktreeAggregates,
    _RuleExecutionContext,
)
from orchestune.dispatch.scoring import (
    SchedulingResult,
    Task,
    select_tasks_with_decisions,
)
from orchestune.dispatch.state import ActiveWorktree, RunState, save_run_state
from orchestune.dispatch.targets import ClaudeCodeCloudRoutineDispatchTarget
from orchestune.integrator.coordinator import (
    IntegrationCoordinator,
    record_pending_not_needed_review,
)


def _dispatch_not_needed_review(
    issue_number: int, subtask_id: str, config: DispatcherConfig
) -> None:
    dispatch_target = config.dispatch_target
    if not isinstance(dispatch_target, ClaudeCodeCloudRoutineDispatchTarget):
        raise RuntimeError("not-needed review requires a cloud routine dispatch target")
    coordinator = IntegrationCoordinator(dispatch_target)
    handle = coordinator.dispatch_not_needed_review(issue_number, subtask_id)
    record_pending_not_needed_review(
        config.not_needed_review_state_path,
        issue_number=issue_number,
        subtask_id=subtask_id,
        session_handle=handle,
    )


# active worktreeごとの判定の優先順位(#86)。#884でphase_reconciliation.pyから
# 移設(early: status:not-needed検知とSupervisor-owned GCへ委譲するstale entry
# の非破壊hold、main: 完了検知・CHANGES_REQUESTEDエスカレーション・自動リベース
# ・footprint逸脱検知)。順序は変更しない。
_EARLY_ACTIVE_WORKTREE_RULES = RuleChain(
    rules=[
        _rule_not_needed,
        _rule_stale_entry_hold,
    ]
)

_MAIN_ACTIVE_WORKTREE_RULES = RuleChain(
    rules=[
        _rule_completed,
        _rule_changes_requested,
        _rule_auto_rebase,
        _rule_footprint_deviation,
    ]
)


def _run_active_worktree_rules(
    ctx: _RuleExecutionContext,
) -> tuple[list[dict], list[dict], bool, set[int]]:
    """#192/#193/#200/#884: active worktreeごとの完了検知・footprint逸脱処理。

    `phase_reconciliation._process_active_worktrees`から移設。完了と判定した
    エントリは（apply時）run_state.active_worktreesから除去してクオータを
    解放し、以後のfootprint逸脱チェックはスキップする。

    新しい判断パターンを追加する場合、このループ自体は変更せず、対応する
    ruleを対応するact側モジュールに書いて、`_EARLY_ACTIVE_WORKTREE_RULES`/
    `_MAIN_ACTIVE_WORKTREE_RULES`に追加するだけでよい（#86）。

    4番目の戻り値(`completed_issue_numbers`)は#873まで互換維持する旧集合
    ——完了receiptは#882/#883の`ctx.record_completion`/`is_completion_confirmed`
    が正本であり、二重recordしない。`CycleActionAdapter.process_active_worktrees`
    はこの要素を使わず`ActivePhaseResult`(3フィールド)を組み立てる。
    """
    aggregates = _ActiveWorktreeAggregates()

    for key, active in list(ctx.run_state.active_worktrees.items()):
        active_task = ctx.queries.task(active.issue_number)

        if _EARLY_ACTIVE_WORKTREE_RULES.run(ctx, key, active, active_task, aggregates):
            continue

        if active.forced_serial:
            aggregates.any_forced_serial = True

        # _MAIN_ACTIVE_WORKTREE_RULESの末尾(_rule_footprint_deviation)は必ず
        # 非Noneかつterminalな結果を返すため、戻り値を見る必要はない。
        _MAIN_ACTIVE_WORKTREE_RULES.run(ctx, key, active, active_task, aggregates)

    return (
        aggregates.completion_events,
        aggregates.deviation_events,
        aggregates.any_forced_serial,
        aggregates.completed_issue_numbers,
    )


class CycleActionAdapter:
    """L3 `CycleActions`実装（#884: process_active_worktrees/run_gcのみ）。

    `run_state`/`config`/`now`をコンストラクタで1回だけ受け取り所有する。
    `bind_context`で`CycleQueries`実装（通常は`CycleContext`自身）を1回だけ
    接続してから各portを呼ぶ。所有する`RunState`を返すgetterは公開しない。
    """

    def __init__(
        self, run_state: RunState, config: DispatcherConfig, now: float
    ) -> None:
        self._run_state = run_state
        self._config = config
        self._now = now
        self._view: CycleQueries | None = None

    def bind_context(self, view: CycleQueries) -> None:
        if self._view is not None:
            raise ValueError("CycleActionAdapter.bind_context called more than once")
        self._view = view

    def _bound_view(self) -> CycleQueries:
        if self._view is None:
            raise ValueError("CycleActionAdapter used before bind_context")
        return self._view

    def _execution_context(self) -> _RuleExecutionContext:
        view = self._bound_view()
        return _RuleExecutionContext(
            run_state=self._run_state,
            queries=view,
            config=self._config,
            prs=view.pull_requests(),
            not_needed_review_dispatcher=_dispatch_not_needed_review,
            issue_records_by_number={
                record.number: record for record in view.issue_records()
            },
            tasks_by_issue={task.issue_number: task for task in view.tasks()},
            # #884 Codex review: not display-only -- `notify_recompute`
            # (rebase.py) uses this to look up the blocked issue and actually
            # transition it to status:blocked/status:blocked-recompute, not
            # just to word a comment. Reconstructed the same way
            # `cycle_context.py` builds it for `CycleContext`.
            issue_number_by_subtask_id={
                task.subtask_id: task.issue_number
                for task in view.tasks()
                if task.subtask_id
            },
        )

    def process_active_worktrees(self) -> ActivePhaseResult:
        completion_events, deviation_events, any_forced_serial, _ = (
            _run_active_worktree_rules(self._execution_context())
        )
        return ActivePhaseResult(
            completion_events=tuple(completion_events),
            deviation_events=tuple(deviation_events),
            any_forced_serial=any_forced_serial,
        )

    def run_gc(self, events: tuple[dict[str, object], ...]) -> GcPhaseResult:
        view = self._bound_view()
        tasks_by_issue = {task.issue_number: task for task in view.tasks()}
        return run_gc_phase(
            self._run_state,
            tasks_by_issue,
            self._config,
            list(events),
            view.pull_requests(),
            now=self._now,
        )

    def scan_external_locks(self) -> ExternalLockScanResult:
        """#885: `phase_rebase._sync_external_locks`のact/portラッパー。

        `view`(=`CycleQueries`)は`LockDependencyView`
        (`task`/`assess_dependencies`/`canonical_branch`)を構造的に満たす
        （`cycle.py`の既存呼出しが`view=ctx`とするのと同じ扱い）。
        """
        view = self._bound_view()
        tasks_by_issue = {task.issue_number: task for task in view.tasks()}
        return _sync_external_locks(
            tasks_by_issue,
            list(view.pull_requests()),
            self._run_state,
            self._config,
            view=view,
        )

    def select_tasks(self, candidates: tuple[Task, ...]) -> SchedulingResult:
        """#885: `scoring.select_tasks_with_decisions`のact/portラッパー。

        `select_tasks_with_decisions`は既存selectorへの1回きりの呼出しで、
        起動後の補充・再選定は行わない。quota/critical-path/conflictに
        必要な全Task母集団は`view.tasks()`から得る（raw mapを再生成して
        decisionへ渡さない）。個々のIssueに対する参照は`view.task(...)`を
        使う。
        """
        view = self._bound_view()
        all_tasks = view.tasks()
        active_subtask_ids = {
            task.subtask_id
            for active in self._run_state.active_worktrees.values()
            if (task := view.task(active.issue_number)) is not None and task.subtask_id
        }
        return select_tasks_with_decisions(
            list(candidates),
            self._run_state,
            self._now,
            self._config.max_concurrent,
            self._config.max_launches_per_window,
            self._config.window_seconds,
            max_tokens_per_window=self._config.max_tokens_per_window,
            conflict_graph=build_task_conflict_graph(
                all_tasks,
                threshold=self._config.dag_similarity_threshold,
                ignore_patterns=self._config.dag_ignore_patterns,
            ),
            active_subtask_ids=active_subtask_ids,
            known_tasks=all_tasks,
        )

    def launch_tasks(
        self,
        selected: tuple[Task, ...],
        bases: tuple[StackBase, ...],
        candidates: tuple[Task, ...],
    ) -> tuple[Task, ...]:
        """#885: `launch._launch_selected_tasks`のact/portラッパー。

        `bases`(選出済み実行計画のtuple、`Context`のbranch map公開ではない)
        は、このメソッドの内部だけで`LaunchContext`が要求するローカルmapへ
        変換する。入力tupleは変更しない。唯一のrecord地点は#871の保存成功
        callback(`view.record_launch`)——`_launch_selected_tasks`自体は
        1回だけ呼び、バッチ内の後続追加（再選定・追加起動）は行わない。
        """
        view = self._bound_view()
        if not self._config.apply:
            return selected

        task_to_base_branch = {base.issue_number: base.branch for base in bases}

        def _record_launch(active: ActiveWorktree) -> None:
            result = view.record_launch(active)
            if result.status is RecordStatus.CONFLICT:
                raise RuntimeError(
                    "record_launch conflict for issue "
                    f"#{active.issue_number}: {result.reason}"
                )

        launched = _launch_selected_tasks(
            LaunchContext(
                list(selected),
                task_to_base_branch,
                list(candidates),
                self._run_state,
                self._now,
                self._config,
                open_prs=view.pull_requests(),
                on_launch_committed=_record_launch,
            )
        )
        self._run_state.last_reconciled_at = self._now
        save_run_state(
            self._run_state,
            self._config.run_state_path,
            now=self._now,
            launch_window_seconds=self._config.window_seconds,
            open_prs=view.pull_requests(),
        )
        return tuple(launched)


__all__ = [
    "CycleActionAdapter",
    "_run_active_worktree_rules",
]
