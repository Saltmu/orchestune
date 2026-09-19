from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestune.consistency.invariants.status import primary_status_labels
from orchestune.dag.graph import recompute_dag_for_footprint_change
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_records import (
    _authoritative_execution_active,
    apply_verified_transition,
)
from orchestune.dispatch.dependency_policy import (
    DependencyPolicyView,
    decide_stack_target,
    has_pending_dependencies,
)
from orchestune.dispatch.escalation import apply_human_review_escalation
from orchestune.dispatch.labels import transition_status_label
from orchestune.dispatch.locks import check_footprint_deviation
from orchestune.dispatch.rebase import SubTask, _build_subtasks_for_recompute
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.state import RunState
from orchestune.dispatch.status_repair import VerifiedStatusTransition
from orchestune.infra.git_cli import resolve_local_or_remote_branch, run_git
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord
from orchestune.outcome_record import OutcomeRecord, parse_from_comments
from orchestune.task_metadata import TaskMetadata


def _collect_active_conflict_subtask_ids(
    run_state: RunState,
    ctx: CycleContext,
    subtasks_for_recompute: dict[str, SubTask],
    config: DispatcherConfig,
) -> set[str]:
    """アクティブなワークツリーが持つフットプリントと競合するサブタスクIDの集合を収集する。"""
    active_conflict_subtask_ids = set()
    for active in run_state.active_worktrees.values():
        active_task = ctx.task(active.issue_number)
        if not active_task or not active_task.subtask_id:
            continue

        deviated = check_footprint_deviation(
            active.worktree_path,
            active.declared_footprint,
            base=active.base_branch,
            min_changed_lines=config.deviation_buffer_lines,
        )
        if deviated is None:
            # 検出不能なエラー時は fail-closed とし、自動復帰させない（＝全てのサブタスクが競合中とする）
            for subtask_id in subtasks_for_recompute:
                active_conflict_subtask_ids.add(subtask_id)
            continue
        merged_footprint = tuple(dict.fromkeys([*active.declared_footprint, *deviated]))
        try:
            _, conflicts = recompute_dag_for_footprint_change(
                subtasks_for_recompute,
                active_task.subtask_id,
                updated_footprint=merged_footprint,
                threshold=config.dag_similarity_threshold,
                ignore_patterns=config.dag_ignore_patterns,
            )
            for conflict in conflicts:
                if conflict.blocked_subtask_id:
                    active_conflict_subtask_ids.add(conflict.blocked_subtask_id)
        except Exception:
            # Conflict Graph再計算中の例外発生時も fail-closed とし、
            # 自動復帰させない（＝全てのサブタスクを競合中とする）
            for subtask_id in subtasks_for_recompute:
                active_conflict_subtask_ids.add(subtask_id)
    return active_conflict_subtask_ids


def _live_verify_queued_transition(
    config: DispatcherConfig,
    *,
    issue_number: int,
    before_labels: tuple[str, ...],
) -> VerifiedStatusTransition | None:
    """#883: live-verify a `BLOCKED -> QUEUED` transition just applied directly
    against Forge (no intent journal here, unlike the typed status executor).

    Built fresh from a live re-fetch of the Issue's open state and full label
    set -- never reconstructed from the promotion-event dict this recovery
    already returns for reporting, which must stay decoupled from this
    confirmation path.

    Codex #899 review: unlike the typed status executor (whose own live
    re-fetch is wrapped by `_execute_with_pending_intent`'s `try/except`),
    nothing upstream of this recovery path (`run_post_gc_reconciliation`)
    catches a Forge read failure, so a transient API error here must be
    treated the same as a failed verification -- no receipt -- rather than
    aborting the entire dispatch cycle after the label mutation already
    landed.
    """
    try:
        if config.resolved_forge.get_issue_state(issue_number).upper() != "OPEN":
            return None
        labels = tuple(config.resolved_forge.get_issue_labels(issue_number))
    except Exception:  # noqa: BLE001 - fail-closed: no receipt, cycle continues
        return None
    if primary_status_labels(labels) != (StatusLabel.QUEUED,):
        return None
    return VerifiedStatusTransition(
        issue_number=issue_number,
        before_labels=before_labels,
        verified_labels=labels,
        intent_id=f"recovery-requeue-{issue_number}",
    )


