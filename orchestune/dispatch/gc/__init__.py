"""Active-worktree GC rule-chain orchestration.

Implementation helpers are re-exported for backward-compatible imports.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from typing import Literal

from orchestune.bounded_limit import exceeds_limit
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_records import CompletionReceipt
from orchestune.dispatch.external_execution import (
    hold_if_not_stopped,
    notify_completed_hold,
    send_hold_to_human_review,
)
from orchestune.dispatch.gc.completion import (
    CompletedWorktreeDecision,
    ForgeFailure,
    _active_dispatch_handle,
    _apply_completed_worktree_outcome,
    _call_is_complete,
    _cloud_worktree_completion_status,
    _decide_completed_worktree_outcome,
    _decide_not_needed_dirty_worktree,
    _fetch_outcome_for_active,
    _finalize_abandoned_cloud_worktree,
    _finalize_completed_worktree,
    _finalize_not_needed_worktree,
    _is_stale_pr_for_active,
    _is_worktree_complete,
    _local_pr_completion_status,
    _parse_github_timestamp,
    failed_operations,
    failure_descriptions,
    is_completion_hold_event,
    warn_forge_failure,
)
from orchestune.dispatch.gc.confirmed import collect_confirmed_completion
from orchestune.dispatch.gc.git import (
    VerifiedWorktreeRemovalRequest,
    WorktreeRemovalEvaluation,
    WorktreeRemovalResult,
    backup_wip_commit,
    evaluate_worktree_removal,
    remote_branch_commit_sha_if_ahead,
    remove_verified_worktree,
    remove_worktree,
    worktree_has_new_commits,
    worktree_has_uncommitted_changes,
)
from orchestune.dispatch.gc.outcome_decision import _is_handoff_ready
from orchestune.dispatch.gc.records import (
    _completed_worktree_record as _completed_worktree_record,
)
from orchestune.dispatch.gc.zombies import (
    ZombieOrTimeoutReclaim,
    _apply_zombie_or_timeout_reclaim,
)
from orchestune.dispatch.launch_state import with_launch
from orchestune.dispatch.rules import ActiveWorktreeRuleOutcome, _RuleExecutionContext
from orchestune.infra.process_utils import is_process_alive
from orchestune.labels import StatusLabel
from orchestune.ledger.active_lifecycle import has_completion_reservation
from orchestune.ledger.completion_reservations import (
    completion_handoff_matches_active,
    completion_mutation_blocked_fresh,
)
from orchestune.ledger.escalation import apply_human_review_escalation
from orchestune.ledger.run_state import (
    ActiveWorktree,
    RunState,
    TaskReclaimRecord,
    save_run_state,
)
from orchestune.models import PrRecord
from orchestune.outcome_record import RESULT_NOT_NEEDED, OutcomeLookupState
from orchestune.task_metadata import TaskMetadata

__all__ = [
    "CompletedWorktreeDecision",
    "ZombieOrTimeoutReclaim",
    "_active_dispatch_handle",
    "_apply_completed_worktree_outcome",
    "_apply_zombie_or_timeout_reclaim",
    "_call_is_complete",
    "_cloud_worktree_completion_status",
    "_decide_completed_worktree_outcome",
    "_decide_not_needed_dirty_worktree",
    "_finalize_abandoned_cloud_worktree",
    "_finalize_completed_worktree",
    "_finalize_not_needed_worktree",
    "_is_stale_pr_for_active",
    "_is_worktree_complete",
    "_local_pr_completion_status",
    "_parse_github_timestamp",
    "is_completion_hold_event",
    "backup_wip_commit",
    "evaluate_worktree_removal",
    "is_process_alive",
    "remote_branch_commit_sha_if_ahead",
    "remove_verified_worktree",
    "remove_worktree",
    "worktree_has_new_commits",
    "worktree_has_uncommitted_changes",
    "VerifiedWorktreeRemovalRequest",
    "WorktreeRemovalEvaluation",
    "WorktreeRemovalResult",
]


def _rule_not_needed(
    ctx: _RuleExecutionContext,
    key: str,
    active: ActiveWorktree,
    active_task: TaskMetadata | None,
) -> ActiveWorktreeRuleOutcome | None:
    """#280/#552: status:not-neededラベルまたはoutcome(not-needed)検知による即時完了処理。

    セッションが「対応不要」と判断した場合、コミット・PRを作らないため
    closingIssuesReferences等の完了シグナルが発生せず、`_rule_completed`
    （PID/PR存在ベース）は永遠にマッチしない。ラベルまたはoutcome検知を最優先の
    完了シグナルとして扱い、stale判定より先に評価する。
    """
    if has_completion_reservation(active) and not completion_handoff_matches_active(
        ctx.run_state, active
    ):
        return ActiveWorktreeRuleOutcome(
            completion_event={
                "issue_number": active.core.issue_number,
                "worktree_path": active.core.worktree_path,
                "action": "completion_reserved_hold",
            },
            terminal=True,
        )
    if has_completion_reservation(active):
        return collect_confirmed_completion(
            ctx.run_state, ctx.config, key, active, ctx.record_completion, active_task
        )
    has_not_needed_label = (
        active_task is not None and StatusLabel.NOT_NEEDED in active_task.status_labels
    )
    has_not_needed_outcome = False
    if not has_not_needed_label:
        lookup = _fetch_outcome_for_active(active, ctx.config.resolved_forge)
        has_not_needed_outcome = (
            lookup.state is OutcomeLookupState.FOUND
            and lookup.record is not None
            and lookup.record.result == RESULT_NOT_NEEDED
        )

    if not has_not_needed_label and not has_not_needed_outcome:
        return None
    completion_event = _finalize_not_needed_worktree(
        active, active_task, ctx.config, ctx.not_needed_review_dispatcher
    )
    if completion_event["action"] in ("not_needed", "not_needed_review_dispatched"):
        if ctx.config.apply:
            del ctx.run_state.active_worktrees[key]
    return ActiveWorktreeRuleOutcome(
        completion_event=completion_event,
        terminal=True,
    )


def _rule_stale_entry_hold(
    ctx: _RuleExecutionContext,
    key: str,
    active: ActiveWorktree,
    active_task: TaskMetadata | None,
) -> ActiveWorktreeRuleOutcome | None:
    """Leave cached stale entries untouched until Supervisor-owned GC runs.

    The early reconciliation chain still has to stop completion/rebase rules
    from acting on an entry whose Issue is no longer in progress.  This rule is
    deliberately non-mutating: the typed ``execution.reclaim`` handler owns
    live Forge revalidation and every cleanup side effect later in the cycle.
    """
    del ctx, key, active
    if active_task is None or StatusLabel.IN_PROGRESS in active_task.status_labels:
        return None
    return ActiveWorktreeRuleOutcome(terminal=True)


def _persist_run_state_best_effort(ctx: _RuleExecutionContext, what: str) -> None:
    """run_stateをその場で永続化する（失敗はサイクル終端の保存に委ねて警告のみ）。"""
    try:
        save_run_state(
            ctx.run_state,
            ctx.config.run_state_path,
            launch_window_seconds=ctx.config.window_seconds,
            open_prs=ctx.prs,
        )
    except Exception as e:  # noqa: BLE001 - ベストエフォートの永続化
        print(f"Warning: failed to persist {what}: {e}", file=sys.stderr)


def _update_hold_record(ctx: _RuleExecutionContext, active: ActiveWorktree) -> int:
    """dirty worktreeの保留回数を記録・永続化して返す。"""
    previous = ctx.run_state.task_reclaim_counts.get(active.core.issue_number)
    hold_count = (previous.count if previous else 0) + 1
    ctx.run_state.task_reclaim_counts[active.core.issue_number] = TaskReclaimRecord(
        count=hold_count, last_reclaimed_at=time.time()
    )
    _persist_run_state_best_effort(
        ctx, f"the dirty-worktree hold count for issue #{active.core.issue_number}"
    )
    return hold_count


def _escalate_held_dirty_worktree(
    ctx: _RuleExecutionContext,
    key: str,
    active: ActiveWorktree,
    active_task: TaskMetadata | None,
    hold_count: int,
) -> str:
    """保留上限を超えたdirty worktreeをエスカレーションする。"""
    released = False

    def _release_entry() -> None:
        nonlocal released
        released = True
        ctx.run_state.active_worktrees.pop(key, None)
        _persist_run_state_best_effort(
            ctx, f"the released ledger entry for issue #{active.core.issue_number}"
        )

    status_labels = (
        active_task.status_labels
        if active_task is not None
        else (StatusLabel.IN_PROGRESS,)
    )
    try:
        apply_human_review_escalation(
            active.core.issue_number,
            status_labels,
            "エージェントプロセスの終了を検知しましたが、worktreeに未コミットの変更が"
            "残っているため、完了処理を保留しました。\n"
            f"保留・回収の累計回数が上限（max_task_reclaims="
            f"{ctx.config.max_task_reclaims}）を超えた（今回で{hold_count}回目）ため、"
            "自動処理を打ち切り、status:blocked-human-reviewへ遷移しました。\n"
            "未コミットの作業データを保全するため、worktreeは削除せずに残しています: "
            f"{active.core.worktree_path}",
            forge=ctx.config.resolved_forge,
            on_label_applied=_release_entry,
        )
    except Exception as e:  # noqa: BLE001 - 1タスクの失敗でサイクルを止めない
        print(
            f"Warning: failed to escalate the held dirty worktree of issue "
            f"#{active.core.issue_number}: {e}",
            file=sys.stderr,
        )
        if not released:
            return "completion_skipped_dirty_worktree"
    return "escalated_reclaim_limit_exceeded"


def _apply_dirty_worktree_hold(
    ctx: _RuleExecutionContext,
    key: str,
    active: ActiveWorktree,
    active_task: TaskMetadata | None,
) -> str:
    """#212のdirty worktree保留にも`max_task_reclaims`の上限を効かせる。"""
    if not ctx.config.apply:
        return "completion_skipped_dirty_worktree"
    hold_count = _update_hold_record(ctx, active)
    if not exceeds_limit(hold_count, ctx.config.max_task_reclaims):
        return "completion_skipped_dirty_worktree"
    return _escalate_held_dirty_worktree(ctx, key, active, active_task, hold_count)


