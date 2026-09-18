"""Scheduling/Launch Phase コーディネーター。

外部ロック・actor権限・スタッキング可否・重複起動・強制直列化の各観点で
起動候補タスクを絞り込み、クオータ判定の上で実際にタスクを起動する。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from orchestune.dispatch.cycle_action_contracts import StackBase
from orchestune.dispatch.dependency_assessment import (
    DependencyAssessment,
    DependencyState,
)
from orchestune.dispatch.dependency_policy import (
    DependencyPolicyView,
    StackDecision,
)
from orchestune.dispatch.dependency_resolution import (
    describe_unresolved_dependency,
)
from orchestune.dispatch.execution_profiles import (
    ExecutionSelection,
    resolve_task_execution_selection,
)
from orchestune.dispatch.filters import _filter_deviation_blocked_candidates
from orchestune.dispatch.launch import (
    _get_stack_eligible_tasks,
    _is_task_stack_eligible,
)
from orchestune.dispatch.locks import ExternalLockScanResult, describe_conflict
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.scoring import (
    SchedulingDecision,
    SchedulingResult,
    reconcile_decisions_with_launches,
)
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
from orchestune.task_metadata import TaskMetadata


@dataclass(frozen=True)
class SchedulingPhaseResult:
    """1サイクル分の選出結果。

    #660: 起動されたタスクだけでなく、全候補分の選定理由・rank・推定costを
    `decisions`として持ち帰り、cycle reportとイベントログから観測できるようにする。
    """

    selected: list[TaskMetadata]
    quota_slots_available: int
    decisions: list[SchedulingDecision]
    execution_selections: dict[int, ExecutionSelection] = field(default_factory=dict)
    # #787: 選定フェーズに到達する前に候補から外れたタスクと、その理由。
    # `decisions`は候補集合しか説明しないため、これが無いと運用者からは
    # 「一覧にも出てこないまま起動されない」タスクに見える。
    skips: list[SkipRecord] = field(default_factory=list)


def _filter_queued_candidates(
    ctx: CycleContext,
    lock_result: ExternalLockScanResult,
    view: DependencyPolicyView,
) -> tuple[list[TaskMetadata], list[TaskMetadata]]:
    newly_locked = {t.issue_number for t in lock_result.to_lock}
    queued_candidates: list[TaskMetadata] = [
        task
        for task in ctx.queued_tasks()
        if task.issue_number not in newly_locked
        and not ctx.is_prior_merge_held(task.issue_number)
    ]
    dependency_rejected: list[TaskMetadata] = [
        task
        for task in queued_candidates
        if (assessment := view.assess_dependencies(task.issue_number)) is None
        or bool(assessment.unresolved)
    ]
    rejected_numbers = {task.issue_number for task in dependency_rejected}
    queued_candidates = [
        task for task in queued_candidates if task.issue_number not in rejected_numbers
    ]
    return queued_candidates, dependency_rejected


def _skip_record(task: TaskMetadata, reason: str, detail: str = "") -> SkipRecord:
    return SkipRecord(
        issue_number=task.issue_number,
        subtask_id=task.subtask_id,
        reason=reason,
        detail=detail,
    )


def _dropped_tasks(
    before: list[TaskMetadata], after: list[TaskMetadata]
) -> list[TaskMetadata]:
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
    candidate_numbers = {
        task.issue_number
        for task in ctx.tasks()
        if StatusLabel.IN_PROGRESS not in task.status_labels
        and (
            StatusLabel.QUEUED in task.status_labels
            or StatusLabel.BLOCKED in task.status_labels
            or StatusLabel.EXTERNAL_LOCK in task.status_labels
        )
    }
    return [
        _skip_record(
            task, REASON_EXTERNAL_LOCK, _conflict_detail(lock_result, issue_number)
        )
        for issue_number in sorted(lock_result.conflicts)
        if (task := ctx.task(issue_number)) is not None
        and issue_number in candidate_numbers
        and StatusLabel.IN_PROGRESS not in task.status_labels
        and ctx.launch_fact(issue_number) is None
    ]


def _dependency_skips(
    blocked_tasks: tuple[TaskMetadata, ...],
    queued_dependency_rejected: Sequence[TaskMetadata],
    stack_eligible: Sequence[TaskMetadata],
    view: DependencyPolicyView,
) -> list[SkipRecord]:
    """Assessment/policyが拒否したqueued/blockedタスクの依存診断を記録する。

    PR#789レビュー対応(Codex P2): `status:blocked`は依存待ち以外の経路でも付く
    （base-branch-redの保留は`gc.completion`が、ブランチ名不正等の起動失敗は
    `launch`が付ける）。それらを一律に「依存タスク未完了」と報告すると、待って
    いる相手が空欄のまま診断を誤らせる。依存が全て解決済みなのに`status:blocked`
    が残っている状態自体は、consistency kernelの
    `status.blocked-with-resolved-dependencies`が扱う関心事である。
    """
    eligible = {task.issue_number for task in stack_eligible}
    skips = []
    for task in (*blocked_tasks, *queued_dependency_rejected):
        if task.issue_number in eligible:
            continue
        assessment = view.assess_dependencies(task.issue_number)
        decision = _is_task_stack_eligible(task, view)
        detail = _dependency_detail(task, assessment, decision)
        if detail is None:
            continue
        skips.append(_skip_record(task, REASON_DEPENDENCY, detail))
    return skips


def _unresolved_detail(assessment: DependencyAssessment) -> list[str]:
    details = []
    for dependency in assessment.unresolved:
        rendered = describe_unresolved_dependency(dependency)
        if rendered == dependency.raw:
            suffix = f" ({dependency.reason}"
            if dependency.candidates:
                suffix += ": " + ", ".join(
                    f"#{number}" for number in dependency.candidates
                )
            rendered += suffix + ")"
        details.append(rendered)
    return details


def _waiting_detail(assessment: DependencyAssessment) -> str | None:
    waiting = [
        f"#{dependency.issue_number}"
        for dependency in assessment.resolved
        if dependency.state is not DependencyState.COMPLETED
    ]
    waiting.extend(_unresolved_detail(assessment))
    return None if not waiting else f"waiting: {', '.join(waiting)}"


def _dependency_detail(
    task: TaskMetadata,
    assessment: DependencyAssessment | None,
    decision: StackDecision,
) -> str | None:
    if assessment is None:
        return f"dependency assessment unavailable: #{task.issue_number}"
    if (
        decision.blocking_issue_number not in (None, task.issue_number)
        and decision.reason
        and (
            decision.reason.startswith("grand-")
            or decision.reason == "branch-unavailable"
        )
    ):
        blocking = decision.blocking_issue_number
        if decision.reason == "grand-assessment-unavailable":
            nested = f"dependency assessment unavailable: #{blocking}"
        elif decision.reason.startswith("grand-") and decision.assessment is not None:
            nested = _waiting_detail(decision.assessment) or decision.reason
        elif decision.reason == "branch-unavailable":
            nested = "branch unavailable"
        else:
            # Defensive fallback for future policy reasons that identify a
            # nested blocker without attaching its assessment.
            nested = _waiting_detail(assessment) or decision.reason
        return f"dependency #{blocking}: {nested}"
    return _waiting_detail(assessment)


def _combine_candidate_sources(
    ctx: CycleContext,
    queued_candidates: Sequence[TaskMetadata],
    stack_eligible_tasks: Sequence[TaskMetadata],
    task_to_base_branch: dict[int, str],
) -> tuple[list[TaskMetadata], dict[int, str]]:
    """Prefer queued tasks on overlap and return the canonical sorted population."""
    queued_numbers = {task.issue_number for task in queued_candidates}
    candidate_numbers = queued_numbers | {
        task.issue_number for task in stack_eligible_tasks
    }
    candidates: list[TaskMetadata] = [
        task
        for issue_number in sorted(candidate_numbers)
        if (task := ctx.task(issue_number)) is not None
        and not ctx.is_prior_merge_held(issue_number)
    ]
    stack_bases = {
        issue_number: branch
        for issue_number, branch in task_to_base_branch.items()
        if issue_number not in queued_numbers
    }
    return candidates, stack_bases


def _determine_candidate_tasks(
    ctx: CycleContext,
    lock_result: ExternalLockScanResult,
) -> tuple[list[TaskMetadata], dict[int, str], list[SkipRecord]]:
    """起動候補タスクを、外部ロック・actor権限・スタッキング可否・重複起動・
    強制直列化の各観点で絞り込んで確定させる。

    #787: 各絞り込み段の前後差分から、落ちたタスクとその理由(`SkipRecord`)も
    併せて持ち帰る。フィルタ関数自体は純粋なまま保つ。
    """
    queued_candidates, queued_dependency_rejected = _filter_queued_candidates(
        ctx, lock_result, ctx
    )

    stack_eligible_tasks, task_to_base_branch = _get_stack_eligible_tasks(
        ctx.blocked_tasks(), ctx
    )
    skips = [
        *_external_lock_skips(ctx, lock_result),
        *_dependency_skips(
            ctx.blocked_tasks(),
            queued_dependency_rejected,
            stack_eligible_tasks,
            ctx,
        ),
    ]

    candidate_tasks, task_to_base_branch = _combine_candidate_sources(
        ctx, queued_candidates, stack_eligible_tasks, task_to_base_branch
    )
    skips.sort(key=lambda record: (record.issue_number, record.reason, record.detail))
    return candidate_tasks, task_to_base_branch, skips


_PRESELECTION_REASONS = {
    REASON_ACTOR_UNVERIFIED,
    REASON_EARLY_DEATH_BACKOFF,
    REASON_REVIEW_TIMEOUT_BACKOFF,
    REASON_FORCED_SERIAL,
    REASON_DUPLICATE_PR,
}


def _duplicate_detail(ctx: CycleContext, issue_number: int) -> str:
    resolution = ctx.branch_resolution(issue_number)
    duplicate = None if resolution is None else resolution.pr
    return "" if duplicate is None else f"PR #{duplicate.number}"


def _collect_selection_skips(
    ctx: CycleContext,
    candidate_tasks: list[TaskMetadata],
    scheduling: SchedulingResult,
    skips: list[SkipRecord],
) -> list[SchedulingDecision]:
    tasks_by_issue = {task.issue_number: task for task in candidate_tasks}
    for decision in scheduling.decisions:
        if decision.reason not in _PRESELECTION_REASONS:
            continue
        detail = (
            _duplicate_detail(ctx, decision.issue_number)
            if decision.reason == REASON_DUPLICATE_PR
            else ""
        )
        skips.append(
            _skip_record(tasks_by_issue[decision.issue_number], decision.reason, detail)
        )
    return [
        decision
        for decision in scheduling.decisions
        if decision.reason not in _PRESELECTION_REASONS
    ]


def _filter_deviated_candidates(
    ctx: CycleContext,
    candidates: list[TaskMetadata],
    deviation_events: list[dict],
    skips: list[SkipRecord],
) -> list[TaskMetadata]:
    undeviated = _filter_deviation_blocked_candidates(
        candidates,
        deviation_events,
        {task.subtask_id: task.issue_number for task in ctx.tasks() if task.subtask_id},
    )
    skips.extend(
        _skip_record(task, REASON_DEVIATION_BLOCKED)
        for task in _dropped_tasks(candidates, undeviated)
    )
    return undeviated


def run_scheduling_phase(
    ctx: CycleContext,
    lock_result: ExternalLockScanResult,
    deviation_events: list[dict],
) -> SchedulingPhaseResult:
    """起動候補の確定からクオータ判定・実起動までの一連を行う。

    `_filter_deviation_blocked_candidates`（deviation_eventsによる絞り込み）
    はdispatch_filtersに定義済みのため、ここではそれを呼び出す。
    """
    candidate_tasks, task_to_base_branch, skips = _determine_candidate_tasks(
        ctx, lock_result
    )

    candidate_tasks = _filter_deviated_candidates(
        ctx, candidate_tasks, deviation_events, skips
    )

    scheduling = ctx.select_tasks(tuple(candidate_tasks))
    decisions = _collect_selection_skips(ctx, candidate_tasks, scheduling, skips)
    selected = list(
        ctx.launch_tasks(
            tuple(scheduling.selected),
            tuple(
                StackBase(issue_number, branch)
                for issue_number, branch in sorted(task_to_base_branch.items())
            ),
            tuple(candidate_tasks),
        )
    )

    execution_selections = {
        task.issue_number: resolve_task_execution_selection(task, ctx.config)
        for task in selected
    }
    return SchedulingPhaseResult(
        selected=selected,
        quota_slots_available=scheduling.quota_slots_available or 0,
        # 実起動は選出の部分集合になり得る（起動枠の予約失敗・起動失敗）。
        # レポートが実態と食い違わないよう、起動結果で判定を突き合わせる。
        decisions=reconcile_decisions_with_launches(decisions, selected),
        execution_selections=execution_selections,
        skips=sorted(
            skips,
            key=lambda record: (record.issue_number, record.reason, record.detail),
        ),
    )
