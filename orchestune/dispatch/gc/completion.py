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
from typing import Literal, NamedTuple

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
from orchestune.dispatch.summary import WARN_PREFIX, ascii_safe
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
    REASON_REVIEW_TIMEOUT,
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
    # PR#789レビュー対応(Codex P2): 判定を保留させたForge呼び出しと、その失敗内容。
    # stderrの警告が失われた後もレポートから原因を特定できるようにする。
    operation: str = ""
    error: str = ""


class _CompletionContext(NamedTuple):
    active: ActiveWorktree
    active_task: Task | None
    config: DispatcherConfig
    dispatch_not_needed_review: NotNeededReviewDispatcher | None
    run_state: RunState | None
    now: float
    open_prs: Sequence[PrRecord] | None
    on_early_death_requeue: Callable[[], None] | None
    on_review_timeout_requeue: Callable[[], None] | None


COMPLETION_HOLD_ACTIONS = frozenset(
    {"completion_skipped_dirty_worktree", "completion_skipped_forge_error"}
)


def is_completion_hold_event(event: Mapping[str, object]) -> bool:
    """Return whether a completion event must be excluded from same-cycle GC."""
    return event.get("action") in COMPLETION_HOLD_ACTIONS


class ForgeFailure(NamedTuple):
    """握り潰したForge呼び出し1件。どの操作がなぜ失敗したのかを対で保つ。

    PR#789レビュー対応(Codex P2): 説明文字列だけを集めると、呼び出し側が
    「どの操作が失敗したのか」を推測で補うことになる（オープンPRのコメント取得が
    失敗しても`list_prs`と報告されていた）。
    """

    operation: str
    description: str


def failed_operations(failures: Sequence[ForgeFailure]) -> str:
    """失敗した呼び出し名を重複なく並べる。空なら空文字。"""
    return ", ".join(dict.fromkeys(failure.operation for failure in failures))


def failure_descriptions(failures: Sequence[ForgeFailure]) -> str:
    """同じ説明は畳む。同一の障害で複数の呼び出しが落ちるのが普通のため。"""
    return "; ".join(dict.fromkeys(failure.description for failure in failures))


def describe_forge_error(error: Exception) -> str:
    """例外を1行へ縮める。原文はUTF-8のレポートに残すためここでは変換しない。"""
    detail = str(error).strip().splitlines()
    return f"{type(error).__name__}: {detail[0]}" if detail else type(error).__name__


def warn_forge_failure(
    operation: str,
    issue_number: int | None,
    error: Exception,
    error_sink: list[ForgeFailure] | None = None,
) -> str:
    """#787: Forge呼び出しの失敗を握り潰す直前に、その事実を必ず表に出す。

    これらの失敗はいずれも`"unknown"`という保守的な判定へ丸められる。無言で
    丸めると、API障害による保留とタスク側の問題が運用者から区別できない。

    stderrへ出す1行はWindows(cp932)のコンソールにも出るため`ascii_safe`を通す。
    例外メッセージは外部由来で非ASCII文字を含みうるが、ここで送出される
    `UnicodeEncodeError`は保守的な保留をサイクルの失敗に化けさせてしまう。
    原文はUTF-8で書かれるレポート側に`error_sink`経由で残す。
    """
    description = describe_forge_error(error)
    subject = f"issue #{issue_number}" if issue_number is not None else "the repository"
    print(
        ascii_safe(
            f"{WARN_PREFIX} forge API call '{operation}' failed for {subject}: "
            f"{description}"
        ),
        file=sys.stderr,
    )
    if error_sink is not None:
        error_sink.append(ForgeFailure(operation, description))
    return description