# GC completion actions confirmed enough to mint a `CompletionReceipt` (#882).
# Exact membership only: several other actions share the `completed`-prefix
# (`completed_no_commits`, `completed_without_outcome`) without being a
# genuine confirmed completion, and `escalated_token_limit_exceeded` is still
# recorded to history below but must never confirm completion.
_CONFIRMED_COMPLETION_ACTIONS = frozenset({"completed", "already_merged"})


def _persist_and_confirm_completion(
    ctx: _RuleExecutionContext,
    completion_active: ActiveWorktree,
    receipt: CompletionReceipt | None,
) -> bool:
    """#882: save_run_stateの成功を境界に、receiptの消費(ctx.record_completion)を
    続ける。保存前・保存例外時はContextへ一切反映しない——保存に追いつく前の
    完了をConsumerへ見せてしまうと、再起動でrun_state.jsonから消えるはずの完了が
    同一サイクル中だけ他タスクの依存解決を進めてしまう。戻り値は保存が成功した
    かどうかで、呼出側はこれに応じて`ActiveWorktreeRuleOutcome`の確認フィールド
    自体を取り消す（Codex #898レビュー対応: `ctx.record_completion`だけでなく、
    `completed_issue_numbers`へ伝搬する確認フィールドも保存失敗時は立てない）。
    """
    try:
        save_run_state(
            ctx.run_state,
            ctx.config.run_state_path,
            launch_window_seconds=ctx.config.window_seconds,
            open_prs=ctx.prs,
        )
    except Exception as e:  # noqa: BLE001 - 保存失敗はrecordを止めるだけ
        print(
            "Warning: failed to persist the completion of issue "
            f"#{completion_active.core.issue_number}: {e}",
            file=sys.stderr,
        )
        return False
    if receipt is not None:
        # CONFLICT here means the issue fell out of `ctx.tasks_by_issue` scope
        # (closed/reparented/filtered) by completion time — unlike
        # `record_launch`'s CONFLICT (always a genuine bug, since a launch
        # only ever targets a freshly selected in-scope task), there is no
        # in-cycle lifecycle left to update, so this is an expected no-op, not
        # an invariant violation worth raising on. NOOP (already effectively
        # done, e.g. a concurrently confirmed prior merge) is likewise a
        # harmless no-op.
        ctx.record_completion(receipt.issue_number)
    return True


