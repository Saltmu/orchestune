"""`CycleActions` portのL3実装。

一つのdispatch cycleにつき一つのadapterが`RunState`を所有し、一つの
`CycleContext`へ一度だけ束縛される。束縛前のport利用と二度目の束縛は
`ValueError`にして、全phaseが同じ状態境界を使うことを保証する。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass

from orchestune.consistency.models import RepairCommand, RepairResult
from orchestune.consistency.repairs.execution import (
    COMMAND_BOOKKEEPING,
    COMMAND_RECLAIM,
    COMMAND_REQUEUE,
)
from orchestune.dag.models import SubTask
from orchestune.dispatch.actor_verification import (
    _apply_actor_verification,
    _decide_actor_verification,
)
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.conflicts import build_task_conflict_graph
from orchestune.dispatch.critical_path import pending_tasks
from orchestune.dispatch.cycle_action_contracts import (
    ActivePhaseResult,
    GcPhaseResult,
    StackBase,
)
from orchestune.dispatch.cycle_context import (
    _build_task_mappings,
    _dispatch_not_needed_review,
    _fetch_issues,
)
from orchestune.dispatch.cycle_context_state import RecordStatus
from orchestune.dispatch.cycle_records import _on_status_transition_verified
from orchestune.dispatch.escalation import _rule_changes_requested
from orchestune.dispatch.execution_repair import DispatchRepairExecutorAdapter
from orchestune.dispatch.filters import _filter_candidates_for_forced_serial
from orchestune.dispatch.gc import (
    _rule_completed,
    _rule_not_needed,
    _rule_stale_entry_hold,
)
from orchestune.dispatch.launch import (
    LaunchContext,
    _apply_duplicate_skip,
    _decide_duplicate_candidates,
    _launch_selected_tasks,
)
from orchestune.dispatch.locks import ExternalLockScanResult
from orchestune.dispatch.phase_gc import build_gc_reclaim_handler, run_gc_phase
from orchestune.dispatch.phase_rebase import _sync_external_locks
from orchestune.dispatch.rebase import (
    _rule_auto_rebase,
    _rule_footprint_deviation,
)
from orchestune.dispatch.reconciliation import (
    _handle_base_branch_red_recovery,
    _handle_blocked_recompute_recovery,
)
from orchestune.dispatch.recovery import (
    RecoveryBookkeepingAdapter,
    execute_bookkeeping_repair_command,
    execute_recovery_requeue_command,
)
from orchestune.dispatch.rules import (
    CycleContext,
    RuleChain,
    _ActiveWorktreeAggregates,
    _RuleExecutionContext,
)
from orchestune.dispatch.scoring import (
    SCHEDULING_MODE_CRITICAL_PATH,
    SchedulingDecision,
    SchedulingResult,
    ScoreComponents,
    select_tasks_with_decisions,
)
from orchestune.dispatch.state import ActiveWorktree, RunState, save_run_state
from orchestune.dispatch.status_repair import execute_status_repair_command
from orchestune.dispatch.summary import (
    REASON_ACTOR_UNVERIFIED,
    REASON_DUPLICATE_PR,
    REASON_EARLY_DEATH_BACKOFF,
    REASON_FORCED_SERIAL,
    REASON_REVIEW_TIMEOUT_BACKOFF,
)
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord
from orchestune.task_metadata import TaskMetadata


@dataclass(frozen=True, slots=True)
class _IssueRecordsView:
    """`.all()`だけを要求する`reconciliation.py`のrecovery関数向けの最小shim。

    `queries.issue_records()`(初期Forge観測)をそのまま`.all()`として返す。
    `status:blocked-recompute`/`ci:base-branch-red`はForgeが直接管理する
    生ラベルであり、record反映後の実効ラベルではないため、初期観測で十分。
    """

    _records: tuple[IssueRecord, ...]

    def all(self) -> tuple[IssueRecord, ...]:
        return self._records


# active worktreeごとの判定順序。early chainはnot-neededとstale entryを先に
# 処理し、main chainは完了・レビュー差戻し・rebase・footprint逸脱を評価する。
# この順序は状態遷移の優先順位であるため変更しない。
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
) -> tuple[list[dict], list[dict], bool]:
    """active worktreeを優先順位付きRuleChainで評価する。

    完了したentryは以後の判定を行わず、完了receiptはcontextのrecord/queryが
    所有する。戻り値はレポート用イベントだけであり、状態の正本ではない。
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
    )


def _filter_retry_backoffs(
    candidates: tuple[TaskMetadata, ...], run_state: RunState, now: float
) -> tuple[list[TaskMetadata], list[tuple[TaskMetadata, str]]]:
    eligible: list[TaskMetadata] = []
    excluded: list[tuple[TaskMetadata, str]] = []
    for task in candidates:
        record = run_state.task_reclaim_counts.get(task.issue_number)
        if record is not None and record.early_death_retry_at > now:
            excluded.append((task, REASON_EARLY_DEATH_BACKOFF))
        elif record is not None and record.review_timeout_retry_at > now:
            excluded.append((task, REASON_REVIEW_TIMEOUT_BACKOFF))
        else:
            eligible.append(task)
    return eligible, excluded


