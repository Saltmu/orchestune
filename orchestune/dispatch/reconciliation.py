from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestune.dag.graph import recompute_dag_for_footprint_change
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.escalation import apply_human_review_escalation
from orchestune.dispatch.labels import transition_status_label
from orchestune.dispatch.locks import check_footprint_deviation
from orchestune.dispatch.rebase import SubTask, _build_subtasks_for_recompute
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import RunState
from orchestune.infra.git_cli import resolve_local_or_remote_branch, run_git
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord
from orchestune.outcome_record import OutcomeRecord, parse_from_comments


def _collect_active_conflict_subtask_ids(
    run_state: RunState,
    ctx: CycleContext,
    subtasks_for_recompute: dict[str, SubTask],
    config: DispatcherConfig,
) -> set[str]:
    """アクティブなワークツリーが持つフットプリントと競合するサブタスクIDの集合を収集する。"""
    active_conflict_subtask_ids = set()
    for active in run_state.active_worktrees.values():
        active_task = ctx.tasks_by_issue.get(active.issue_number)
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


def _handle_blocked_recompute_recovery(
    issues: Any,
    run_state: RunState,
    ctx: CycleContext,
    completed_subtask_ids: set[str],
    config: DispatcherConfig,
) -> list[dict]:
    """フットプリント逸脱によるブロック（status:blocked-recompute）の自動復帰（解除）処理を行う。"""
    recompute_resolved_promoted_events: list[dict] = []
    blocked_recompute_issues = [
        issue for issue in issues.all() if StatusLabel.BLOCKED_RECOMPUTE in issue.labels
    ]

    if not blocked_recompute_issues:
        return recompute_resolved_promoted_events

    subtasks_for_recompute = _build_subtasks_for_recompute(ctx.tasks_by_issue)
    active_conflict_subtask_ids = _collect_active_conflict_subtask_ids(
        run_state, ctx, subtasks_for_recompute, config
    )

    for issue in blocked_recompute_issues:
        task = ctx.tasks_by_issue.get(issue.number)
        if not task or not task.subtask_id:
            continue

        if task.subtask_id not in active_conflict_subtask_ids:
            if config.apply:
                config.resolved_forge.remove_label(
                    issue.number, StatusLabel.BLOCKED_RECOMPUTE
                )

            done_subtask_ids = ctx.done_subtask_ids | completed_subtask_ids
            has_pending_deps = any(
                dep not in done_subtask_ids for dep in task.depends_on
            )

            if not has_pending_deps:
                if config.apply:
                    transition_status_label(
                        config.resolved_forge,
                        issue.number,
                        StatusLabel.QUEUED,
                        (StatusLabel.BLOCKED,),
                    )
                recompute_resolved_promoted_events.append(
                    {"issue_number": issue.number, "subtask_id": task.subtask_id}
                )

    return recompute_resolved_promoted_events


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
    task: Task,
    config: DispatcherConfig,
    subtask_branch_map: dict[str, str] | None = None,
    done_subtask_ids: set[str] | None = None,
) -> str:
    if task.depends_on and subtask_branch_map and done_subtask_ids is not None:
        unresolved_deps = [
            dep for dep in task.depends_on if dep not in done_subtask_ids
        ]
        if len(unresolved_deps) == 1:
            dep = unresolved_deps[0]
            if dep in subtask_branch_map:
                return subtask_branch_map[dep]
    if config.parent_issue_number is not None:
        return f"parent/issue-{config.parent_issue_number}"
    return "origin/main"


def _decide_base_branch_red_recovery(
    base_branch_red_issues: list[IssueRecord],
    tasks_by_issue: dict[int, Task],
    done_subtask_ids: set[str],
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
        if outcome.attempt is not None and outcome.attempt >= 3:
            decisions.append(
                BaseBranchRedRecoveryDecision(
                    issue_number=issue.number,
                    subtask_id=task.subtask_id,
                    action="escalate",
                    attempt=outcome.attempt,
                )
            )
            continue
        if outcome.base_sha:
            current_sha = current_base_shas.get(issue.number)
            if current_sha is not None:
                has_advanced = (
                    current_sha != outcome.base_sha
                    and not current_sha.startswith(outcome.base_sha)
                    and not outcome.base_sha.startswith(current_sha)
                )
                if has_advanced:
                    has_pending_deps = any(
                        dep not in done_subtask_ids for dep in task.depends_on
                    )
                    action = "unmark_only" if has_pending_deps else "requeue"
                    decisions.append(
                        BaseBranchRedRecoveryDecision(
                            issue_number=issue.number,
                            subtask_id=task.subtask_id,
                            action=action,
                            recorded_base_sha=outcome.base_sha,
                            current_base_sha=current_sha,
                            attempt=outcome.attempt,
                        )
                    )
    return decisions


def _apply_base_branch_red_requeue(
    decision: BaseBranchRedRecoveryDecision,
    config: DispatcherConfig,
    rec_sha: str,
    cur_sha: str,
) -> dict:
    if config.apply:
        config.resolved_forge.remove_label(decision.issue_number, "ci:base-branch-red")
        transition_status_label(
            config.resolved_forge,
            decision.issue_number,
            StatusLabel.QUEUED,
            (StatusLabel.BLOCKED,),
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
    config: DispatcherConfig,
) -> dict | None:
    rec_sha = (decision.recorded_base_sha or "")[:7]
    cur_sha = (decision.current_base_sha or "")[:7]
    if decision.action == "requeue":
        return _apply_base_branch_red_requeue(decision, config, rec_sha, cur_sha)
    if decision.action == "unmark_only":
        _apply_base_branch_red_unmark(decision, config, rec_sha, cur_sha)
        return None
    if decision.action == "escalate":
        _apply_base_branch_red_escalate(decision, config)
    return None


def _apply_base_branch_red_recovery(
    decisions: list[BaseBranchRedRecoveryDecision],
    config: DispatcherConfig,
) -> list[dict]:
    events: list[dict] = []
    for decision in decisions:
        event = _apply_single_base_branch_red_decision(decision, config)
        if event is not None:
            events.append(event)
    return events


def _handle_base_branch_red_recovery(
    issues: Any,
    ctx: CycleContext,
    completed_subtask_ids: set[str],
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
    done_subtask_ids = ctx.done_subtask_ids | completed_subtask_ids

    for issue in base_branch_red_issues:
        try:
            comments = config.resolved_forge.list_comments(issue.number)
            outcome = parse_from_comments(comments)
        except Exception:
            outcome = None
        outcomes_by_issue[issue.number] = outcome

        task = ctx.tasks_by_issue.get(issue.number)
        if task is not None:
            base_branch = _resolve_base_branch_for_task(
                task, config, ctx.subtask_branch_map, done_subtask_ids
            )
            current_base_shas[issue.number] = _get_branch_commit_sha(
                base_branch, repo_root
            )

    decisions = _decide_base_branch_red_recovery(
        base_branch_red_issues,
        ctx.tasks_by_issue,
        done_subtask_ids,
        current_base_shas,
        outcomes_by_issue,
    )
    return _apply_base_branch_red_recovery(decisions, config)
