"""Completion and abandonment handling for dispatcher worktrees."""

from __future__ import annotations

import dataclasses
import math
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from orchestune.bounded_limit import exceeds_limit
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.escalation import apply_human_review_escalation
from orchestune.dispatch.gc.git import (
    remote_branch_commit_sha_if_ahead,
    remove_worktree,
    worktree_has_new_commits,
    worktree_has_uncommitted_changes,
)
from orchestune.dispatch.labels import (
    PRIMARY_STATUS_LABELS,
    TERMINAL_ESCALATION_LABELS,
    transition_status_label,
)
from orchestune.dispatch.rules import NotNeededReviewDispatcher
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import (
    ActiveWorktree,
    RunState,
    TaskReclaimRecord,
    save_run_state,
)
from orchestune.dispatch.summary import WARN_PREFIX
from orchestune.dispatch.targets import (
    ClaudeCodeCloudRoutineDispatchTarget,
    DispatchHandle,
)
from orchestune.forge import Forge
from orchestune.infra.git_cli import run_git
from orchestune.infra.process_utils import is_process_alive
from orchestune.labels import StatusLabel
from orchestune.models import PrRecord, Usage
from orchestune.outcome_record import (
    REASON_BASE_BRANCH_RED,
    RESULT_BLOCKED,
    RESULT_DONE,
    RESULT_NOT_NEEDED,
    OutcomeRecord,
    parse_from_comments,
)
from orchestune.pr_link_notice import pr_matches_issue


@dataclass(frozen=True, slots=True)
class CompletedWorktreeDecision:
    action: str
    subtask_id: str = ""
    commit_sha: str | None = None
    outcome: OutcomeRecord | None = None


COMPLETION_HOLD_ACTIONS = frozenset(
    {"completion_skipped_dirty_worktree", "completion_skipped_forge_error"}
)


def is_completion_hold_event(event: Mapping[str, object]) -> bool:
    """Return whether a completion event must be excluded from same-cycle GC."""
    return event.get("action") in COMPLETION_HOLD_ACTIONS


def describe_forge_error(error: Exception) -> str:
    """例外を1行のASCII表現へ縮める。stderrとサイクルレポートの双方で使う。"""
    detail = str(error).strip().splitlines()
    return f"{type(error).__name__}: {detail[0]}" if detail else type(error).__name__


def warn_forge_failure(
    operation: str,
    issue_number: int | None,
    error: Exception,
    error_sink: list[str] | None = None,
) -> str:
    """#787: Forge呼び出しの失敗を握り潰す直前に、その事実を必ず表に出す。

    これらの失敗はいずれも`"unknown"`という保守的な判定へ丸められる。無言で
    丸めると、API障害による保留とタスク側の問題が運用者から区別できない。

    stderrへ出す1行はWindows(cp932)のコンソールにも出るためASCIIで組む。
    `error_sink`を渡すと、同じ説明をサイクルレポート用に集められる。
    """
    description = describe_forge_error(error)
    subject = f"issue #{issue_number}" if issue_number is not None else "the repository"
    print(
        f"{WARN_PREFIX} forge API call '{operation}' failed for {subject}: "
        f"{description}",
        file=sys.stderr,
    )
    if error_sink is not None:
        error_sink.append(description)
    return description


def _fetch_outcome_for_active(
    active: ActiveWorktree, forge: Forge
) -> OutcomeRecord | None | Literal["error"]:
    try:
        comments = list(forge.list_comments(active.issue_number))
    except Exception as error:  # noqa: BLE001 - 判定を保留し、事実だけ表に出す
        warn_forge_failure("list_comments", active.issue_number, error)
        return "error"
    try:
        prs = forge.list_prs(state="all")
        matching_prs = [
            pr
            for pr in prs
            if (
                (active.branch is not None and pr.head_ref == active.branch)
                or pr_matches_issue(pr, active.issue_number)
            )
            and not _is_stale_pr_for_active(pr, active)
        ]
        for pr in matching_prs:
            if pr.number != active.issue_number:
                try:
                    comments.extend(forge.list_comments(pr.number))
                except Exception:
                    pass
    except Exception:
        pass
    return parse_from_comments(comments, since=active.started_at)