def _fetch_outcome_for_active(
    active: ActiveWorktree, forge: Forge, error_sink: list[ForgeFailure] | None = None
) -> OutcomeRecord | None | Literal["error"]:
    try:
        comments = list(forge.list_comments(active.issue_number))
    except Exception as error:  # noqa: BLE001 - 判定を保留し、事実だけ表に出す
        warn_forge_failure("list_comments", active.issue_number, error, error_sink)
        return "error"
    had_error = False
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
                except Exception as error:  # noqa: BLE001 - 事実だけ表に出す
                    warn_forge_failure("list_comments", pr.number, error, error_sink)
                    had_error = True
    except Exception as error:  # noqa: BLE001 - 事実だけ表に出す
        warn_forge_failure("list_prs", active.issue_number, error, error_sink)
        had_error = True

    outcome = parse_from_comments(comments, since=active.started_at)
    # PR#789レビュー対応(Codex P2): 後段の取得に失敗した状態で結論が見つからない
    # 場合は保守的に保留する。ここで`None`を返すと、Outcome Recordを一時的な
    # API障害で見落としたまま`completed_without_outcome`（人手レビューへの
    # エスカレーションとrun_stateからの除去）へ進んでしまう。
    # 既に読めたコメントに結論があるならそれを使う（取りこぼしていない）。
    if outcome is None and had_error:
        return "error"
    return outcome


def _decide_action_from_outcome(
    outcome: OutcomeRecord | None,
    has_new_commits: bool,
    review_timeout_retry_count: int = 0,
    max_review_timeout_retries: int = 2,
    review_timeout_retry_pending: bool = False,
) -> str:
    if outcome is None:
        return (
            "completed_without_outcome" if has_new_commits else "completed_no_commits"
        )
    if outcome.result == RESULT_NOT_NEEDED:
        return "not_needed"
    if outcome.result == RESULT_BLOCKED:
        if outcome.reason == REASON_BASE_BRANCH_RED:
            attempt = outcome.attempt if outcome.attempt is not None else 1
            return (
                "escalated_base_branch_red"
                if attempt >= 3
                else "blocked_base_branch_red"
            )
        if outcome.reason == REASON_REVIEW_TIMEOUT:
            is_esc = (
                review_timeout_retry_count >= max_review_timeout_retries - 1
                and not review_timeout_retry_pending
            )
            return "escalated_review_timeout" if is_esc else "blocked_review_timeout"
        return "blocked_unknown_reason"
    if outcome.result == RESULT_DONE:
        return "completed" if has_new_commits else "completed_no_commits"
    return "blocked_unknown_reason"


def _detect_worktree_commits(
    active: ActiveWorktree, repository_root: str | Path | None
) -> tuple[bool, str | None]:
    if active.external_id is not None:
        repo_root = repository_root or Path(active.worktree_path).parent
        sha = remote_branch_commit_sha_if_ahead(
            repo_root, active.branch, active.base_branch
        )
        return sha is not None, sha
    return worktree_has_new_commits(active.worktree_path, active.base_branch), None


def _get_review_timeout_retry_state(
    run_state: RunState | None, issue_number: int
) -> tuple[int, bool]:
    rec = run_state.task_reclaim_counts.get(issue_number) if run_state else None
    if rec is not None:
        return rec.review_timeout_retry_count, rec.review_timeout_retry_pending
    return 0, False