def _record_completed_worktree(
    ctx: _RuleExecutionContext,
    key: str,
    completion_active: ActiveWorktree,
    active_task: TaskMetadata | None,
    completion_event: dict,
) -> ActiveWorktreeRuleOutcome:
    """完了（またはトークン上限超過）で終端したworktreeを完了履歴へ退避する。"""
    action = completion_event["action"]
    receipt = (
        CompletionReceipt(issue_number=completion_active.core.issue_number)
        if action in _CONFIRMED_COMPLETION_ACTIONS
        else None
    )
    if ctx.config.apply:
        ctx.run_state.completed_worktrees.append(
            _completed_worktree_record(completion_active, active_task, completion_event)
        )
        del ctx.run_state.active_worktrees[key]
        _persist_and_confirm_completion(ctx, completion_active, receipt)

    return ActiveWorktreeRuleOutcome(
        completion_event=completion_event,
        terminal=True,
    )


def _cleanup_stale_active_worktree(
    active: ActiveWorktree, reason: str, config: DispatcherConfig
) -> bool:
    """古い帳簿エントリに対応するworktreeのWIPバックアップとクリーンアップを行う。"""
    worktree_exists = os.path.exists(active.core.worktree_path)
    if worktree_exists:
        backup_error = backup_wip_commit(
            active.core.worktree_path,
            "WIP: backup by Orchestune GC (stale active entry)",
        )
        if backup_error is not None:
            config.resolved_forge.add_comment(
                active.core.issue_number,
                "run_stateの古い帳簿エントリを検知しました"
                f"（{reason}）。対象プロセスの後始末を試みましたが、WIP"
                "バックアップコミットの作成に失敗しました。\n"
                "未コミットの作業データ消失を防ぐため、今回の帳簿破棄処理を"
                "一時スキップしました。次サイクルで再試行します。\n"
                f"エラー詳細:\n```\n{backup_error}\n```",
            )
            return False

    if active.launch.pid and is_process_alive(active.launch.pid):
        try:
            os.kill(active.launch.pid, 9)
        except Exception:
            pass

    if worktree_exists:
        remove_worktree(active.core.worktree_path)
    return True