def _decide_action_from_outcome(
    outcome: OutcomeRecord | None, has_new_commits: bool
) -> str:
    if outcome is None:
        return (
            "completed_without_outcome" if has_new_commits else "completed_no_commits"
        )
    if outcome.result == RESULT_NOT_NEEDED:
        return "not_needed"
    if outcome.result == RESULT_BLOCKED and outcome.reason == REASON_BASE_BRANCH_RED:
        attempt = outcome.attempt if outcome.attempt is not None else 1
        return (
            "escalated_base_branch_red" if attempt >= 3 else "blocked_base_branch_red"
        )
    if outcome.result == RESULT_DONE:
        return "completed" if has_new_commits else "completed_no_commits"
    return "completed_without_outcome"


def _decide_completed_worktree_outcome(
    active: ActiveWorktree,
    active_task: Task | None,
    repository_root: str | Path | None = None,
    forge: Forge | None = None,
) -> CompletedWorktreeDecision:
    subtask_id = active_task.subtask_id if active_task else ""
    if worktree_has_uncommitted_changes(active.worktree_path):
        return CompletedWorktreeDecision(action="completion_skipped_dirty_worktree")
    if active.external_id is not None:
        repository_root = repository_root or Path(active.worktree_path).parent
        commit_sha = remote_branch_commit_sha_if_ahead(
            repository_root, active.branch, active.base_branch
        )
        has_new_commits = commit_sha is not None
    else:
        has_new_commits = worktree_has_new_commits(
            active.worktree_path, active.base_branch
        )
        commit_sha = None

    if forge is not None:
        outcome_or_err = _fetch_outcome_for_active(active, forge)
        if outcome_or_err == "error":
            if has_new_commits:
                return CompletedWorktreeDecision(
                    action="completion_skipped_forge_error", subtask_id=subtask_id
                )
            outcome = None
        else:
            outcome = outcome_or_err
    else:
        outcome = None

    action = _decide_action_from_outcome(outcome, has_new_commits)
    return CompletedWorktreeDecision(
        action=action,
        subtask_id=subtask_id,
        commit_sha=commit_sha if action != "completed_no_commits" else None,
        outcome=outcome,
    )


def _apply_blocked_base_branch_red(
    active: ActiveWorktree,
    decision: CompletedWorktreeDecision,
    config: DispatcherConfig,
    active_task: Task | None,
) -> None:
    if not config.apply:
        return
    remove_worktree(active.worktree_path)
    stale_labels = (
        tuple(
            label
            for label in PRIMARY_STATUS_LABELS
            if label in active_task.status_labels
        )
        if active_task is not None
        else (StatusLabel.IN_PROGRESS,)
    )
    transition_status_label(
        config.resolved_forge,
        active.issue_number,
        StatusLabel.BLOCKED,
        stale_labels,
    )
    config.resolved_forge.add_label(active.issue_number, "ci:base-branch-red")
    attempt_str = (
        f"（試行回数: {decision.outcome.attempt}/3）"
        if decision.outcome and decision.outcome.attempt is not None
        else ""
    )
    config.resolved_forge.add_comment(
        active.issue_number,
        f"ベースブランチ（`{active.base_branch}`）由来のCI失敗を検知したため、"
        f"`ci:base-branch-red`マーカーを付与して`status:blocked`で保留しました{attempt_str}。"
        "ベースブランチの前進（新コミット）時に自動で再キューイングされます。",
    )