def _decide_completed_worktree_outcome(
    active: ActiveWorktree,
    active_task: Task | None,
    repository_root: str | Path | None = None,
    forge: Forge | None = None,
    run_state: RunState | None = None,
    max_review_timeout_retries: int = 2,
) -> CompletedWorktreeDecision:
    subtask_id = active_task.subtask_id if active_task else ""
    if worktree_has_uncommitted_changes(active.worktree_path):
        return CompletedWorktreeDecision(action="completion_skipped_dirty_worktree")
    has_new_commits, commit_sha = _detect_worktree_commits(active, repository_root)

    outcome = None
    if forge is not None:
        failures: list[ForgeFailure] = []
        outcome_or_err = _fetch_outcome_for_active(active, forge, failures)
        if outcome_or_err == "error":
            if has_new_commits:
                return CompletedWorktreeDecision(
                    action="completion_skipped_forge_error",
                    subtask_id=subtask_id,
                    operation=failed_operations(failures),
                    error=failure_descriptions(failures),
                )
        else:
            outcome = outcome_or_err

    retry_count, retry_pending = _get_review_timeout_retry_state(
        run_state, active.issue_number
    )
    action = _decide_action_from_outcome(
        outcome,
        has_new_commits,
        review_timeout_retry_count=retry_count,
        max_review_timeout_retries=max_review_timeout_retries,
        review_timeout_retry_pending=retry_pending,
    )
    return CompletedWorktreeDecision(
        action=action,
        subtask_id=subtask_id,
        commit_sha=commit_sha if action != "completed_no_commits" else None,
        outcome=outcome,
    )


def _stale_status_labels(active_task: Task | None) -> tuple[str, ...]:
    if active_task is not None:
        return tuple(
            label
            for label in PRIMARY_STATUS_LABELS
            if label in active_task.status_labels
        )
    return (StatusLabel.IN_PROGRESS,)


def _prepare_apply_escalation(
    active: ActiveWorktree,
    config: DispatcherConfig,
    active_task: Task | None = None,
) -> tuple[str, ...] | None:
    if not config.apply:
        return None
    remove_worktree(active.worktree_path)
    return _stale_status_labels(active_task)


def _apply_escalation(
    active: ActiveWorktree,
    config: DispatcherConfig,
    message: str,
    active_task: Task | None = None,
) -> None:
    stale_labels = _prepare_apply_escalation(active, config, active_task)
    if stale_labels is not None:
        apply_human_review_escalation(
            active.issue_number,
            stale_labels,
            message,
            forge=config.resolved_forge,
        )


def _apply_blocked_hold(
    active: ActiveWorktree,
    config: DispatcherConfig,
    active_task: Task | None,
    comment: str,
    extra_label: str | None = None,
) -> None:
    stale_labels = _prepare_apply_escalation(active, config, active_task)
    if stale_labels is None:
        return
    transition_status_label(
        config.resolved_forge,
        active.issue_number,
        StatusLabel.BLOCKED,
        stale_labels,
    )
    if extra_label:
        config.resolved_forge.add_label(active.issue_number, extra_label)
    config.resolved_forge.add_comment(active.issue_number, comment)


def _apply_blocked_base_branch_red(
    ctx: _CompletionContext, decision: CompletedWorktreeDecision
) -> None:
    attempt = decision.outcome.attempt if decision.outcome else None
    attempt_str = f"（試行回数: {attempt}/3）" if attempt is not None else ""
    _apply_blocked_hold(
        ctx.active,
        ctx.config,
        ctx.active_task,
        f"ベースブランチ（`{ctx.active.base_branch}`）由来のCI失敗を検知したため、"
        f"`ci:base-branch-red`マーカーを付与して`status:blocked`で保留しました{attempt_str}。"
        "ベースブランチの前進（新コミット）時に自動で再キューイングされます。",
        extra_label="ci:base-branch-red",
    )


def _apply_escalated_base_branch_red(
    ctx: _CompletionContext, decision: CompletedWorktreeDecision
) -> None:
    attempt = (
        decision.outcome.attempt
        if decision.outcome and decision.outcome.attempt is not None
        else 3
    )
    _apply_escalation(
        ctx.active,
        ctx.config,
        f"ベースブランチ由来のCI失敗（base-branch-red）が{attempt}回連続で発生したため、"
        "自動再キューイングを停止し`status:blocked-human-review`へエスカレーションしました。"
        "ベースブランチの修正およびCI状況を確認の上、必要であれば`status:queued`へ再設定してください。",
        ctx.active_task,
    )
    try:
        ctx.config.resolved_forge.remove_label(
            ctx.active.issue_number, "ci:base-branch-red"
        )
    except Exception:
        pass


