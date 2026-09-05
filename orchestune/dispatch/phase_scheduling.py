"""Scheduling/Launch Phase コーディネーター。

外部ロック・actor権限・スタッキング可否・重複起動・強制直列化の各観点で
起動候補タスクを絞り込み、クオータ判定の上で実際にタスクを起動する。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from orchestune.dispatch.actor_verification import (
    _apply_actor_verification,
    _decide_actor_verification,
)
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.conflicts import build_task_conflict_graph
from orchestune.dispatch.cycle_context import IssuesByStatus
from orchestune.dispatch.dependency_resolution import (
    EMPTY_DEPENDENCIES,
    describe_unresolved_dependency,
)
from orchestune.dispatch.execution_profiles import (
    ExecutionSelection,
    resolve_task_execution_selection,
)
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
from orchestune.dispatch.locks import ExternalLockScanResult, describe_conflict
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.scoring import (
    SchedulingDecision,
    SchedulingResult,
    Task,
    quota_available,
    reconcile_decisions_with_launches,
    select_tasks_with_decisions,
)
from orchestune.dispatch.state import save_run_state
from orchestune.dispatch.summary import (
    REASON_ACTOR_UNVERIFIED,
    REASON_DEPENDENCY,
    REASON_DEVIATION_BLOCKED,
    REASON_DUPLICATE_PR,
    REASON_EARLY_DEATH_BACKOFF,
    REASON_EXTERNAL_LOCK,
    REASON_FORCED_SERIAL,
    REASON_REVIEW_TIMEOUT_BACKOFF,
    SkipRecord,
)
from orchestune.labels import StatusLabel


@dataclass(frozen=True)
class SchedulingPhaseResult:
    """1サイクル分の選出結果。

    #660: 起動されたタスクだけでなく、全候補分の選定理由・rank・推定costを
    `decisions`として持ち帰り、cycle reportとイベントログから観測できるようにする。
    """

    selected: list[Task]
    quota_slots_available: int
    decisions: list[SchedulingDecision]
    execution_selections: dict[int, ExecutionSelection] = field(default_factory=dict)
    # #787: 選定フェーズに到達する前に候補から外れたタスクと、その理由。
    # `decisions`は候補集合しか説明しないため、これが無いと運用者からは
    # 「一覧にも出てこないまま起動されない」タスクに見える。
    skips: list[SkipRecord] = field(default_factory=list)


def _filter_queued_candidates(
    ctx: CycleContext,
    issues: IssuesByStatus,
    lock_result: ExternalLockScanResult,
    now: float = 0.0,
) -> list[Task]:
    newly_locked = {t.issue_number for t in lock_result.to_lock}
    queued_candidates = [
        ctx.tasks_by_issue[issue.number]
        for issue in issues.queued
        if issue.number not in newly_locked
        and StatusLabel.DONE not in ctx.tasks_by_issue[issue.number].status_labels
        and StatusLabel.IN_PROGRESS
        not in ctx.tasks_by_issue[issue.number].status_labels
        and (
            (record := ctx.run_state.task_reclaim_counts.get(issue.number)) is None
            or max(record.early_death_retry_at, record.review_timeout_retry_at) <= now
        )
    ]
    actor_decisions = _decide_actor_verification(
        queued_candidates, forge=ctx.config.resolved_forge
    )
    return _apply_actor_verification(actor_decisions, ctx.config)


def _skip_record(task: Task, reason: str, detail: str = "") -> SkipRecord:
    return SkipRecord(
        issue_number=task.issue_number,
        subtask_id=task.subtask_id,
        reason=reason,
        detail=detail,
    )


def _dropped_tasks(before: list[Task], after: list[Task]) -> list[Task]:
    """フィルタ適用の前後差分。フィルタ関数自体は純粋なまま理由を取り出す。"""
    survivors = {task.issue_number for task in after}
    return [task for task in before if task.issue_number not in survivors]


def _conflict_detail(lock_result: ExternalLockScanResult, issue_number: int) -> str:
    conflicts = lock_result.conflicts.get(issue_number, ())
    if not conflicts:
        return ""
    head = describe_conflict(conflicts[0])
    return head if len(conflicts) == 1 else f"{head}, +{len(conflicts) - 1}"


def _external_lock_skips(
    ctx: CycleContext, lock_result: ExternalLockScanResult
) -> list[SkipRecord]:
    """新規ロックと、前サイクルから継続してロック中のタスクの双方を拾う。

    継続ロックは`to_lock`にも`issues.queued`にも現れないため、これが無いと
    「ずっと止まっているのに一覧に出ない」タスクが残る（#695の実例）。

    PR#789レビュー対応(Codex P2): 母集団は`issues.locked`ではなく現在の
    `conflicts`とする。ラベルを持つIssueを起点にすると、同じサイクルで
    ロックを外すタスク（`to_unlock`）や、終端状態のまま古いラベルが残って
    いるタスクまで「外部ロックで見送った候補」として報告してしまう。
    `to_lock`は必ず`conflicts`に含まれる（`scan_external_locks`参照）。
    """
    running = {
        active.issue_number for active in ctx.run_state.active_worktrees.values()
    }
    return [
        _skip_record(
            task, REASON_EXTERNAL_LOCK, _conflict_detail(lock_result, issue_number)
        )
        for issue_number in sorted(lock_result.conflicts)
        if (task := ctx.tasks_by_issue.get(issue_number)) is not None
        # PR#789レビュー対応(Codex P2): 実行中のタスクは起動候補ではない。
        # `scan_external_locks`はdone/not-neededしか除外しないため、実行中の
        # タスクが外部ブランチと重なると「見送った候補」として報告されてしまう。
        and issue_number not in running
        and StatusLabel.IN_PROGRESS not in task.status_labels
    ]


def _queued_drop_skips(
    ctx: CycleContext,
    issues: IssuesByStatus,
    lock_result: ExternalLockScanResult,
    survivors: list[Task],
    now: float,
) -> list[SkipRecord]:
    """`_filter_queued_candidates`が落としたqueuedタスクの理由を復元する。

    `status:done` / `status:in-progress`のタスクは「起動されなかった」とは
    言えないため要約には載せない。

    PR#789レビュー対応(Codex P2): 新規ロックされたタスクも`_filter_queued_candidates`
    から外れるが、その理由は`_external_lock_skips`が衝突の詳細付きで既に記録して
    いる。ここで拾うと「actor権限の未確認で落ちた」という誤った記録がJSONレポートと
    events.jsonlに残る。
    """
    survivor_numbers = {task.issue_number for task in survivors}
    survivor_numbers |= {task.issue_number for task in lock_result.to_lock}
    skips = []
    for issue in issues.queued:
        task = ctx.tasks_by_issue.get(issue.number)
        if task is None or issue.number in survivor_numbers:
            continue
        if StatusLabel.DONE in task.status_labels:
            continue
        if StatusLabel.IN_PROGRESS in task.status_labels:
            continue
        record = ctx.run_state.task_reclaim_counts.get(issue.number)
        if record is not None and record.early_death_retry_at > now:
            skips.append(_skip_record(task, REASON_EARLY_DEATH_BACKOFF))
        elif record is not None and record.review_timeout_retry_at > now:
            skips.append(_skip_record(task, REASON_REVIEW_TIMEOUT_BACKOFF))
        else:
            skips.append(_skip_record(task, REASON_ACTOR_UNVERIFIED))
    return skips


def _dependency_skips(
    ctx: CycleContext, issues: IssuesByStatus, stack_eligible: list[Task]
) -> list[SkipRecord]:
    """未解決の依存を実際に持つ`status:blocked`タスクだけを依存待ちとして記録する。

    PR#789レビュー対応(Codex P2): `status:blocked`は依存待ち以外の経路でも付く
    （base-branch-redの保留は`gc.completion`が、ブランチ名不正等の起動失敗は
    `launch`が付ける）。それらを一律に「依存タスク未完了」と報告すると、待って
    いる相手が空欄のまま診断を誤らせる。依存が全て解決済みなのに`status:blocked`
    が残っている状態自体は、consistency kernelの
    `status.blocked-with-resolved-dependencies`が扱う関心事である。
    """
    eligible = {task.issue_number for task in stack_eligible}
    done_issue_numbers = ctx.done_issue_numbers
    skips = []
    for issue in issues.blocked:
        task = ctx.tasks_by_issue.get(issue.number)
        if task is None or issue.number in eligible:
            continue
        deps = ctx.dependency_resolution.get(task.issue_number, EMPTY_DEPENDENCIES)
        waiting = [
            f"#{dep_issue}"
            for dep_issue in deps.resolved
            if dep_issue not in done_issue_numbers
        ]
        waiting.extend(
            describe_unresolved_dependency(dependency) for dependency in deps.unresolved
        )
        if not waiting:
            continue
        skips.append(
            _skip_record(task, REASON_DEPENDENCY, f"waiting: {', '.join(waiting)}")
        )
    return skips


def _drop_duplicate_candidates(
    candidate_tasks: list[Task], ctx: CycleContext
) -> tuple[list[Task], list[SkipRecord]]:
    """既にオープンなPRを持つ候補を外し、除外した理由（PR番号）を残す。"""
    decisions = _decide_duplicate_candidates(candidate_tasks, ctx)
    remaining = _apply_duplicate_skip(decisions, ctx)
    existing_prs = {
        decision.task.issue_number: decision.existing_pr
        for decision in decisions
        if decision.existing_pr is not None
    }
    skips = [
        _skip_record(
            task,
            REASON_DUPLICATE_PR,
            f"PR #{pr.number}" if (pr := existing_prs.get(task.issue_number)) else "",
        )
        for task in _dropped_tasks(candidate_tasks, remaining)
    ]
    return remaining, skips


def _determine_candidate_tasks(
    ctx: CycleContext,
    issues: IssuesByStatus,
    lock_result: ExternalLockScanResult,
    completed_issue_numbers: set[int],
    any_forced_serial: bool,
    now: float = 0.0,
) -> tuple[list[Task], dict[int, str], list[SkipRecord]]:
    """起動候補タスクを、外部ロック・actor権限・スタッキング可否・重複起動・
    強制直列化の各観点で絞り込んで確定させる。

    #787: 各絞り込み段の前後差分から、落ちたタスクとその理由(`SkipRecord`)も
    併せて持ち帰る。フィルタ関数自体は純粋なまま保つ。
    """
    queued_candidates = _filter_queued_candidates(ctx, issues, lock_result, now)

    stack_eligible_tasks, task_to_base_branch = _get_stack_eligible_tasks(
        issues.blocked,
        ctx.tasks_by_issue,
        ctx.done_issue_numbers,
        ctx.ci_passed_pr_issue_numbers,
        ctx.branch_by_issue_number,
        ctx.dependency_resolution,
        completed_issue_numbers=completed_issue_numbers,
    )
    skips = [
        *_external_lock_skips(ctx, lock_result),
        *_queued_drop_skips(ctx, issues, lock_result, queued_candidates, now),
        *_dependency_skips(ctx, issues, stack_eligible_tasks),
    ]

    candidate_tasks = queued_candidates + stack_eligible_tasks
    candidate_tasks, duplicate_skips = _drop_duplicate_candidates(candidate_tasks, ctx)
    skips.extend(duplicate_skips)

    if any_forced_serial:
        serialized = _filter_candidates_for_forced_serial(
            candidate_tasks,
            ctx.run_state,
            ctx.tasks_by_issue,
            ctx.dependency_resolution,
        )
        skips.extend(
            _skip_record(task, REASON_FORCED_SERIAL)
            for task in _dropped_tasks(candidate_tasks, serialized)
        )
        candidate_tasks = serialized

    return candidate_tasks, task_to_base_branch, skips


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
        # Precedence DAGの母集団はサイクルが見ている全タスク。候補集合だけで
        # 組むと、まだ依存待ちで候補に入っていない後続が数えられず、共有契約
        # タスクのcritical-path rankが過小評価される。
        known_tasks=ctx.tasks_by_issue.values(),
    )


def run_scheduling_phase(
    ctx: CycleContext,
    issues: IssuesByStatus,
    lock_result: ExternalLockScanResult,
    completed_issue_numbers: set[int],
    any_forced_serial: bool,
    deviation_events: list[dict],
    now: float,
    config: DispatcherConfig,
) -> SchedulingPhaseResult:
    """起動候補の確定からクオータ判定・実起動までの一連を行う。

    `_filter_deviation_blocked_candidates`（deviation_eventsによる絞り込み）
    はdispatch_filtersに定義済みのため、ここではそれを呼び出す。
    """
    candidate_tasks, task_to_base_branch, skips = _determine_candidate_tasks(
        ctx, issues, lock_result, completed_issue_numbers, any_forced_serial, now
    )

    undeviated = _filter_deviation_blocked_candidates(
        candidate_tasks,
        deviation_events,
        ctx.issue_number_by_subtask_id,
    )
    skips.extend(
        _skip_record(task, REASON_DEVIATION_BLOCKED)
        for task in _dropped_tasks(candidate_tasks, undeviated)
    )
    candidate_tasks = undeviated

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

    execution_selections = {
        task.issue_number: resolve_task_execution_selection(task, config)
        for task in selected
    }
    return SchedulingPhaseResult(
        selected=selected,
        quota_slots_available=quota_slots,
        # 実起動は選出の部分集合になり得る（起動枠の予約失敗・起動失敗）。
        # レポートが実態と食い違わないよう、起動結果で判定を突き合わせる。
        decisions=reconcile_decisions_with_launches(scheduling.decisions, selected),
        execution_selections=execution_selections,
        skips=skips,
    )