def _confirm_queued_recovery(
    ctx: CycleContext,
    run_state: RunState,
    config: DispatcherConfig,
    *,
    issue_number: int,
    before_labels: tuple[str, ...],
) -> None:
    """#883: apply成功後にlive検証した`VerifiedStatusTransition`を`ctx`へ反映する。

    `QUEUED`は`_EXECUTION_ACTIVE_TARGETS`(cycle_records.py)に含まれないため
    `True`を主張することはないが、`False`を無条件に主張してよいわけではない
    （Codex #899レビュー対応）: このIssueに`run_state.active_worktrees`の
    エントリ（クリーンな単一起動、handle欠如、複数曖昧のいずれか）が残って
    いる場合、`False`は「実行停止済み」という積極的な主張になり、
    `record_transition`がその起動事実を退役させてしまう——このrecovery自体は
    実行停止を検証していない。判定は`_authoritative_execution_active`
    （status executor側と同じ関数）へ委譲し、無条件`False`は使わない。
    """
    receipt = _live_verify_queued_transition(
        config, issue_number=issue_number, before_labels=before_labels
    )
    if receipt is not None:
        apply_verified_transition(
            ctx,
            receipt,
            execution_active=_authoritative_execution_active(
                ctx,
                receipt,
                has_active_entry=lambda number: any(
                    active.issue_number == number
                    for active in run_state.active_worktrees.values()
                ),
            ),
        )


def _handle_blocked_recompute_recovery(
    issues: Any,
    run_state: RunState,
    ctx: CycleContext,
    config: DispatcherConfig,
) -> list[dict]:
    """フットプリント逸脱によるブロック（status:blocked-recompute）の自動復帰（解除）処理を行う。"""
    recompute_resolved_promoted_events: list[dict] = []
    blocked_recompute_issues = [
        issue for issue in issues.all() if StatusLabel.BLOCKED_RECOMPUTE in issue.labels
    ]

    if not blocked_recompute_issues:
        return recompute_resolved_promoted_events

    subtasks_for_recompute = _build_subtasks_for_recompute(
        ctx.dag_inputs(tuple(task.issue_number for task in ctx.tasks()))
    )
    active_conflict_subtask_ids = _collect_active_conflict_subtask_ids(
        run_state, ctx, subtasks_for_recompute, config
    )
    for issue in blocked_recompute_issues:
        task = ctx.task(issue.number)
        if not task or not task.subtask_id:
            continue
        event = _resolve_one_blocked_recompute_issue(
            issue, task, active_conflict_subtask_ids, ctx, run_state, config
        )
        if event is not None:
            recompute_resolved_promoted_events.append(event)

    return recompute_resolved_promoted_events


def _resolve_one_blocked_recompute_issue(
    issue: IssueRecord,
    task: TaskMetadata,
    active_conflict_subtask_ids: set[str],
    ctx: CycleContext,
    run_state: RunState,
    config: DispatcherConfig,
) -> dict | None:
    if task.subtask_id in active_conflict_subtask_ids:
        return None
    if config.apply:
        config.resolved_forge.remove_label(issue.number, StatusLabel.BLOCKED_RECOMPUTE)
    if _has_pending_dependencies(task, ctx):
        return None
    if config.apply:
        before_labels = tuple(
            current.status_labels
            if (current := ctx.task(issue.number)) is not None
            else task.status_labels
        )
        transition_status_label(
            config.resolved_forge,
            issue.number,
            StatusLabel.QUEUED,
            (StatusLabel.BLOCKED,),
        )
        _confirm_queued_recovery(
            ctx,
            run_state,
            config,
            issue_number=issue.number,
            before_labels=before_labels,
        )
    return {"issue_number": issue.number, "subtask_id": task.subtask_id}


@dataclass(frozen=True)
class BaseBranchRedRecoveryDecision:
    issue_number: int
    subtask_id: str
    action: str  # "requeue", "unmark_only", "escalate"
    recorded_base_sha: str | None = None
    current_base_sha: str | None = None
    attempt: int | None = None


def _get_branch_commit_sha(
    branch: str, repository_root: str | Path | None = None
) -> str | None:
    try:
        resolved = resolve_local_or_remote_branch(
            repository_root or ".", branch, prefer_remote=True
        )
        result = run_git(["rev-parse", resolved], cwd=repository_root, check=True)
        return result.stdout.strip() or None
    except Exception:
        return None