def _apply_escalated_base_branch_red(
    active: ActiveWorktree,
    decision: CompletedWorktreeDecision,
    config: DispatcherConfig,
    active_task: Task | None,
) -> None:
    if not config.apply:
        return
    remove_worktree(active.worktree_path)
    stale_labels = (
        tuple(
            label
            for label in PRIMARY_STATUS_LABELS
            if label in active_task.status_labels
        )
        if active_task is not None
        else (StatusLabel.IN_PROGRESS,)
    )
    attempt = (
        decision.outcome.attempt
        if decision.outcome and decision.outcome.attempt is not None
        else 3
    )
    apply_human_review_escalation(
        active.issue_number,
        stale_labels,
        f"ベースブランチ由来のCI失敗（base-branch-red）が{attempt}回連続で発生したため、"
        "自動再キューイングを停止し`status:blocked-human-review`へエスカレーションしました。"
        "ベースブランチの修正およびCI状況を確認の上、必要であれば`status:queued`へ再設定してください。",
        forge=config.resolved_forge,
    )
    try:
        config.resolved_forge.remove_label(active.issue_number, "ci:base-branch-red")
    except Exception:
        pass


def _apply_no_commits_escalation(
    active: ActiveWorktree, config: DispatcherConfig
) -> None:
    if not config.apply:
        return
    remove_worktree(active.worktree_path)
    apply_human_review_escalation(
        active.issue_number,
        (StatusLabel.IN_PROGRESS,),
        "エージェントプロセスの終了を検知しましたが、ベースブランチ"
        f"(`{active.base_branch}`)に対する新規コミットが1件も検出できませんでした。"
        "権限拒否やエラーにより実際の作業が行われなかった可能性があるため、"
        "自動的な完了・依存タスクの昇格を見送り、`status:blocked-human-review`に"
        "変更しました。ログを確認の上、必要であれば`status:queued`へ再設定してください。",
        forge=config.resolved_forge,
    )


def _reserve_early_death_retry(
    active: ActiveWorktree,
    config: DispatcherConfig,
    run_state: RunState,
    now: float,
) -> tuple[int, float] | None:
    """再投入枠を予約し、回数と次回起動可能時刻を返す。"""
    if (
        active.external_id is not None
        or active.started_at is None
        or not 0 <= now - active.started_at <= config.early_death_window_seconds
    ):
        return None
    previous = run_state.task_reclaim_counts.get(active.issue_number)
    retries = previous.early_death_retry_count if previous is not None else 0
    pending = previous is not None and previous.early_death_retry_pending
    if retries >= config.max_early_death_retries and not pending:
        return None

    retry_count = retries if pending else retries + 1
    retry_at = (
        previous.early_death_retry_at
        if pending and previous is not None
        else now + config.early_death_backoff_seconds * 2 ** (retry_count - 1)
    )
    record = previous or TaskReclaimRecord()
    record.early_death_retry_count = retry_count
    record.early_death_retry_at = retry_at
    record.early_death_retry_pending = True
    run_state.task_reclaim_counts[active.issue_number] = record
    return retry_count, retry_at


def _publish_early_death_requeue(
    active: ActiveWorktree,
    active_task: Task | None,
    config: DispatcherConfig,
    run_state: RunState,
    now: float,
    retry_count: int,
    retry_at: float,
    open_prs: Sequence[PrRecord] | None,
    on_requeue_applied: Callable[[], None] | None,
) -> None:
    """予約を先に保存し、worktree回収とqueued遷移を反映する。"""
    save_run_state(
        run_state,
        config.run_state_path,
        now=now,
        launch_window_seconds=config.window_seconds,
        open_prs=open_prs,
    )
    remove_worktree(active.worktree_path)
    status_labels = (
        active_task.status_labels if active_task else (StatusLabel.IN_PROGRESS,)
    )
    transition_status_label(
        config.resolved_forge,
        active.issue_number,
        StatusLabel.QUEUED,
        tuple(label for label in PRIMARY_STATUS_LABELS if label in status_labels),
        on_label_added=on_requeue_applied,
    )
    config.resolved_forge.add_comment(
        active.issue_number,
        "起動直後にコミットなしでエージェントプロセスが終了したため、一時的な通信障害として"
        f"自動再投入します（{retry_count}/{config.max_early_death_retries}回目）。"
        f"次回起動は指数バックオフ後（Unix時刻 {retry_at:.0f} 以降）です。",
    )