def _apply_escalated_review_timeout(
    ctx: _CompletionContext, decision: CompletedWorktreeDecision
) -> None:
    msg = (
        f"AIレビュー待機のタイムアウト（review-timeout）が上限（{ctx.config.max_review_timeout_retries}回）に達したため、"
        "自動再投入を停止し`status:blocked-human-review`へエスカレーションしました。\n\n"
        "**診断導線**: レビューワークフローがスキップまたは失敗している可能性があります。"
        "GitHub Actions の実行履歴（ワークフロー: `claude-code-review.yml` 等）を確認し、"
        "actor（ボット名義）、job conclusion（skipped等）、および認可エラーの有無をご確認ください。"
        "問題解決後、必要であれば`status:queued`へ再設定してください。"
    )
    _apply_escalation(ctx.active, ctx.config, msg, ctx.active_task)


def _apply_blocked_unknown_reason(
    ctx: _CompletionContext, decision: CompletedWorktreeDecision
) -> None:
    reason = decision.outcome.reason if decision.outcome else None
    reason_str = f"`{reason}`" if reason else "未指定"
    _apply_blocked_hold(
        ctx.active,
        ctx.config,
        ctx.active_task,
        f"未知のブロック理由（{reason_str}）を持つOutcome Recordを検知したため、"
        "`status:blocked`で保留しました。"
        "ログやIssueの状況を確認の上、必要であれば`status:queued`へ再設定してください。",
    )


def _apply_no_commits_escalation(ctx: _CompletionContext) -> None:
    _apply_escalation(
        ctx.active,
        ctx.config,
        f"エージェントプロセスの終了を検知しましたが、ベースブランチ(`{ctx.active.base_branch}`)に対する新規コミットが1件も検出できませんでした。"
        "権限拒否やエラーにより実際の作業が行われなかった可能性があるため、自動的な完了・依存タスクの昇格を見送り、"
        "`status:blocked-human-review`に変更しました。ログを確認の上、必要であれば`status:queued`へ再設定してください。",
        ctx.active_task,
    )


def _reserve_backoff_retry(
    run_state: RunState,
    issue_number: int,
    now: float,
    attr_count: str,
    attr_at: str,
    attr_pending: str,
    max_retries: int,
    backoff_seconds: float,
) -> tuple[int, float] | None:
    previous = run_state.task_reclaim_counts.get(issue_number)
    retries = getattr(previous, attr_count, 0) if previous is not None else 0
    pending = getattr(previous, attr_pending, False) if previous is not None else False
    if retries >= max_retries and not pending:
        return None
    retry_count = retries if pending else retries + 1
    retry_at = (
        getattr(previous, attr_at, 0.0)
        if pending and previous is not None
        else now + backoff_seconds * 2 ** (retry_count - 1)
    )
    record = previous or TaskReclaimRecord()
    setattr(record, attr_count, retry_count)
    setattr(record, attr_at, retry_at)
    setattr(record, attr_pending, True)
    run_state.task_reclaim_counts[issue_number] = record
    return retry_count, retry_at


def _publish_requeue(
    active: ActiveWorktree,
    active_task: Task | None,
    config: DispatcherConfig,
    run_state: RunState,
    now: float,
    comment: str,
    open_prs: Sequence[PrRecord] | None = None,
    on_requeue_applied: Callable[[], None] | None = None,
) -> None:
    save_run_state(
        run_state,
        config.run_state_path,
        now=now,
        launch_window_seconds=config.window_seconds,
        open_prs=open_prs,
    )
    remove_worktree(active.worktree_path)
    transition_status_label(
        config.resolved_forge,
        active.issue_number,
        StatusLabel.QUEUED,
        _stale_status_labels(active_task),
        on_label_added=on_requeue_applied,
    )
    config.resolved_forge.add_comment(active.issue_number, comment)