def _apply_stale_active_entry_discard(
    run_state: RunState,
    key: str,
    active: ActiveWorktree,
    reason: str,
    config: DispatcherConfig,
    *,
    status_labels: tuple[str, ...] = (),
    subtask_id: str = "",
    events: list[dict] | None = None,
) -> bool:
    """#382: 帳簿(run_state)を破棄する前に、対応する物理worktree・プロセスの
    状態を確認し、必要な後始末を行う。

    #1154: 外部実行の停止を確認できない場合は台帳・ハンドル・枠を保持し、
    人間確認へ送って`False`を返す（回収成功として扱わない）。
    """
    if completion_mutation_blocked_fresh(
        run_state, active.core.issue_number, config.run_state_path
    ):
        return False
    hold = hold_if_not_stopped(active, config, "stale")
    if hold is not None:
        if config.apply:
            send_hold_to_human_review(hold, status_labels, config)
        if events is not None:
            events.append(hold.event(subtask_id=subtask_id))
        return False
    if not config.apply:
        return True
    if not _cleanup_stale_active_worktree(active, reason, config):
        return False
    del run_state.active_worktrees[key]
    record = run_state.task_reclaim_counts.get(active.core.issue_number)
    if record is not None and record.pending:
        record.pending = False
    return True


def _create_abandonment_callbacks(
    ctx: _RuleExecutionContext, key: str, active: ActiveWorktree
) -> tuple[Callable[[], None], Callable[[], None], Callable[[], bool]]:
    """放棄worktree処理時の永続化・解放コールバック群を生成する。"""
    released = False

    def _release_entry() -> None:
        nonlocal released
        ctx.run_state.active_worktrees.pop(key, None)
        rec = ctx.run_state.task_reclaim_counts.get(active.core.issue_number)
        if rec is not None:
            rec.pending = False
        save_run_state(
            ctx.run_state,
            ctx.config.run_state_path,
            launch_window_seconds=ctx.config.window_seconds,
            open_prs=ctx.prs,
        )
        released = True

    def _reserve_reclaim() -> None:
        save_run_state(
            ctx.run_state,
            ctx.config.run_state_path,
            launch_window_seconds=ctx.config.window_seconds,
            open_prs=ctx.prs,
        )

    def _is_released() -> bool:
        return released

    return _release_entry, _reserve_reclaim, _is_released