def _apply_early_death_retry(
    active: ActiveWorktree,
    active_task: Task | None,
    config: DispatcherConfig,
    run_state: RunState,
    now: float,
    open_prs: Sequence[PrRecord] | None = None,
    on_requeue_applied: Callable[[], None] | None = None,
) -> dict | None:
    """起動直後・コミットなし終了を指数バックオフ付きで再投入する。"""
    reservation = _reserve_early_death_retry(active, config, run_state, now)
    if reservation is None:
        return None
    retry_count, retry_at = reservation
    if config.apply:
        _publish_early_death_requeue(
            active,
            active_task,
            config,
            run_state,
            now,
            retry_count,
            retry_at,
            open_prs,
            on_requeue_applied,
        )
    return {
        "action": "early_death_requeued",
        "subtask_id": active_task.subtask_id if active_task else "",
        "commit_sha": None,
        "early_death_retry_at": retry_at,
    }


def _apply_without_outcome_escalation(
    active: ActiveWorktree, config: DispatcherConfig
) -> None:
    if not config.apply:
        return
    remove_worktree(active.worktree_path)
    apply_human_review_escalation(
        active.issue_number,
        (StatusLabel.IN_PROGRESS,),
        "エージェントプロセスの終了とコミットを検知しましたが、"
        "完了宣言レコード（orchestune:outcome）が検出できませんでした。"
        "レビューサイクルが未完了または作業途中で終了した可能性があるため、"
        "自動的な完了・依存タスクの昇格を見送り、`status:blocked-human-review`に"
        "変更しました。ログを確認の上、必要であれば`status:queued`へ再設定してください。",
        forge=config.resolved_forge,
    )


def _apply_token_limit_escalation(
    active: ActiveWorktree, config: DispatcherConfig, usage: Usage
) -> None:
    if not config.apply:
        return
    remove_worktree(active.worktree_path)
    model_info = f"（モデル: {usage.model}）" if usage.model else ""
    apply_human_review_escalation(
        active.issue_number,
        (StatusLabel.IN_PROGRESS,),
        f"サブタスクのトークン消費量が上限（{config.max_tokens_per_task:,} tokens）を超過しました"
        f"{model_info}。\n実消費量: {usage.total_tokens:,} tokens "
        f"(Input: {usage.input_tokens:,}, Output: {usage.output_tokens:,})。\n"
        "タスクの分割粒度やモデルの適性を確認の上、必要であれば`status:queued`へ再設定してください。",
        forge=config.resolved_forge,
    )


def _apply_done_worktree_cleanup(
    active: ActiveWorktree,
    config: DispatcherConfig,
    active_task: Task | None,
) -> str | None:
    commit_sha = None
    if active.external_id is None:
        try:
            commit_sha = run_git(
                ["rev-parse", "HEAD"], cwd=active.worktree_path, check=True
            ).stdout.strip()
        except Exception:
            pass
    remove_worktree(active.worktree_path)
    stale_labels = (
        tuple(
            label
            for label in PRIMARY_STATUS_LABELS
            if label in active_task.status_labels
        )
        if active_task is not None
        else (StatusLabel.IN_PROGRESS,)
    )
    transition_status_label(
        config.resolved_forge,
        active.issue_number,
        StatusLabel.DONE,
        stale_labels,
    )
    return commit_sha