def _apply_backoff_retry(
    active: ActiveWorktree,
    active_task: Task | None,
    config: DispatcherConfig,
    run_state: RunState,
    now: float,
    spec: tuple[str, int, int, float, str, str],
    open_prs: Sequence[PrRecord] | None = None,
    on_requeue: Callable[[], None] | None = None,
) -> dict | None:
    prefix, max_retries, total_allowed, backoff, reason, action = spec
    res = _reserve_backoff_retry(
        run_state,
        active.issue_number,
        now,
        f"{prefix}_count",
        f"{prefix}_at",
        f"{prefix}_pending",
        max_retries,
        backoff,
    )
    if res is None:
        return None
    cnt, at = res
    if config.apply:
        comment = (
            f"{reason}自動再投入します（{cnt}/{total_allowed}回目）。"
            f"次回起動は指数バックオフ後（Unix時刻 {at:.0f} 以降）です。"
        )
        _publish_requeue(
            active, active_task, config, run_state, now, comment, open_prs, on_requeue
        )
    subtask_id = active_task.subtask_id if active_task else ""
    return {
        "action": action,
        "subtask_id": subtask_id,
        "commit_sha": None,
        f"{prefix}_at": at,
    }


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
    if (
        active.external_id is not None
        or active.started_at is None
        or not 0 <= now - active.started_at <= config.early_death_window_seconds
    ):
        return None
    spec = (
        "early_death_retry",
        config.max_early_death_retries,
        config.max_early_death_retries,
        config.early_death_backoff_seconds,
        "起動直後にコミットなしでエージェントプロセスが終了したため、一時的な通信障害として",
        "early_death_requeued",
    )
    return _apply_backoff_retry(
        active, active_task, config, run_state, now, spec, open_prs, on_requeue_applied
    )


def _apply_review_timeout_retry(
    active: ActiveWorktree,
    active_task: Task | None,
    config: DispatcherConfig,
    run_state: RunState,
    now: float,
    open_prs: Sequence[PrRecord] | None = None,
    on_requeue_applied: Callable[[], None] | None = None,
) -> dict | None:
    """AIレビュー待機タイムアウトを指数バックオフ付きで再投入する。"""
    spec = (
        "review_timeout_retry",
        config.max_review_timeout_retries - 1,
        config.max_review_timeout_retries,
        config.review_timeout_backoff_seconds,
        "AIレビュー待機のタイムアウト（review-timeout）を検知したため、",
        "blocked_review_timeout",
    )
    return _apply_backoff_retry(
        active, active_task, config, run_state, now, spec, open_prs, on_requeue_applied
    )


def _apply_without_outcome_escalation(ctx: _CompletionContext) -> None:
    _apply_escalation(
        ctx.active,
        ctx.config,
        "エージェントプロセスの終了とコミットを検知しましたが、完了宣言レコード（orchestune:outcome）が検出できませんでした。"
        "レビューサイクルが未完了または作業途中で終了した可能性があるため、自動的な完了・依存タスクの昇格を見送り、"
        "`status:blocked-human-review`に変更しました。ログを確認の上、必要であれば`status:queued`へ再設定してください。",
        ctx.active_task,
    )


def _apply_token_limit_escalation(
    active: ActiveWorktree, config: DispatcherConfig, usage: Usage
) -> None:
    model_info = f"（モデル: {usage.model}）" if usage.model else ""
    _apply_escalation(
        active,
        config,
        f"サブタスクのトークン消費量が上限（{config.max_tokens_per_task:,} tokens）を超過しました"
        f"{model_info}。\n実消費量: {usage.total_tokens:,} tokens "
        f"(Input: {usage.input_tokens:,}, Output: {usage.output_tokens:,})。\n"
        "タスクの分割粒度やモデルの適性を確認の上、必要であれば`status:queued`へ再設定してください。",
    )