def _resolve_base_branch_for_task(
    task: TaskMetadata,
    config: DispatcherConfig,
    view: DependencyPolicyView,
) -> str:
    """Use the common safe stack target, retaining the existing fallback base."""
    decision = decide_stack_target(task.issue_number, view)
    if decision.target is not None:
        return decision.target.branch
    if config.parent_issue_number is not None:
        return f"parent/issue-{config.parent_issue_number}"
    return "origin/main"


def _has_pending_dependencies(
    task: TaskMetadata,
    view: DependencyPolicyView,
) -> bool:
    """Delegate completion waiting to the common assessment-based predicate."""
    return has_pending_dependencies(view.assess_dependencies(task.issue_number))


def _decide_single_base_branch_red_recovery(
    issue: IssueRecord,
    task: TaskMetadata,
    outcome: OutcomeRecord,
    current_base_shas: dict[int, str | None],
    dependencies: DependencyPolicyView,
) -> BaseBranchRedRecoveryDecision | None:
    if outcome.attempt is not None and outcome.attempt >= 3:
        return BaseBranchRedRecoveryDecision(
            issue_number=issue.number,
            subtask_id=task.subtask_id,
            action="escalate",
            attempt=outcome.attempt,
        )
    if not outcome.base_sha:
        return None
    current_sha = current_base_shas.get(issue.number)
    if current_sha is None:
        return None
    has_advanced = (
        current_sha != outcome.base_sha
        and not current_sha.startswith(outcome.base_sha)
        and not outcome.base_sha.startswith(current_sha)
    )
    if not has_advanced:
        return None
    action = (
        "unmark_only" if _has_pending_dependencies(task, dependencies) else "requeue"
    )
    return BaseBranchRedRecoveryDecision(
        issue_number=issue.number,
        subtask_id=task.subtask_id,
        action=action,
        recorded_base_sha=outcome.base_sha,
        current_base_sha=current_sha,
        attempt=outcome.attempt,
    )


def _decide_base_branch_red_recovery(
    base_branch_red_issues: list[IssueRecord],
    tasks_by_issue: Mapping[int, TaskMetadata],
    dependencies: DependencyPolicyView,
    current_base_shas: dict[int, str | None],
    outcomes_by_issue: dict[int, OutcomeRecord | None],
) -> list[BaseBranchRedRecoveryDecision]:
    """#555: ci:base-branch-red を持つタスクの自動復帰・エスカレーション判定を行う（副作用なし）。"""
    decisions: list[BaseBranchRedRecoveryDecision] = []
    for issue in base_branch_red_issues:
        task = tasks_by_issue.get(issue.number)
        if not task or not task.subtask_id:
            continue
        outcome = outcomes_by_issue.get(issue.number)
        if outcome is None:
            continue
        decision = _decide_single_base_branch_red_recovery(
            issue,
            task,
            outcome,
            current_base_shas,
            dependencies,
        )
        if decision is not None:
            decisions.append(decision)
    return decisions


def _apply_base_branch_red_requeue(
    decision: BaseBranchRedRecoveryDecision,
    ctx: CycleContext,
    run_state: RunState,
    config: DispatcherConfig,
    rec_sha: str,
    cur_sha: str,
) -> dict:
    if config.apply:
        current = ctx.task(decision.issue_number)
        before_labels = tuple(current.status_labels) if current is not None else ()
        config.resolved_forge.remove_label(decision.issue_number, "ci:base-branch-red")
        transition_status_label(
            config.resolved_forge,
            decision.issue_number,
            StatusLabel.QUEUED,
            (StatusLabel.BLOCKED,),
        )
        _confirm_queued_recovery(
            ctx,
            run_state,
            config,
            issue_number=decision.issue_number,
            before_labels=before_labels,
        )
        config.resolved_forge.add_comment(
            decision.issue_number,
            f"ベースブランチのコミット前進（{rec_sha} → {cur_sha}）を検知したため、"
            "`ci:base-branch-red`マーカーを解除して再キューイング（`status:queued`）しました。",
        )
    return {
        "issue_number": decision.issue_number,
        "subtask_id": decision.subtask_id,
    }


def _apply_base_branch_red_unmark(
    decision: BaseBranchRedRecoveryDecision,
    config: DispatcherConfig,
    rec_sha: str,
    cur_sha: str,
) -> None:
    if config.apply:
        config.resolved_forge.remove_label(decision.issue_number, "ci:base-branch-red")
        config.resolved_forge.add_comment(
            decision.issue_number,
            f"ベースブランチのコミット前進（{rec_sha} → {cur_sha}）を検知したため、"
            "`ci:base-branch-red`マーカーを解除しました（未解決の依存関係があるため`status:blocked`を維持します）。",
        )