def _abandoned_worktree_outcome(
    ctx: _RuleExecutionContext,
    key: str,
    active: ActiveWorktree,
    active_task: TaskMetadata | None,
) -> ActiveWorktreeRuleOutcome:
    release_entry, reserve_reclaim, is_released = _create_abandonment_callbacks(
        ctx, key, active
    )
    try:
        completion_event = _finalize_abandoned_cloud_worktree(
            active,
            active_task,
            ctx.config,
            ctx.run_state,
            on_label_applied=release_entry,
            on_reclaim_reserved=reserve_reclaim,
        )
    except Exception as e:
        print(
            f"Warning: skipping abandonment of issue #{active.core.issue_number}: "
            f"failed to persist the reclaim count: {e}",
            file=sys.stderr,
        )
        return ActiveWorktreeRuleOutcome(
            completion_event={
                "issue_number": active.core.issue_number,
                "subtask_id": active_task.subtask_id if active_task else "",
                "worktree_path": active.core.worktree_path,
                "action": "abandonment_skipped_persistence_failure",
            },
            terminal=True,
        )

    if (
        completion_event["action"]
        in ("abandoned_pr_requeued", "escalated_reclaim_limit_exceeded")
        and ctx.config.apply
        and not is_released()
    ):
        ctx.run_state.active_worktrees.pop(key, None)
        _persist_run_state_best_effort(
            ctx, f"the released ledger entry for issue #{active.core.issue_number}"
        )
    return ActiveWorktreeRuleOutcome(completion_event=completion_event, terminal=True)


def _find_recovery_pr(
    active: ActiveWorktree, config: DispatcherConfig
) -> PrRecord | None:
    all_prs = config.resolved_forge.list_prs(state="all")
    matching_prs = [
        pr for pr in all_prs if active.core.issue_number in pr.closes_issue_numbers
    ]
    return next(
        (pr for pr in matching_prs if pr.state.upper() in {"OPEN", "MERGED"}),
        matching_prs[0] if matching_prs else None,
    )


@dataclass(frozen=True, slots=True)
class CompletionResolution:
    """Explicit result of probing whether one active worktree can be completed."""

    state: Literal["pending", "ready", "resolved"]
    completion_active: ActiveWorktree | None = None
    rule_outcome: ActiveWorktreeRuleOutcome | None = None

    def __post_init__(self) -> None:
        valid = (
            (
                self.state == "pending"
                and self.completion_active is None
                and self.rule_outcome is None
            )
            or (
                self.state == "ready"
                and self.completion_active is not None
                and self.rule_outcome is None
            )
            or (
                self.state == "resolved"
                and self.completion_active is None
                and self.rule_outcome is not None
            )
        )
        if not valid:
            raise ValueError(f"invalid completion resolution: {self.state}")

    @classmethod
    def pending(cls) -> CompletionResolution:
        return cls(state="pending")

    @classmethod
    def ready(cls, active: ActiveWorktree) -> CompletionResolution:
        return cls(state="ready", completion_active=active)

    @classmethod
    def resolved(cls, outcome: ActiveWorktreeRuleOutcome) -> CompletionResolution:
        return cls(state="resolved", rule_outcome=outcome)


def _completion_forge_error_hold(
    active: ActiveWorktree, operation: str = "", error: str = ""
) -> ActiveWorktreeRuleOutcome:
    """Build a non-mutating, same-cycle GC hold for an indeterminate completion.

    #787: どのForge呼び出しがなぜ失敗して保留になったのかをイベントへ載せ、
    サイクルレポートの警告セクションから辿れるようにする。
    """
    event: dict[str, object] = {
        "issue_number": active.core.issue_number,
        "worktree_path": active.core.worktree_path,
        "action": "completion_skipped_forge_error",
    }
    if operation:
        event["operation"] = operation
    if error:
        event["error"] = error
    return ActiveWorktreeRuleOutcome(completion_event=event, terminal=True)