def _apply_special_completed_action(
    active: ActiveWorktree,
    decision: CompletedWorktreeDecision,
    config: DispatcherConfig,
    active_task: Task | None,
    dispatch_not_needed_review: NotNeededReviewDispatcher | None,
    run_state: RunState | None,
    now: float,
    open_prs: Sequence[PrRecord] | None,
    on_early_death_requeue: Callable[[], None] | None,
) -> dict | None:
    action = decision.action
    if action == "completed_no_commits":
        if run_state is not None:
            early_death = _apply_early_death_retry(
                active,
                active_task,
                config,
                run_state,
                now,
                open_prs,
                on_early_death_requeue,
            )
            if early_death is not None:
                return early_death
        _apply_no_commits_escalation(active, config)
        return {"subtask_id": decision.subtask_id, "commit_sha": None}
    if action == "completed_without_outcome":
        _apply_without_outcome_escalation(active, config)
        return {"subtask_id": decision.subtask_id, "commit_sha": decision.commit_sha}
    if action == "blocked_base_branch_red":
        _apply_blocked_base_branch_red(active, decision, config, active_task)
        return {"subtask_id": decision.subtask_id, "commit_sha": decision.commit_sha}
    if action == "escalated_base_branch_red":
        _apply_escalated_base_branch_red(active, decision, config, active_task)
        return {"subtask_id": decision.subtask_id, "commit_sha": decision.commit_sha}
    if action == "not_needed":
        return _finalize_not_needed_worktree(
            active, active_task, config, dispatch_not_needed_review
        )
    return None


def _check_token_limit_exceeded(
    active: ActiveWorktree,
    config: DispatcherConfig,
    usage: Usage | None,
    decision: CompletedWorktreeDecision,
    event: dict,
) -> bool:
    if (
        config.max_tokens_per_task is not None
        and isinstance(usage, Usage)
        and usage.total_tokens > config.max_tokens_per_task
    ):
        _apply_token_limit_escalation(active, config, usage)
        event["action"] = "escalated_token_limit_exceeded"
        event["subtask_id"] = decision.subtask_id
        event["commit_sha"] = None
        return True
    return False


def _apply_completed_worktree_outcome(
    active: ActiveWorktree,
    decision: CompletedWorktreeDecision,
    config: DispatcherConfig,
    active_task: Task | None = None,
    dispatch_not_needed_review: NotNeededReviewDispatcher | None = None,
    run_state: RunState | None = None,
    now: float | None = None,
    open_prs: Sequence[PrRecord] | None = None,
    on_early_death_requeue: Callable[[], None] | None = None,
) -> dict:
    usage = _collect_completed_usage(active, config)
    event: dict = {
        "issue_number": active.issue_number,
        "worktree_path": active.worktree_path,
        "action": decision.action,
    }
    if isinstance(usage, Usage):
        event["usage"] = dataclasses.asdict(usage)
    if decision.action in (
        "completion_skipped_dirty_worktree",
        "completion_skipped_forge_error",
    ):
        return event

    special = _apply_special_completed_action(
        active,
        decision,
        config,
        active_task,
        dispatch_not_needed_review,
        run_state,
        time.time() if now is None else now,
        open_prs,
        on_early_death_requeue,
    )
    if special is not None:
        if decision.action == "not_needed":
            return special
        event.update(special)
        return event

    if _check_token_limit_exceeded(active, config, usage, decision, event):
        return event

    commit_sha = decision.commit_sha
    if config.apply:
        resolved_sha = _apply_done_worktree_cleanup(active, config, active_task)
        if resolved_sha is not None:
            commit_sha = resolved_sha
    event["subtask_id"] = decision.subtask_id
    event["commit_sha"] = commit_sha
    return event


def _collect_completed_usage(
    active: ActiveWorktree, config: DispatcherConfig
) -> Usage | None:
    if config.dispatch_target is None:
        return None
    return config.dispatch_target.collect_usage(_active_dispatch_handle(active))


def _finalize_completed_worktree(
    active: ActiveWorktree,
    active_task: Task | None,
    config: DispatcherConfig,
    dispatch_not_needed_review: NotNeededReviewDispatcher | None = None,
    run_state: RunState | None = None,
    now: float | None = None,
    open_prs: Sequence[PrRecord] | None = None,
    on_early_death_requeue: Callable[[], None] | None = None,
) -> dict:
    decision = _decide_completed_worktree_outcome(
        active,
        active_task,
        config.worktree_root.parent if config.worktree_root else None,
        forge=config.resolved_forge,
    )
    return _apply_completed_worktree_outcome(
        active,
        decision,
        config,
        active_task,
        dispatch_not_needed_review,
        run_state,
        now,
        open_prs,
        on_early_death_requeue,
    )