def _apply_done_worktree_cleanup(ctx: _CompletionContext) -> str | None:
    commit_sha = None
    if ctx.active.external_id is None:
        try:
            commit_sha = run_git(
                ["rev-parse", "HEAD"], cwd=ctx.active.worktree_path, check=True
            ).stdout.strip()
        except Exception:
            pass
    remove_worktree(ctx.active.worktree_path)
    transition_status_label(
        ctx.config.resolved_forge,
        ctx.active.issue_number,
        StatusLabel.DONE,
        _stale_status_labels(ctx.active_task),
    )
    return commit_sha


def _dispatch_terminal_or_blocked_action(
    ctx: _CompletionContext, decision: CompletedWorktreeDecision
) -> bool:
    action = decision.action
    if action == "completed_without_outcome":
        _apply_without_outcome_escalation(ctx)
    elif action == "blocked_base_branch_red":
        _apply_blocked_base_branch_red(ctx, decision)
    elif action == "escalated_base_branch_red":
        _apply_escalated_base_branch_red(ctx, decision)
    elif action == "escalated_review_timeout":
        _apply_escalated_review_timeout(ctx, decision)
    elif action == "blocked_unknown_reason":
        _apply_blocked_unknown_reason(ctx, decision)
    else:
        return False
    return True


def _handle_special_retry(
    ctx: _CompletionContext, decision: CompletedWorktreeDecision
) -> dict | None:
    action = decision.action
    if action == "completed_no_commits":
        if ctx.run_state is not None:
            retry = _apply_early_death_retry(
                ctx.active,
                ctx.active_task,
                ctx.config,
                ctx.run_state,
                ctx.now,
                ctx.open_prs,
                ctx.on_early_death_requeue,
            )
            if retry is not None:
                return retry
        _apply_no_commits_escalation(ctx)
        return {"subtask_id": decision.subtask_id, "commit_sha": decision.commit_sha}
    if action == "blocked_review_timeout" and ctx.run_state is not None:
        return _apply_review_timeout_retry(
            ctx.active,
            ctx.active_task,
            ctx.config,
            ctx.run_state,
            ctx.now,
            ctx.open_prs,
            ctx.on_review_timeout_requeue,
        )
    return None


def _apply_special_completed_action(
    ctx: _CompletionContext, decision: CompletedWorktreeDecision
) -> dict | None:
    retry_event = _handle_special_retry(ctx, decision)
    if retry_event is not None:
        return retry_event
    if decision.action == "not_needed":
        return _finalize_not_needed_worktree(
            ctx.active, ctx.active_task, ctx.config, ctx.dispatch_not_needed_review
        )
    if _dispatch_terminal_or_blocked_action(ctx, decision):
        return {"subtask_id": decision.subtask_id, "commit_sha": decision.commit_sha}
    return None


def _check_token_limit_exceeded(
    ctx: _CompletionContext,
    usage: Usage | None,
    decision: CompletedWorktreeDecision,
    event: dict,
) -> bool:
    if (
        ctx.config.max_tokens_per_task is not None
        and isinstance(usage, Usage)
        and usage.total_tokens > ctx.config.max_tokens_per_task
    ):
        _apply_token_limit_escalation(ctx.active, ctx.config, usage)
        event["action"] = "escalated_token_limit_exceeded"
        event["subtask_id"] = decision.subtask_id
        event["commit_sha"] = None
        return True
    return False


def _is_completion_hold(decision: CompletedWorktreeDecision, event: dict) -> bool:
    """同一サイクルでの完了処理を見送る判定か。保留理由をイベントへ書き足す。"""
    if decision.action not in COMPLETION_HOLD_ACTIONS:
        return False
    if decision.operation:
        event["operation"] = decision.operation
    if decision.error:
        event["error"] = decision.error
    return True