def _resolve_recovered_completion(
    ctx: _RuleExecutionContext,
    key: str,
    active: ActiveWorktree,
    active_task: TaskMetadata | None,
) -> CompletionResolution:
    """run_stateに起動時刻もexternal idも無い項目を、PRから復元して解決する。"""
    try:
        recovery_pr = _find_recovery_pr(active, ctx.config)
    except Exception as error:  # noqa: BLE001 - 判定を保留し、事実だけ表に出す
        return CompletionResolution.resolved(
            _completion_forge_error_hold(
                active,
                "find_recovery_pr",
                warn_forge_failure("find_recovery_pr", active.core.issue_number, error),
            )
        )
    if recovery_pr is None:
        return CompletionResolution.pending()
    if recovery_pr.state.upper() == "CLOSED":
        return CompletionResolution.resolved(
            _abandoned_worktree_outcome(ctx, key, active, active_task)
        )
    return CompletionResolution.ready(
        with_launch(
            active.with_core(replace(active.core, branch=recovery_pr.head_ref)),
            replace(
                active.launch,
                external_id=f"recovered-pr:{recovery_pr.number}",
                external_url=f"PR#{recovery_pr.number}",
            ),
        )
    )


def _resolve_cloud_completion(
    ctx: _RuleExecutionContext,
    key: str,
    active: ActiveWorktree,
    active_task: TaskMetadata | None,
) -> CompletionResolution:
    failures: list[ForgeFailure] = []
    status = _cloud_worktree_completion_status(active, ctx.config, failures)
    if status in ("abandoned", "completed"):
        # #1154: PR/成果物の完了判定は外部実行の停止証拠ではない。
        # 停止未確認なら台帳・ハンドル・枠を保持する（完了予約は壊さない）。
        hold = hold_if_not_stopped(active, ctx.config, "completion")
        if hold is not None:
            if ctx.config.apply and status == "completed":
                notify_completed_hold(hold, ctx.config)
            elif ctx.config.apply and active_task is not None:
                send_hold_to_human_review(
                    hold, tuple(active_task.status_labels), ctx.config
                )
            return CompletionResolution.resolved(
                ActiveWorktreeRuleOutcome(
                    completion_event=hold.event(
                        subtask_id=active_task.subtask_id if active_task else ""
                    ),
                    terminal=True,
                )
            )
    if status == "abandoned":
        return CompletionResolution.resolved(
            _abandoned_worktree_outcome(ctx, key, active, active_task)
        )
    if status == "unknown":
        return CompletionResolution.resolved(
            _completion_forge_error_hold(
                active,
                failed_operations(failures) or "completion_status",
                failure_descriptions(failures),
            )
        )
    if status != "completed":
        return CompletionResolution.pending()
    return CompletionResolution.ready(active)


def _resolve_local_completion(
    ctx: _RuleExecutionContext,
    key: str,
    active: ActiveWorktree,
    active_task: TaskMetadata | None,
) -> CompletionResolution:
    if not _is_worktree_complete(active, ctx.config):
        return CompletionResolution.pending()
    failures: list[ForgeFailure] = []
    status = _local_pr_completion_status(active, ctx.config, failures)
    if status == "abandoned":
        return CompletionResolution.resolved(
            _abandoned_worktree_outcome(ctx, key, active, active_task)
        )
    if status == "unknown":
        return CompletionResolution.resolved(
            _completion_forge_error_hold(
                active,
                # PR#789レビュー対応(Codex P2): 実際に失敗した呼び出しを名乗る。
                # オープンPRのコメント取得が失敗しても`list_prs`と報告していた。
                failed_operations(failures) or "list_prs",
                failure_descriptions(failures),
            )
        )
    return CompletionResolution.ready(active)