def _decide_not_needed_dirty_worktree(active: ActiveWorktree) -> bool:
    return worktree_has_uncommitted_changes(active.worktree_path)


def _finalize_not_needed_worktree(
    active: ActiveWorktree,
    active_task: Task | None,
    config: DispatcherConfig,
    dispatch_not_needed_review: NotNeededReviewDispatcher | None = None,
) -> dict:
    event: dict = {
        "issue_number": active.issue_number,
        "worktree_path": active.worktree_path,
    }
    if _decide_not_needed_dirty_worktree(active):
        event["action"] = "completion_skipped_dirty_worktree"
        return event
    subtask_id = active_task.subtask_id if active_task else ""
    if config.apply:
        remove_worktree(active.worktree_path)
        config.resolved_forge.remove_label(active.issue_number, StatusLabel.IN_PROGRESS)
        if isinstance(config.dispatch_target, ClaudeCodeCloudRoutineDispatchTarget):
            if dispatch_not_needed_review is None:
                raise RuntimeError("not-needed review dispatcher is not configured")
            dispatch_not_needed_review(active.issue_number, subtask_id, config)
            event["action"] = "not_needed_review_dispatched"
        else:
            config.resolved_forge.close_issue(
                active.issue_number,
                "not planned",
                comment=(
                    "対応不要（status:not-needed）と判定されたため、"
                    "Orchestuneが自動的にクローズしました。"
                ),
            )
            event["action"] = "not_needed"
    else:
        event["action"] = "not_needed"
    event["subtask_id"] = subtask_id
    return event


def _active_dispatch_handle(active: ActiveWorktree) -> DispatchHandle:
    return DispatchHandle(
        pid=active.pid,
        external_id=active.external_id,
        external_url=active.external_url,
        branch_name=active.branch,
        issue_number=active.issue_number,
        started_at=active.started_at,
    )


def _parse_github_timestamp(value: str) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_stale_pr_for_active(pr: PrRecord, active: ActiveWorktree) -> bool:
    if active.started_at is None:
        return False
    started_sec = math.floor(active.started_at)
    if pr.state == "CLOSED":
        if pr.closed_at:
            closed_at = _parse_github_timestamp(pr.closed_at)
            if closed_at is not None and closed_at < started_sec:
                return True
        return False
    if pr.created_at:
        created_at = _parse_github_timestamp(pr.created_at)
        if created_at is not None and created_at < started_sec:
            return True
    return False


def _collect_open_pr_comments(
    active: ActiveWorktree,
    handle: DispatchHandle,
    open_prs: list[PrRecord],
    config: DispatcherConfig,
) -> tuple[list[dict], bool]:
    all_comments: list[dict] = []
    had_error = False
    if handle.issue_number is not None:
        try:
            all_comments.extend(
                config.resolved_forge.list_comments(handle.issue_number)
            )
        except Exception:
            had_error = True
    pr_numbers = {pr.number for pr in open_prs}
    for pr_num in pr_numbers:
        if handle.issue_number is None or pr_num != handle.issue_number:
            try:
                all_comments.extend(config.resolved_forge.list_comments(pr_num))
            except Exception:
                had_error = True
    return all_comments, had_error


def _eval_open_pr_status(
    active: ActiveWorktree,
    handle: DispatchHandle,
    open_prs: list[PrRecord],
    config: DispatcherConfig,
) -> str:
    all_comments, had_error = _collect_open_pr_comments(
        active, handle, open_prs, config
    )
    outcome = parse_from_comments(all_comments, since=active.started_at)
    if outcome is not None and outcome.result in (
        RESULT_DONE,
        RESULT_NOT_NEEDED,
        RESULT_BLOCKED,
    ):
        return "completed"
    if had_error and outcome is None:
        return "unknown"
    return "pending"


