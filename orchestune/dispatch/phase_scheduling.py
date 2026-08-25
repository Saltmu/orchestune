"""Scheduling/Launch Phase コーディネーター。

外部ロック・actor権限・スタッキング可否・重複起動・強制直列化の各観点で
起動候補タスクを絞り込み、クオータ判定の上で実際にタスクを起動する。
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestune.dispatch.actor_verification import (
    _apply_actor_verification,
    _decide_actor_verification,
)
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.conflicts import build_task_conflict_graph
from orchestune.dispatch.cycle_context import IssuesByStatus
from orchestune.dispatch.filters import (
    _filter_candidates_for_forced_serial,
    _filter_deviation_blocked_candidates,
)
from orchestune.dispatch.launch import (
    LaunchContext,
    _apply_duplicate_skip,
    _decide_duplicate_candidates,
    _get_stack_eligible_tasks,
    _launch_selected_tasks,
)
from orchestune.dispatch.locks import ExternalLockScanResult
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.scoring import (
    SchedulingDecision,
    SchedulingResult,
    Task,
    quota_available,
    select_tasks_with_decisions,
)
from orchestune.dispatch.state import save_run_state


@dataclass(frozen=True)
class SchedulingPhaseResult:
    """1サイクル分の選出結果。

    #660: 起動されたタスクだけでなく、全候補分の選定理由・rank・推定costを
    `decisions`として持ち帰り、cycle reportとイベントログから観測できるようにする。
    """

    selected: list[Task]
    quota_slots_available: int
    decisions: list[SchedulingDecision]


def _filter_queued_candidates(
    ctx: CycleContext,
    issues: IssuesByStatus,
    lock_result: ExternalLockScanResult,
) -> list[Task]:
    newly_locked = {t.issue_number for t in lock_result.to_lock}
    queued_candidates = [
        ctx.tasks_by_issue[issue.number]
        for issue in issues.queued
        if issue.number not in newly_locked
        and "status:done" not in ctx.tasks_by_issue[issue.number].status_labels
        and "status:in-progress" not in ctx.tasks_by_issue[issue.number].status_labels
    ]
    actor_decisions = _decide_actor_verification(
        queued_candidates, forge=ctx.config.resolved_forge
    )
    return _apply_actor_verification(actor_decisions, ctx.config)


def _determine_candidate_tasks(
    ctx: CycleContext,
    issues: IssuesByStatus,
    lock_result: ExternalLockScanResult,
    completed_subtask_ids: set[str],
    any_forced_serial: bool,
) -> tuple[list[Task], dict[int, str]]:
    """起動候補タスクを、外部ロック・actor権限・スタッキング可否・重複起動・
    強制直列化の各観点で絞り込んで確定させる。"""
    queued_candidates = _filter_queued_candidates(ctx, issues, lock_result)

    stack_eligible_tasks, task_to_base_branch = _get_stack_eligible_tasks(
        issues.blocked,
        ctx.tasks_by_issue,
        ctx.done_subtask_ids,
        ctx.ci_passed_pr_subtask_ids,
        ctx.subtask_branch_map,
        completed_subtask_ids=completed_subtask_ids,
    )

    candidate_tasks = queued_candidates + stack_eligible_tasks
    duplicate_decisions = _decide_duplicate_candidates(candidate_tasks, ctx)
    candidate_tasks = _apply_duplicate_skip(duplicate_decisions, ctx)

    if any_forced_serial:
        candidate_tasks = _filter_candidates_for_forced_serial(
            candidate_tasks,
            ctx.run_state,
            ctx.tasks_by_issue,
        )

    return candidate_tasks, task_to_base_branch


def _finalize_launch(
    selected: list[Task],
    task_to_base_branch: dict[int, str],
    candidate_tasks: list[Task],
    ctx: CycleContext,
    now: float,
    config: DispatcherConfig,
) -> list[Task]:
    """apply時のみ、選出タスクを実起動しrun_stateを永続化する。"""
    if not config.apply:
        return selected
    selected = _launch_selected_tasks(
        LaunchContext(
            selected,
            task_to_base_branch,
            candidate_tasks,
            ctx.run_state,
            now,
            config,
            open_prs=ctx.prs,
        )
    )
    ctx.run_state.last_reconciled_at = now
    save_run_state(
        ctx.run_state,
        config.run_state_path,
        now=now,
        launch_window_seconds=config.window_seconds,
        open_prs=ctx.prs,
    )
    return selected


def _select_tasks_for_cycle(
    ctx: CycleContext,
    candidate_tasks: list[Task],
    now: float,
    config: DispatcherConfig,
) -> SchedulingResult:
    """クオータ・競合・トークン予算の下で起動タスクを選出する。"""
    return select_tasks_with_decisions(
        candidate_tasks,
        ctx.run_state,
        now,
        config.max_concurrent,
        config.max_launches_per_window,
        config.window_seconds,
        max_tokens_per_window=config.max_tokens_per_window,
        conflict_graph=build_task_conflict_graph(
            ctx.tasks_by_issue.values(),
            threshold=config.dag_similarity_threshold,
            ignore_patterns=config.dag_ignore_patterns,
        ),
        active_subtask_ids={
            task.subtask_id
            for active in ctx.run_state.active_worktrees.values()
            if (task := ctx.tasks_by_issue.get(active.issue_number)) is not None
            and task.subtask_id
        },
        scheduling_mode=config.scheduling_mode,
        # Precedence DAGの母集団はサイクルが見ている全タスク。候補集合だけで
        # 組むと、まだ依存待ちで候補に入っていない後続が数えられず、共有契約
        # タスクのcritical-path rankが過小評価される。
        known_tasks=ctx.tasks_by_issue.values(),
    )


def run_scheduling_phase(
    ctx: CycleContext,
    issues: IssuesByStatus,
    lock_result: ExternalLockScanResult,
    completed_subtask_ids: set[str],
    any_forced_serial: bool,
    deviation_events: list[dict],
    now: float,
    config: DispatcherConfig,
) -> SchedulingPhaseResult:
    """起動候補の確定からクオータ判定・実起動までの一連を行う。

    `_filter_deviation_blocked_candidates`（deviation_eventsによる絞り込み）
    はdispatch_filtersに定義済みのため、ここではそれを呼び出す。
    """
    candidate_tasks, task_to_base_branch = _determine_candidate_tasks(
        ctx, issues, lock_result, completed_subtask_ids, any_forced_serial
    )

    candidate_tasks = _filter_deviation_blocked_candidates(
        candidate_tasks,
        deviation_events,
        ctx.issue_number_by_subtask_id,
    )

    quota_slots = quota_available(
        ctx.run_state,
        now,
        config.max_concurrent,
        config.max_launches_per_window,
        config.window_seconds,
        max_tokens_per_window=config.max_tokens_per_window,
    )
    scheduling = _select_tasks_for_cycle(ctx, candidate_tasks, now, config)
    selected = _finalize_launch(
        scheduling.selected, task_to_base_branch, candidate_tasks, ctx, now, config
    )
    return SchedulingPhaseResult(
        selected=selected,
        quota_slots_available=quota_slots,
        decisions=scheduling.decisions,
    )