def _resolve_completion(
    ctx: _RuleExecutionContext,
    key: str,
    active: ActiveWorktree,
    active_task: TaskMetadata | None,
) -> CompletionResolution:
    """完了候補・保留・早期終端を明示的な値として解決する。"""
    core = active.core
    claim = active.claim
    launch = active.launch
    if ctx.completion_reserved(core.issue_number):
        return CompletionResolution.pending()
    is_handoff_ready = _is_handoff_ready(active) and ctx.handoff_matches(active)
    if has_completion_reservation(active) and not is_handoff_ready:
        return CompletionResolution.pending()
    if is_handoff_ready:
        return CompletionResolution.ready(active)
    if claim.owner_kind == "interactive":
        return CompletionResolution.pending()
    if launch.started_at is None and launch.external_id is None:
        return _resolve_recovered_completion(ctx, key, active, active_task)
    if launch.external_id is not None:
        return _resolve_cloud_completion(ctx, key, active, active_task)
    return _resolve_local_completion(ctx, key, active, active_task)


def _handle_completed_event_outcome(
    ctx: _RuleExecutionContext,
    key: str,
    completion_active: ActiveWorktree,
    active_task: TaskMetadata | None,
    completion_event: dict,
) -> ActiveWorktreeRuleOutcome | None:
    """完了イベントのアクションに応じてクリーンアップまたは履歴保存を行う。"""
    action = completion_event["action"]
    if action in (
        "completion_skipped_forge_error",
        "completion_skipped_prior_merge_indeterminate",
    ):
        return ActiveWorktreeRuleOutcome(
            completion_event=completion_event, terminal=True
        )
    if action in ("completed", "already_merged", "escalated_token_limit_exceeded"):
        return _record_completed_worktree(
            ctx, key, completion_active, active_task, completion_event
        )
    if action in (
        "completed_no_commits",
        "early_death_requeued",
        "completed_without_outcome",
        "not_needed",
        "not_needed_review_dispatched",
        "blocked_base_branch_red",
        "escalated_base_branch_red",
        "blocked_review_timeout",
        "escalated_review_timeout",
        "blocked_unknown_reason",
    ):
        if ctx.config.apply:
            ctx.run_state.active_worktrees.pop(key, None)
    elif action == "completion_skipped_dirty_worktree":
        completion_event["action"] = _apply_dirty_worktree_hold(
            ctx, key, completion_active, active_task
        )
    return ActiveWorktreeRuleOutcome(completion_event=completion_event, terminal=True)


def _rule_completed(
    ctx: _RuleExecutionContext,
    key: str,
    active: ActiveWorktree,
    active_task: TaskMetadata | None,
) -> ActiveWorktreeRuleOutcome | None:
    if has_completion_reservation(active):
        return collect_confirmed_completion(
            ctx.run_state, ctx.config, key, active, ctx.record_completion, active_task
        )
    resolution = _resolve_completion(ctx, key, active, active_task)
    if resolution.state == "pending":
        return None
    if resolution.state == "resolved":
        return resolution.rule_outcome
    completion_active = resolution.completion_active
    assert completion_active is not None

    completion_event = _finalize_completed_worktree(
        completion_active,
        active_task,
        ctx.config,
        dispatch_not_needed_review=ctx.not_needed_review_dispatcher,
        run_state=ctx.run_state,
        now=time.time(),
        open_prs=ctx.prs,
        on_early_death_requeue=partial(
            _settle_completion_requeue,
            ctx.run_state,
            ctx.config,
            key,
            active.core.issue_number,
            "early_death_retry_pending",
            ctx.prs,
        ),
        on_review_timeout_requeue=partial(
            _settle_completion_requeue,
            ctx.run_state,
            ctx.config,
            key,
            active.core.issue_number,
            "review_timeout_retry_pending",
            ctx.prs,
        ),
        issue=ctx.issue_records_by_number.get(active.core.issue_number),
    )
    return _handle_completed_event_outcome(
        ctx, key, completion_active, active_task, completion_event
    )


def _settle_completion_requeue(
    state: RunState,
    config: DispatcherConfig,
    key: str,
    issue: int,
    pending_field: str,
    prs: tuple[PrRecord, ...],
) -> None:
    state.active_worktrees.pop(key, None)
    record = state.task_reclaim_counts.get(issue)
    if record is not None:
        setattr(record, pending_field, False)
    save_run_state(
        state,
        config.run_state_path,
        launch_window_seconds=config.window_seconds,
        open_prs=prs,
    )