def _local_pr_completion_status(
    active: ActiveWorktree,
    config: DispatcherConfig,
    error_sink: list[str] | None = None,
) -> str:
    handle = _active_dispatch_handle(active)
    try:
        candidate_prs = config.resolved_forge.list_prs(state="all")
    except Exception as error:  # noqa: BLE001 - 判定を保留し、事実だけ表に出す
        warn_forge_failure("list_prs", active.issue_number, error, error_sink)
        return "unknown"
    matching_prs = [
        pr
        for pr in candidate_prs
        if (
            (handle.branch_name is not None and pr.head_ref == handle.branch_name)
            or (
                handle.issue_number is not None
                and pr_matches_issue(pr, handle.issue_number)
            )
        )
        and not _is_stale_pr_for_active(pr, active)
    ]
    if any(pr.state == "MERGED" for pr in matching_prs):
        return "completed"
    open_prs = [pr for pr in matching_prs if pr.state == "OPEN"]
    if open_prs:
        return _eval_open_pr_status(active, handle, open_prs, config)
    if any(pr.state == "CLOSED" for pr in matching_prs):
        return "abandoned"
    return "pending"


def _call_is_complete(config: DispatcherConfig, handle: DispatchHandle) -> bool:
    """#315レビュー対応: 旧is_completeシグネチャとの互換性を保つ。"""
    assert config.dispatch_target is not None
    try:
        return config.dispatch_target.is_complete(handle, forge=config.resolved_forge)
    except TypeError:
        # `forge`引数なしの旧dispatch_target実装は、引数なしで再試行する。
        return config.dispatch_target.is_complete(handle)


def _cloud_worktree_completion_status(
    active: ActiveWorktree,
    config: DispatcherConfig,
    error_sink: list[str] | None = None,
) -> str:
    assert config.dispatch_target is not None
    handle = _active_dispatch_handle(active)
    try:
        status = config.dispatch_target.completion_status(
            handle, forge=config.resolved_forge
        )
    except Exception as error:  # noqa: BLE001 - 判定を保留し、事実だけ表に出す
        warn_forge_failure("completion_status", active.issue_number, error, error_sink)
        return "unknown"
    if isinstance(status, str):
        return status
    return "completed" if _call_is_complete(config, handle) else "pending"


def _reserve_cloud_reclaim_record(
    issue_number: int,
    run_state: RunState | None,
    on_reclaim_reserved: Callable[[], None] | None = None,
) -> int:
    if run_state is None:
        return 1
    previous_record = run_state.task_reclaim_counts.get(issue_number)
    if previous_record is None:
        reclaim_count = 1
    elif previous_record.pending:
        reclaim_count = previous_record.count
    else:
        reclaim_count = previous_record.count + 1
    run_state.task_reclaim_counts[issue_number] = TaskReclaimRecord(
        count=reclaim_count, last_reclaimed_at=time.time(), pending=True
    )
    if on_reclaim_reserved is not None:
        try:
            on_reclaim_reserved()
        except Exception:
            if previous_record is None:
                run_state.task_reclaim_counts.pop(issue_number, None)
            else:
                run_state.task_reclaim_counts[issue_number] = previous_record
            raise
    return reclaim_count


def _requeue_abandoned_cloud_worktree(
    active: ActiveWorktree,
    config: DispatcherConfig,
    status_labels: tuple[str, ...],
    reclaim_count: int,
    on_settle_reclaim: Callable[[], None],
) -> None:
    stale_labels = tuple(
        label for label in PRIMARY_STATUS_LABELS if label in status_labels
    )
    transition_status_label(
        config.resolved_forge,
        active.issue_number,
        StatusLabel.QUEUED,
        stale_labels,
        on_label_added=on_settle_reclaim,
    )
    try:
        config.resolved_forge.add_comment(
            active.issue_number,
            "タスクのPRがマージされずにクローズされたか、Cloudタスクが終了したため、完了扱いにはせず、"
            "GCによりタスクを再キューイング（status:queued）しました"
            f"（回収{reclaim_count}回目 / 上限{config.max_task_reclaims}回）。",
        )
    except Exception as e:  # noqa: BLE001 - 通知の失敗で回収をやり直さない
        print(
            f"Warning: requeued issue #{active.issue_number} but failed to post "
            f"abandonment comment: {e}",
            file=sys.stderr,
        )


