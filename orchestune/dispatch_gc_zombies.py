"""Zombie and timeout reclamation for active dispatcher worktrees."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_escalation import apply_human_review_escalation
from orchestune.dispatch_gc_git import (
    backup_wip_commit,
    remove_worktree,
    worktree_has_uncommitted_changes,
)
from orchestune.dispatch_labels import (
    TERMINAL_ESCALATION_LABELS,
    transition_status_label,
)
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree, RunState, TaskReclaimRecord
from orchestune.process_utils import is_process_alive


def _check_zombie_and_timeout(
    active: ActiveWorktree,
    zombie_enabled: bool,
    timeout_limit: int,
    now: float,
) -> tuple[bool, bool, bool]:
    """Return ``(is_zombie, is_timeout, process_alive)``."""
    is_zombie = False
    is_timeout = False
    process_alive = is_process_alive(active.pid)

    if zombie_enabled and not process_alive:
        worktree_exists = os.path.exists(active.worktree_path)
        if worktree_exists and worktree_has_uncommitted_changes(active.worktree_path):
            is_zombie = True
        elif not worktree_exists and active.started_at is None:
            # #383: run_state自己修復で対応PRが見つからず復元されたエントリは
            # started_at=None かつ物理worktreeも存在しないため、通常のゾンビ判定
            # （worktree実在+dirty）にもタイムアウト判定（started_at必須）にも
            # 永久に該当できずクオータを占有し続ける。プロセス不在・worktree不在・
            # 開始時刻不明の三条件が揃った場合はゾンビ相当として回収する。
            is_zombie = True

    if not is_zombie and active.started_at is not None:
        if timeout_limit > 0 and now - active.started_at > timeout_limit:
            is_timeout = True

    return is_zombie, is_timeout, process_alive


@dataclass
class ZombieOrTimeoutReclaim:
    key: str
    active: ActiveWorktree
    subtask_id: str
    reason: str
    is_timeout: bool
    process_alive: bool
    status_labels: tuple[str, ...] = ("status:in-progress",)
    # #512: この回収を含めた累計回収回数（1始まり）と、上限超過の判定結果。
    reclaim_count: int = 1
    escalate: bool = False
    now: float = 0.0


def _decide_zombie_or_timeout_reclaims(
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
    held_worktree_paths: set[str] | None,
    now: float,
) -> list[ZombieOrTimeoutReclaim]:
    """Decide all reclaim candidates without applying side effects."""
    zombie_enabled = getattr(config, "zombie_gc", True)
    timeout_limit = getattr(config, "task_timeout_seconds", 0)
    max_task_reclaims = config.max_task_reclaims
    held_worktree_paths = held_worktree_paths or set()

    if not zombie_enabled and timeout_limit <= 0:
        return []

    reclaims: list[ZombieOrTimeoutReclaim] = []
    for key, active in run_state.active_worktrees.items():
        if active.worktree_path in held_worktree_paths:
            continue
        is_zombie, is_timeout, process_alive = _check_zombie_and_timeout(
            active, zombie_enabled, timeout_limit, now
        )
        if not (is_zombie or is_timeout):
            continue
        active_task = tasks_by_issue.get(active.issue_number)
        # #512: 台帳の累計回数＋今回分が上限を超えるかを判定する。台帳への
        # 書き戻しはapply層の責務（decide層は副作用を持たない）。
        previous_record = run_state.task_reclaim_counts.get(active.issue_number)
        reclaim_count = (previous_record.count if previous_record else 0) + 1
        reclaims.append(
            ZombieOrTimeoutReclaim(
                key=key,
                active=active,
                subtask_id=active_task.subtask_id if active_task else "",
                reason="process disappeared" if is_zombie else "timeout exceeded",
                is_timeout=is_timeout,
                process_alive=process_alive,
                status_labels=(
                    active_task.status_labels
                    if active_task is not None
                    else ("status:in-progress",)
                ),
                reclaim_count=reclaim_count,
                escalate=reclaim_count > max_task_reclaims,
                now=now,
            )
        )
    return reclaims


def _record_reclaim(run_state: RunState, reclaim: ZombieOrTimeoutReclaim) -> None:
    """#512: 回収回数の台帳（`RunState.task_reclaim_counts`）を更新する。

    ディスクへの永続化はサイクル終端の`save_run_state`が担う。回収に伴う
    `active_worktrees`からの削除と同じ書き込みで永続化されるため、両者が
    食い違うことはない（クラッシュ時は「まだ回収していない」状態へ揃って戻る）。
    """
    run_state.task_reclaim_counts[reclaim.active.issue_number] = TaskReclaimRecord(
        count=reclaim.reclaim_count, last_reclaimed_at=reclaim.now
    )


def _apply_zombie_or_timeout_reclaim(
    run_state: RunState,
    reclaim: ZombieOrTimeoutReclaim,
    config: DispatcherConfig,
) -> dict | None:
    """decide層が判定した回収対象に基づき、安全に副作用を適用する。

    worktreeの存在確認は、decide時点のスナップショットを信用せず、副作用を
    実行する直前にこの関数内で再評価する。全回収対象の判定（decide）を先に
    まとめて行ってから1件ずつapplyする都合上、decideからこの関数の実行までの
    間にworktreeの状態（削除・再作成）が変化し得るため、古いスナップショットを
    そのまま使うとバックアップ・削除・orphan worktree残存に関する安全策を
    迂回しかねない。

    WIPバックアップコミットの作成に失敗した場合は、未コミットの作業データ
    消失を防ぐため今回のGC回収処理全体をスキップし、Noneを返す
    （run_stateは変更せず、次サイクルでの再試行に委ねる）。
    """
    active = reclaim.active
    reason = reclaim.reason
    already_escalated = any(
        label in reclaim.status_labels for label in TERMINAL_ESCALATION_LABELS
    )
    # #512: 既に人間の確認待ちなら再投入は起きないため、回収回数にも数えない
    # （数えると、人間が対処して再度キューへ戻した際に、実際の再投入回数より
    # 進んだカウンタで即座に再エスカレーションしてしまう）。
    counted = not already_escalated
    escalating = counted and reclaim.escalate
    if config.apply:
        # #385: タイムアウトかつプロセス生存中の場合、対象プロセスがまだ
        # worktreeへ書き込み中の可能性がある。WIPバックアップより先に停止させ
        # ないと、書き込み途中の不整合なスナップショットやgit操作のロック
        # 競合を招きうる（ゾンビ判定はプロセスが既に停止済みのため無関係）。
        if reclaim.is_timeout and active.pid and reclaim.process_alive:
            try:
                os.kill(active.pid, 9)
            except Exception:
                pass
        worktree_exists = os.path.exists(active.worktree_path)
        if worktree_exists:
            backup_error = backup_wip_commit(
                active.worktree_path, f"WIP: backup by Orchestune GC ({reason})"
            )
            if backup_error is not None:
                config.resolved_forge.add_comment(
                    active.issue_number,
                    f"タスク実行が {reason} のためGCによる回収を試みましたが、WIPバックアップコミットの作成に失敗しました。\n"
                    "未コミットの作業データ消失を防ぐため、今回のGC回収およびworktree削除処理を一時スキップしました。\n"
                    f"エラー詳細:\n```\n{backup_error}\n```",
                )
                return None
        if worktree_exists:
            remove_worktree(active.worktree_path)
        worktree_note = (
            "作業ブランチにWIPコミットを退避した上で、"
            if worktree_exists
            else "物理worktreeが見つからなかったため、"
        )
        if already_escalated:
            # 中断した以前の遷移でstatus:blocked-human-review/
            # status:manual-merge-requiredが既に付与されている場合、
            # status:queuedへ書き換えると人間の確認要求を握りつぶして
            # 自動的に再起動してしまう。物理的な後始末のみ行い、
            # ラベルには一切触れない。
            config.resolved_forge.add_comment(
                active.issue_number,
                f"タスク実行が {reason} のため、GCにより{worktree_note}"
                "後始末しました。既に人間の確認が必要な状態のため、"
                "status:*ラベルは変更していません。",
            )
        elif escalating:
            # #512: 回収・再投入の累計が上限を超えたタスクは、status:queuedへ
            # 戻さずstatus:blocked-human-reviewで停止させる。台帳の更新は
            # GitHub側のラベル遷移より先に行う（ローカル先行）: 逆順だと、
            # ラベル成功後にプロセスが落ちた場合に「GitHub上は確認待ちだが
            # ローカルの回数は上限未満」という非対称が残る。
            _record_reclaim(run_state, reclaim)
            apply_human_review_escalation(
                active.issue_number,
                reclaim.status_labels,
                f"タスク実行が {reason} のため、GCにより{worktree_note}"
                "後始末しました。\n"
                f"回収・再投入の累計回数が上限（max_task_reclaims="
                f"{config.max_task_reclaims}）を超えた"
                f"（今回で{reclaim.reclaim_count}回目）ため、"
                "status:queuedへの再投入を打ち切り、"
                "status:blocked-human-reviewへ遷移しました。\n"
                f"最後の回収理由: {reason}\n"
                "タイムアウト設定やサブタスクの粒度、実行環境を確認してください。",
                forge=config.resolved_forge,
            )
        else:
            # #381レビュー対応(Codex P2): stacked launch等の中断した遷移で
            # status:blockedが取り残されている場合も併せて除去し、
            # status:queuedへ確実に収束させる。
            stale_labels = tuple(
                label
                for label in ("status:in-progress", "status:blocked")
                if label in reclaim.status_labels
            )
            # #512: ラベル遷移より先に回数を記録する（上のescalate分岐と同じ理由）。
            _record_reclaim(run_state, reclaim)
            transition_status_label(
                config.resolved_forge,
                active.issue_number,
                "status:queued",
                stale_labels,
            )
            config.resolved_forge.add_comment(
                active.issue_number,
                f"タスク実行が {reason} のため、GCにより{worktree_note}"
                "タスクを再キューイング（status:queued）しました"
                f"（回収{reclaim.reclaim_count}回目 / 上限"
                f"{config.max_task_reclaims}回）。",
            )
        del run_state.active_worktrees[reclaim.key]

    return {
        "issue_number": active.issue_number,
        "subtask_id": reclaim.subtask_id,
        "action": (
            "escalated_reclaim_limit_exceeded" if escalating else "gc_reclaimed"
        ),
        "reason": reason,
        # 数えない分岐（既に人間の確認待ち）では、台帳が保持している既存の
        # 回数（今回分を含まない値）をそのまま報告する。
        "reclaim_count": (
            reclaim.reclaim_count if counted else reclaim.reclaim_count - 1
        ),
    }


def _collect_zombies_and_timeouts(
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
    held_worktree_paths: set[str] | None = None,
) -> list[dict]:
    """Decide and apply zombie and timeout reclamations."""
    reclaims = _decide_zombie_or_timeout_reclaims(
        run_state, tasks_by_issue, config, held_worktree_paths, time.time()
    )
    events: list[dict] = []
    for reclaim in reclaims:
        event = _apply_zombie_or_timeout_reclaim(run_state, reclaim, config)
        if event is not None:
            events.append(event)
    return events