def _apply_completed_decision(
    ctx: _CompletionContext, decision: CompletedWorktreeDecision
) -> dict:
    usage = _collect_completed_usage(ctx.active, ctx.config)
    event: dict = {
        "issue_number": ctx.active.issue_number,
        "worktree_path": ctx.active.worktree_path,
        "action": decision.action,
    }
    if isinstance(usage, Usage):
        event["usage"] = dataclasses.asdict(usage)
    if _is_completion_hold(decision, event):
        return event

    special = _apply_special_completed_action(ctx, decision)
    if special is not None:
        if decision.action == "not_needed":
            return special
        event.update(special)
        return event

    if _check_token_limit_exceeded(ctx, usage, decision, event):
        return event

    event["subtask_id"] = decision.subtask_id
    event["commit_sha"] = (
        _apply_done_worktree_cleanup(ctx) or decision.commit_sha
        if ctx.config.apply
        else decision.commit_sha
    )
    return event


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
    on_review_timeout_requeue: Callable[[], None] | None = None,
) -> dict:
    ctx = _CompletionContext(
        active,
        active_task,
        config,
        dispatch_not_needed_review,
        run_state,
        time.time() if now is None else now,
        open_prs,
        on_early_death_requeue,
        on_review_timeout_requeue,
    )
    return _apply_completed_decision(ctx, decision)


def _finalize_completed_worktree(
    active: ActiveWorktree,
    active_task: Task | None,
    config: DispatcherConfig,
    dispatch_not_needed_review: NotNeededReviewDispatcher | None = None,
    run_state: RunState | None = None,
    now: float | None = None,
    open_prs: Sequence[PrRecord] | None = None,
    on_early_death_requeue: Callable[[], None] | None = None,
    on_review_timeout_requeue: Callable[[], None] | None = None,
) -> dict:
    repo_root = config.worktree_root.parent if config.worktree_root else None
    decision = _decide_completed_worktree_outcome(
        active,
        active_task,
        repo_root,
        forge=config.resolved_forge,
        run_state=run_state,
        max_review_timeout_retries=config.max_review_timeout_retries,
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
        on_review_timeout_requeue,
    )


def _collect_completed_usage(
    active: ActiveWorktree, config: DispatcherConfig
) -> Usage | None:
    if config.dispatch_target is None:
        return None
    return config.dispatch_target.collect_usage(_active_dispatch_handle(active))


def _decide_not_needed_dirty_worktree(active: ActiveWorktree) -> bool:
    return worktree_has_uncommitted_changes(active.worktree_path)


def _finalize_not_needed_worktree(
    active: ActiveWorktree,
    active_task: Task | None,
    config: DispatcherConfig,
    dispatch_not_needed_review: NotNeededReviewDispatcher | None = None,
) -> dict:
    subtask_id = active_task.subtask_id if active_task else ""
    event: dict = {
        "issue_number": active.issue_number,
        "subtask_id": subtask_id,
        "worktree_path": active.worktree_path,
    }
    if _decide_not_needed_dirty_worktree(active):
        event["action"] = "completion_skipped_dirty_worktree"
        return event
    if not config.apply:
        event["action"] = "not_needed"
        return event
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
            comment="対応不要（status:not-needed）と判定されたため、Orchestuneが自動的にクローズしました。",
        )
        event["action"] = "not_needed"
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
    ts_str = pr.closed_at if pr.state == "CLOSED" else pr.created_at
    if not ts_str:
        return False
    ts = _parse_github_timestamp(ts_str)
    return ts is not None and ts < math.floor(active.started_at)


def _collect_open_pr_comments(
    active: ActiveWorktree,
    handle: DispatchHandle,
    open_prs: list[PrRecord],
    config: DispatcherConfig,
    error_sink: list[ForgeFailure] | None = None,
) -> tuple[list[dict], bool]:
    all_comments: list[dict] = []
    had_error = False
    targets = {pr.number for pr in open_prs}
    if handle.issue_number is not None:
        targets.add(handle.issue_number)
    for num in sorted(targets):
        try:
            all_comments.extend(config.resolved_forge.list_comments(num))
        except Exception as error:  # noqa: BLE001
            warn_forge_failure("list_comments", num, error, error_sink)
            had_error = True
    return all_comments, had_error