def _apply_base_branch_red_escalate(
    decision: BaseBranchRedRecoveryDecision,
    config: DispatcherConfig,
) -> None:
    if not config.apply:
        return
    apply_human_review_escalation(
        decision.issue_number,
        (StatusLabel.BLOCKED,),
        f"ベースブランチ由来のCI失敗（base-branch-red）が{decision.attempt}回連続で発生したため、"
        "`status:blocked-human-review`へエスカレーションしました。",
        forge=config.resolved_forge,
    )
    try:
        config.resolved_forge.remove_label(decision.issue_number, "ci:base-branch-red")
    except Exception:
        pass


def _apply_single_base_branch_red_decision(
    decision: BaseBranchRedRecoveryDecision,
    ctx: CycleContext,
    run_state: RunState,
    config: DispatcherConfig,
) -> dict | None:
    rec_sha = (decision.recorded_base_sha or "")[:7]
    cur_sha = (decision.current_base_sha or "")[:7]
    if decision.action == "requeue":
        return _apply_base_branch_red_requeue(
            decision, ctx, run_state, config, rec_sha, cur_sha
        )
    if decision.action == "unmark_only":
        _apply_base_branch_red_unmark(decision, config, rec_sha, cur_sha)
        return None
    if decision.action == "escalate":
        _apply_base_branch_red_escalate(decision, config)
    return None


def _apply_base_branch_red_recovery(
    decisions: list[BaseBranchRedRecoveryDecision],
    ctx: CycleContext,
    run_state: RunState,
    config: DispatcherConfig,
) -> list[dict]:
    events: list[dict] = []
    for decision in decisions:
        event = _apply_single_base_branch_red_decision(decision, ctx, run_state, config)
        if event is not None:
            events.append(event)
    return events


def _resolve_recovery_base_sha(
    task: TaskMetadata,
    config: DispatcherConfig,
    dependencies: DependencyPolicyView,
    repo_root: Path | None,
) -> str | None:
    """#860: 未完了依存があるタスクにおいて、依存先がCI未通過等でスタック対象外
    （親/mainへフォールバック）の場合は、前回の依存先ブランチと異なるブランチの
    SHAを比較して誤ったhas_advanced（unmark_only）を招かないよう、Noneとする。
    """
    decision = decide_stack_target(task.issue_number, dependencies)
    if decision.target is not None:
        base_branch = decision.target.branch
    elif decision.reason == "no-stack-dependency":
        base_branch = (
            f"parent/issue-{config.parent_issue_number}"
            if config.parent_issue_number is not None
            else "origin/main"
        )
    else:
        return None
    return _get_branch_commit_sha(base_branch, repo_root)


def _handle_base_branch_red_recovery(
    issues: Any,
    ctx: CycleContext,
    run_state: RunState,
    config: DispatcherConfig,
) -> list[dict]:
    """#555: ci:base-branch-red マーカーを持つタスクのベースコミット前進検知および再キューを行う。"""
    base_branch_red_issues = [
        issue for issue in issues.all() if "ci:base-branch-red" in issue.labels
    ]
    if not base_branch_red_issues:
        return []

    outcomes_by_issue: dict[int, OutcomeRecord | None] = {}
    current_base_shas: dict[int, str | None] = {}
    repo_root = config.worktree_root.parent if config.worktree_root else None
    for issue in base_branch_red_issues:
        try:
            comments = config.resolved_forge.list_comments(issue.number)
            outcome = parse_from_comments(comments)
        except Exception:
            outcome = None
        outcomes_by_issue[issue.number] = outcome

        task = ctx.task(issue.number)
        if task is not None:
            current_base_shas[issue.number] = _resolve_recovery_base_sha(
                task, config, ctx, repo_root
            )

    tasks_by_issue = {task.issue_number: task for task in ctx.tasks()}
    decisions = _decide_base_branch_red_recovery(
        base_branch_red_issues,
        tasks_by_issue,
        ctx,
        current_base_shas,
        outcomes_by_issue,
    )
    return _apply_base_branch_red_recovery(decisions, ctx, run_state, config)