def _apply_abandoned_cloud_reclaim(
    active: ActiveWorktree,
    config: DispatcherConfig,
    status_labels: tuple[str, ...],
    reclaim_count: int,
    on_settle_reclaim: Callable[[], None],
) -> str:
    remove_worktree(active.worktree_path)
    if exceeds_limit(reclaim_count, config.max_task_reclaims):
        apply_human_review_escalation(
            active.issue_number,
            status_labels,
            "タスクのPRがクローズされたか、Cloudタスクの失敗により回収を行いました。\n"
            f"回収・再投入の累計回数が上限（max_task_reclaims="
            f"{config.max_task_reclaims}）を超えた"
            f"（今回で{reclaim_count}回目）ため、"
            "status:queuedへの再投入を打ち切り、"
            "status:blocked-human-reviewへ遷移しました。\n"
            "タスクの実装方針や実行環境を確認してください。",
            forge=config.resolved_forge,
            on_label_applied=on_settle_reclaim,
        )
        return "escalated_reclaim_limit_exceeded"

    _requeue_abandoned_cloud_worktree(
        active, config, status_labels, reclaim_count, on_settle_reclaim
    )
    return "abandoned_pr_requeued"


def _finalize_abandoned_cloud_worktree(
    active: ActiveWorktree,
    active_task: Task | None,
    config: DispatcherConfig,
    run_state: RunState | None = None,
    on_label_applied: Callable[[], None] | None = None,
    on_reclaim_reserved: Callable[[], None] | None = None,
) -> dict:
    event = {
        "issue_number": active.issue_number,
        "subtask_id": active_task.subtask_id if active_task else "",
        "worktree_path": active.worktree_path,
    }
    if worktree_has_uncommitted_changes(active.worktree_path):
        event["action"] = "completion_skipped_dirty_worktree"
        return event
    if not config.apply:
        event["action"] = "abandoned_pr_requeued"
        return event

    status_labels = (
        active_task.status_labels if active_task else (StatusLabel.IN_PROGRESS,)
    )
    if any(label in status_labels for label in TERMINAL_ESCALATION_LABELS):
        remove_worktree(active.worktree_path)
        config.resolved_forge.add_comment(
            active.issue_number,
            "タスクのPRがマージされずにクローズされたためworktreeを回収しました。"
            "既に人間の確認が必要な状態のため、status:*ラベルは変更していません。",
        )
        event["action"] = "abandoned_pr_requeued"
        return event

    reclaim_count = _reserve_cloud_reclaim_record(
        active.issue_number, run_state, on_reclaim_reserved=on_reclaim_reserved
    )

    def _settle_reclaim() -> None:
        if run_state is not None:
            rec = run_state.task_reclaim_counts.get(active.issue_number)
            if rec is not None:
                rec.pending = False
        if on_label_applied is not None:
            on_label_applied()

    event["action"] = _apply_abandoned_cloud_reclaim(
        active, config, status_labels, reclaim_count, _settle_reclaim
    )
    return event


def _is_worktree_complete(active: ActiveWorktree, config: DispatcherConfig) -> bool:
    if active.external_id is not None:
        assert config.dispatch_target is not None
        handle = _active_dispatch_handle(active)
        status = config.dispatch_target.completion_status(
            handle, forge=config.resolved_forge
        )
        if isinstance(status, str):
            return status == "completed"
        return _call_is_complete(config, handle)
    if active.started_at is None:
        return False
    return not is_process_alive(active.pid)