def _eval_open_pr_status(
    active: ActiveWorktree,
    handle: DispatchHandle,
    open_prs: list[PrRecord],
    config: DispatcherConfig,
    error_sink: list[ForgeFailure] | None = None,
) -> str:
    all_comments, had_error = _collect_open_pr_comments(
        active, handle, open_prs, config, error_sink
    )
    outcome = parse_from_comments(all_comments, since=active.started_at)
    if outcome is not None and outcome.result in (
        RESULT_DONE,
        RESULT_NOT_NEEDED,
        RESULT_BLOCKED,
    ):
        return "completed"
    return "unknown" if had_error and outcome is None else "pending"


def _local_pr_completion_status(
    active: ActiveWorktree,
    config: DispatcherConfig,
    error_sink: list[ForgeFailure] | None = None,
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
        return _eval_open_pr_status(active, handle, open_prs, config, error_sink)
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
    error_sink: list[ForgeFailure] | None = None,
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
    previous = run_state.task_reclaim_counts.get(issue_number)
    count = (
        1
        if previous is None
        else previous.count
        if previous.pending
        else previous.count + 1
    )
    run_state.task_reclaim_counts[issue_number] = TaskReclaimRecord(
        count=count, last_reclaimed_at=time.time(), pending=True
    )
    if on_reclaim_reserved is not None:
        try:
            on_reclaim_reserved()
        except Exception:
            if previous is None:
                run_state.task_reclaim_counts.pop(issue_number, None)
            else:
                run_state.task_reclaim_counts[issue_number] = previous
            raise
    return count


def _handle_abandoned_cloud_reclaim(
    active: ActiveWorktree,
    config: DispatcherConfig,
    status_labels: tuple[str, ...],
    reclaim_count: int,
    on_settle: Callable[[], None],
) -> str:
    remove_worktree(active.worktree_path)
    if exceeds_limit(reclaim_count, config.max_task_reclaims):
        msg = (
            "タスクのPRがクローズされたか、Cloudタスクの失敗により回収を行いました。\n"
            f"回収・再投入の累計回数が上限（max_task_reclaims={config.max_task_reclaims}）を超えた"
            f"（今回で{reclaim_count}回目）ため、status:queuedへの再投入を打ち切り、"
            "status:blocked-human-reviewへ遷移しました。\nタスクの実装方針や実行環境を確認してください。"
        )
        apply_human_review_escalation(
            active.issue_number,
            status_labels,
            msg,
            forge=config.resolved_forge,
            on_label_applied=on_settle,
        )
        return "escalated_reclaim_limit_exceeded"
    stale_labels = tuple(
        label for label in PRIMARY_STATUS_LABELS if label in status_labels
    )
    transition_status_label(
        config.resolved_forge,
        active.issue_number,
        StatusLabel.QUEUED,
        stale_labels,
        on_label_added=on_settle,
    )
    try:
        config.resolved_forge.add_comment(
            active.issue_number,
            "タスクのPRがマージされずにクローズされたか、Cloudタスクが終了したため、完了扱いにはせず、"
            f"GCによりタスクを再キューイング（status:queued）しました（回収{reclaim_count}回目 / 上限{config.max_task_reclaims}回）。",
        )
    except Exception as e:
        print(
            f"Warning: requeued issue #{active.issue_number} but failed to post comment: {e}",
            file=sys.stderr,
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
    subtask_id = active_task.subtask_id if active_task else ""
    event = {
        "issue_number": active.issue_number,
        "subtask_id": subtask_id,
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

    event["action"] = _handle_abandoned_cloud_reclaim(
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