def _filter_actor_permissions(
    candidates: list[TaskMetadata], config: DispatcherConfig
) -> tuple[list[TaskMetadata], list[tuple[TaskMetadata, str]]]:
    queued = [task for task in candidates if StatusLabel.QUEUED in task.status_labels]
    nonqueued = [
        task for task in candidates if StatusLabel.QUEUED not in task.status_labels
    ]
    decisions = _decide_actor_verification(queued, forge=config.resolved_forge)
    authorized = [*nonqueued, *_apply_actor_verification(decisions, config)]
    authorized_numbers = {task.issue_number for task in authorized}
    excluded = [
        (decision.task, REASON_ACTOR_UNVERIFIED)
        for decision in decisions
        if decision.task.issue_number not in authorized_numbers
    ]
    return authorized, excluded


def _filter_duplicate_candidates(
    candidates: list[TaskMetadata],
    view: CycleContext,
    run_state: RunState,
    config: DispatcherConfig,
) -> tuple[list[TaskMetadata], list[tuple[TaskMetadata, str]]]:
    decisions = _decide_duplicate_candidates(
        candidates, view, run_state.completed_worktrees
    )
    survivors = _apply_duplicate_skip(decisions, config)
    survivor_numbers = {task.issue_number for task in survivors}
    excluded = [
        (decision.task, REASON_DUPLICATE_PR)
        for decision in decisions
        if decision.task.issue_number not in survivor_numbers
    ]
    return survivors, excluded


def _filter_forced_serial_candidates(
    candidates: list[TaskMetadata], run_state: RunState, view: CycleContext
) -> tuple[list[TaskMetadata], list[tuple[TaskMetadata, str]]]:
    survivors = _filter_candidates_for_forced_serial(candidates, run_state, view)
    survivor_numbers = {task.issue_number for task in survivors}
    excluded = [
        (task, REASON_FORCED_SERIAL)
        for task in candidates
        if task.issue_number not in survivor_numbers
    ]
    return survivors, excluded


def _preselection_decisions(
    excluded: list[tuple[TaskMetadata, str]],
) -> list[SchedulingDecision]:
    return [
        SchedulingDecision(
            task.issue_number,
            task.subtask_id,
            SCHEDULING_MODE_CRITICAL_PATH,
            0.0,
            ScoreComponents(),
            reason=reason,
        )
        for task, reason in sorted(excluded, key=lambda item: item[0].issue_number)
    ]


def _scheduling_dag_inputs(
    view: CycleContext, tasks: Sequence[TaskMetadata]
) -> tuple[tuple[SubTask, ...], tuple[SubTask, ...]]:
    rank_inputs = view.dag_inputs(
        tuple(task.issue_number for task in pending_tasks(tasks))
    )
    conflict_inputs = view.dag_inputs(tuple(task.issue_number for task in tasks))
    return rank_inputs, conflict_inputs


class CycleActionAdapter:
    """L3 `CycleActions`の全port実装。

    `run_state`/`config`/`now`を構築時に受け取り、全portは一度束縛した
    `CycleContext`を使う。adapterが所有する`RunState`を外部へ公開しない。
    """

    def __init__(
        self, run_state: RunState, config: DispatcherConfig, now: float
    ) -> None:
        self._run_state = run_state
        self._config = config
        self._now = now
        self._view: CycleContext | None = None
        self._completion_events: list[dict] = []

    def bind_context(self, view: CycleContext) -> None:
        """全portで共有する具体的な`CycleContext`を一度だけ束縛する。

        recoveryとrepairを含む全portが同じcontextの確認済み状態を読むため、
        部分的に弱いquery viewへ差し替えることは許可しない。
        """
        if self._view is not None:
            raise ValueError("CycleActionAdapter.bind_context called more than once")
        self._view = view

    def _bound_view(self) -> CycleContext:
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
            dag_inputs=view.dag_inputs(
                tuple(task.issue_number for task in view.tasks())
            ),
            # footprint逸脱時に対象Issueを遷移させるための逆引き。表示用ではない。
            issue_number_by_subtask_id={
                task.subtask_id: task.issue_number
                for task in view.tasks()
                if task.subtask_id
            },
        )

    def process_active_worktrees(self) -> ActivePhaseResult:
        completion_events, deviation_events, any_forced_serial = (
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
        result = run_gc_phase(
            self._run_state,
            tasks_by_issue,
            self._config,
            list(events),
            view.pull_requests(),
            now=self._now,
        )
        self._completion_events = result.completion_events
        return result

    def scan_external_locks(self) -> ExternalLockScanResult:
        """外部lockを、束縛済みcontextの依存queryで評価する。"""
        view = self._bound_view()
        tasks_by_issue = {task.issue_number: task for task in view.tasks()}
        return _sync_external_locks(
            tasks_by_issue,
            list(view.pull_requests()),
            self._run_state,
            self._config,
            view=view,
        )

    def select_tasks(self, candidates: tuple[TaskMetadata, ...]) -> SchedulingResult:
        """候補を一度選定し、派生DAG入力だけをscoringへ渡す。

        起動後の補充・再選定は行わない。個別taskはcontext queryで取得し、
        raw dependency declarationを選定処理へ渡さない。
        """
        view = self._bound_view()
        eligible, excluded = _filter_retry_backoffs(
            candidates, self._run_state, self._now
        )
        authorized, actor_excluded = _filter_actor_permissions(eligible, self._config)
        excluded.extend(actor_excluded)
        nonduplicates, duplicate_excluded = _filter_duplicate_candidates(
            authorized, view, self._run_state, self._config
        )
        excluded.extend(duplicate_excluded)
        serial_filtered, serial_excluded = _filter_forced_serial_candidates(
            nonduplicates, self._run_state, view
        )
        excluded.extend(serial_excluded)
        all_tasks = view.tasks()
        rank_inputs, conflict_inputs = _scheduling_dag_inputs(view, all_tasks)
        active_subtask_ids = {
            active_task.subtask_id
            for active in self._run_state.active_worktrees.values()
            if (active_task := view.task(active.issue_number)) is not None
            and active_task.subtask_id
        }
        result = select_tasks_with_decisions(
            serial_filtered,
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
                derived_inputs=conflict_inputs,
            ),
            active_subtask_ids=active_subtask_ids,
            known_tasks=all_tasks,
            derived_inputs=rank_inputs,
        )
        preselection = _preselection_decisions(excluded)
        return SchedulingResult(
            result.selected,
            [*result.decisions, *preselection],
            quota_slots_available=result.quota_slots_available,
        )

    def launch_tasks(
        self,
        selected: tuple[TaskMetadata, ...],
        bases: tuple[StackBase, ...],
        candidates: tuple[TaskMetadata, ...],
    ) -> tuple[TaskMetadata, ...]:
        """選定済みtaskを起動し、保存成功後だけ起動事実を記録する。

        `bases`はこのメソッド内でlaunch用mapへ変換する。入力tupleは変更せず、
        バッチ内で追加の選定・起動は行わない。
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

    def reconcile_recovery(self) -> tuple[dict[str, object], ...]:
        """通常cycleのpost-GC復帰を、束縛済みcontextの確認済み事実で処理する。"""
        ctx = self._bound_view()
        issues = _IssueRecordsView(ctx.issue_records())
        events = _handle_blocked_recompute_recovery(
            issues, self._run_state, ctx, self._config
        )
        events.extend(
            _handle_base_branch_red_recovery(issues, ctx, self._run_state, self._config)
        )
        return tuple(events)

    def execute_repair(self, command: RepairCommand) -> RepairResult:
        """repair commandを限定されたhandlerへ委譲する。

        `status.*`は再取得したIssueからtask mappingだけを更新し、同一cycleの
        completion evidenceとrecord状態は束縛済みcontextに保持する。その他の
        commandは明示的なhandlerが無ければfail-closedとし、任意commandの実行や
        独自retry loopは持たない。

        task mappingは`cycle_context`の`_fetch_issues`と`_build_task_mappings`で
        再構築する。`cycle.py`は`phase_reconciliation`経由でこのモジュールをimport
        するため、そこにあるconsistency adapterを再利用すると循環importになる。
        """
        ctx = self._bound_view()
        if command.code == COMMAND_RECLAIM:
            fresh_issues = _fetch_issues(self._config).filtered_by_parent(
                self._config.parent_issue_number
            )
            tasks_by_issue, _, _ = _build_task_mappings(fresh_issues.all())
            handler = build_gc_reclaim_handler(
                self._run_state,
                tasks_by_issue,
                self._config,
                self._completion_events,
                ctx.pull_requests(),
                now=self._now,
            )
            return handler(command)
        if command.code in {COMMAND_REQUEUE, COMMAND_BOOKKEEPING}:
            recovery = RecoveryBookkeepingAdapter(
                os.environ.get("GITHUB_REPOSITORY") or "orchestune-repository",
                self._run_state,
                self._config,
                now=self._now,
            )
            recovery.observe()
            if command.code == COMMAND_REQUEUE:
                return execute_recovery_requeue_command(
                    command, self._run_state, recovery.snapshot, self._config
                )
            return execute_bookkeeping_repair_command(
                command, self._run_state, recovery.snapshot, self._config
            )
        if not command.code.startswith("status."):
            return DispatchRepairExecutorAdapter({}).execute(command)

        fresh_issues = _fetch_issues(self._config).filtered_by_parent(
            self._config.parent_issue_number
        )
        tasks_by_issue, _, _ = _build_task_mappings(fresh_issues.all())
        return execute_status_repair_command(
            command,
            tasks_by_issue,
            completion_evidence=ctx,
            config=self._config,
            on_verified=_on_status_transition_verified(
                ctx,
                has_active_entry=lambda issue_number: any(
                    active.issue_number == issue_number
                    for active in self._run_state.active_worktrees.values()
                ),
            ),
        )


__all__ = [
    "CycleActionAdapter",
    "_run_active_worktree_rules",
]
