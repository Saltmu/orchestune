"""Zombie and timeout reclamation for active dispatcher worktrees."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from orchestune.bounded_limit import exceeds_limit
from orchestune.consistency.invariants.execution import (
    EXECUTION_TIMED_OUT,
    HANDLELESS_EXECUTION_ORPHAN,
    LOCAL_PROCESS_DEAD,
    RUN_STATE_STALE,
)
from orchestune.consistency.models import RepairCommand, RepairResult, RepairStatus
from orchestune.consistency.repairs.execution import COMMAND_RECLAIM
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.escalation import apply_human_review_escalation
from orchestune.dispatch.execution_repair import (
    command_finding_codes,
    evaluate_execution_repair_plan,
)
from orchestune.dispatch.gc.git import (
    backup_wip_commit,
    remove_worktree,
)
from orchestune.dispatch.labels import (
    TERMINAL_ESCALATION_LABELS,
    transition_status_label,
)
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import (
    ActiveWorktree,
    RunState,
    TaskReclaimRecord,
    save_run_state,
)
from orchestune.infra.process_utils import is_process_alive
from orchestune.labels import StatusLabel
from orchestune.models import PrRecord


@dataclass
class ZombieOrTimeoutReclaim:
    key: str
    active: ActiveWorktree
    subtask_id: str
    reason: str
    is_timeout: bool
    process_alive: bool
    status_labels: tuple[str, ...] = (StatusLabel.IN_PROGRESS,)
    # #512: この回収を含めた累計回収回数（1始まり）と、上限超過の判定結果。
    reclaim_count: int = 1
    escalate: bool = False
    now: float = 0.0
    finding_codes: tuple[str, ...] = ()


def _resolve_reclaim_count(run_state: RunState, issue_number: int) -> int:
    """台帳から累計回収回数（または予約分）を解決する。"""
    previous_record = run_state.task_reclaim_counts.get(issue_number)
    if previous_record is None:
        return 1
    if previous_record.pending:
        return previous_record.count
    return previous_record.count + 1


def _build_reclaim_candidate(
    key: str,
    active: ActiveWorktree,
    active_task: Task | None,
    finding_codes: tuple[str, ...],
    process_alive: bool,
    reclaim_count: int,
    max_task_reclaims: int,
    now: float,
) -> ZombieOrTimeoutReclaim:
    """判定結果からZombieOrTimeoutReclaimインスタンスを構築する。"""
    is_timeout = EXECUTION_TIMED_OUT in finding_codes
    if is_timeout:
        reason = "timeout exceeded"
    elif LOCAL_PROCESS_DEAD in finding_codes:
        reason = "process disappeared"
    elif HANDLELESS_EXECUTION_ORPHAN in finding_codes:
        reason = "process disappeared"
    else:
        raise ValueError(f"unsupported reclaim findings: {finding_codes!r}")
    return ZombieOrTimeoutReclaim(
        key=key,
        active=active,
        subtask_id=active_task.subtask_id if active_task else "",
        reason=reason,
        is_timeout=is_timeout,
        process_alive=process_alive,
        status_labels=(
            active_task.status_labels
            if active_task is not None
            else (StatusLabel.IN_PROGRESS,)
        ),
        reclaim_count=reclaim_count,
        escalate=exceeds_limit(reclaim_count, max_task_reclaims),
        now=now,
        finding_codes=finding_codes,
    )


def _reclaim_candidate_from_command(
    command: RepairCommand,
    active_by_subject: dict[str, tuple[str, ActiveWorktree]],
    tasks_by_issue: dict[int, Task],
    run_state: RunState,
    max_task_reclaims: int,
    now: float,
) -> ZombieOrTimeoutReclaim | None:
    if command.code != COMMAND_RECLAIM or command.subject_id is None:
        return None
    resolved = active_by_subject.get(command.subject_id)
    if resolved is None:
        return None
    key, active = resolved
    finding_codes = command_finding_codes(command)
    reclaim_codes = {
        EXECUTION_TIMED_OUT,
        HANDLELESS_EXECUTION_ORPHAN,
        LOCAL_PROCESS_DEAD,
    }
    if RUN_STATE_STALE in finding_codes or not reclaim_codes.intersection(
        finding_codes
    ):
        return None
    return _build_reclaim_candidate(
        key=key,
        active=active,
        active_task=tasks_by_issue.get(active.issue_number),
        finding_codes=finding_codes,
        process_alive=(
            active.pid is not None and LOCAL_PROCESS_DEAD not in finding_codes
        ),
        reclaim_count=_resolve_reclaim_count(run_state, active.issue_number),
        max_task_reclaims=max_task_reclaims,
        now=now,
    )


def _decide_zombie_or_timeout_reclaims(
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
    held_worktree_paths: set[str] | None,
    now: float,
    open_prs: Sequence[PrRecord] | None = None,
) -> list[ZombieOrTimeoutReclaim]:
    """Build reclaim candidates exclusively from kernel repair commands."""
    zombie_enabled = getattr(config, "zombie_gc", True)
    timeout_limit = getattr(config, "task_timeout_seconds", 0)
    max_task_reclaims = config.max_task_reclaims
    held_worktree_paths = held_worktree_paths or set()

    if not zombie_enabled and timeout_limit <= 0:
        return []

    active_by_subject = {
        str(active.issue_number): (key, active)
        for key, active in run_state.active_worktrees.items()
    }
    held_issue_numbers = {
        active.issue_number
        for active in run_state.active_worktrees.values()
        if active.worktree_path in held_worktree_paths
    }
    evaluation = evaluate_execution_repair_plan(
        run_state,
        tasks_by_issue,
        config,
        open_prs=open_prs or (),
        held_issue_numbers=held_issue_numbers,
        now=now,
    )
    candidates = (
        _reclaim_candidate_from_command(
            command,
            active_by_subject,
            tasks_by_issue,
            run_state,
            max_task_reclaims,
            now,
        )
        for command in evaluation.commands
    )
    return [candidate for candidate in candidates if candidate is not None]


def _record_reclaim(
    run_state: RunState,
    reclaim: ZombieOrTimeoutReclaim,
    config: DispatcherConfig,
    open_prs: Sequence[PrRecord] | None,
) -> bool:
    """#512: 回収回数の台帳（`RunState.task_reclaim_counts`）を更新し永続化する。

    PR#520レビュー2巡目対応(Codex P2): 「使う前に予約する」順序で、GitHub側の
    ラベル遷移より**先**にディスクへ書く。サイクル終端の`save_run_state`任せに
    すると、`status:queued`の露出後・保存前に落ちた場合（後続のforge呼び出しの
    例外を含む）、`run_state.json`には古い回数と古いactiveエントリだけが残る。
    次サイクルでは`_rule_stale_entry`がそのactiveエントリを破棄して再起動を許す
    ため、回収が1回も数えられないまま再投入が繰り返され、断続的な障害下では
    上限を超えて起動し続けてしまう。

    永続化に失敗した場合は`False`を返し、呼び出し側は今回の回収自体を見送る
    （fail-closed。ラベルだけ`status:queued`にして回数を失うより、次サイクルで
    回収をやり直す方が安全）。逆に、書き込み成功直後・ラベル遷移前に落ちた場合は
    「回数は数えられているがIssueはstatus:in-progressのまま」となり、次サイクルで
    同じタスクが再度タイムアウト回収される（上限に対して厳しくなる方向の非対称）。

    刈り込みパラメータはサイクル終端の保存と揃える（`launch_window_seconds`を
    渡さないと既定の24時間窓で起動履歴を削り、`open_prs`を渡さないと重複起動
    判定に必要な完了履歴の保護が外れる）。
    """
    previous = run_state.task_reclaim_counts.get(reclaim.active.issue_number)
    run_state.task_reclaim_counts[reclaim.active.issue_number] = TaskReclaimRecord(
        count=reclaim.reclaim_count, last_reclaimed_at=reclaim.now, pending=True
    )
    try:
        save_run_state(
            run_state,
            config.run_state_path,
            now=reclaim.now,
            launch_window_seconds=config.window_seconds,
            open_prs=open_prs,
        )
    except Exception as e:  # noqa: BLE001 - 保存失敗時は回収を見送る
        if previous is None:
            run_state.task_reclaim_counts.pop(reclaim.active.issue_number, None)
        else:
            run_state.task_reclaim_counts[reclaim.active.issue_number] = previous
        print(
            f"Warning: skipping GC reclaim of issue #{reclaim.active.issue_number}: "
            f"failed to persist the reclaim count to {config.run_state_path}: {e}",
            file=sys.stderr,
        )
        return False
    return True


def _reclaim_event(reclaim: ZombieOrTimeoutReclaim, action: str, counted: bool) -> dict:
    """回収結果イベントを組み立てる（`_apply_zombie_or_timeout_reclaim`の戻り値）。"""
    return {
        "issue_number": reclaim.active.issue_number,
        "subtask_id": reclaim.subtask_id,
        "action": action,
        "reason": reclaim.reason,
        # 数えない分岐（既に人間の確認待ち）では、台帳が保持している既存の
        # 回数（今回分を含まない値）をそのまま報告する。
        "reclaim_count": (
            reclaim.reclaim_count if counted else reclaim.reclaim_count - 1
        ),
    }


def _escalate_backup_failure(
    run_state: RunState,
    reclaim: ZombieOrTimeoutReclaim,
    config: DispatcherConfig,
    backup_error: str,
    open_prs: Sequence[PrRecord] | None,
) -> dict | None:
    released = False

    def _release_entry() -> None:
        nonlocal released
        released = True
        _settle_reclaim(run_state, reclaim, config, open_prs, release_entry=True)

    try:
        apply_human_review_escalation(
            reclaim.active.issue_number,
            reclaim.status_labels,
            f"タスク実行が {reclaim.reason} のためGCによる回収を試みましたが、"
            "WIPバックアップコミットの作成に失敗しました。\n"
            f"回収の累計回数が上限（max_task_reclaims={config.max_task_reclaims}）"
            f"を超えた（今回で{reclaim.reclaim_count}回目）ため、自動回収を打ち切り、"
            "status:blocked-human-reviewへ遷移しました。\n"
            "未コミットの作業データを保全するため、worktreeは削除せずに残しています: "
            f"{reclaim.active.worktree_path}\n"
            f"エラー詳細:\n```\n{backup_error}\n```",
            forge=config.resolved_forge,
            on_label_applied=_release_entry,
        )
    except Exception as e:  # noqa: BLE001 - 1タスクの失敗でサイクルを止めない
        print(
            f"Warning: failed to escalate the GC reclaim of issue "
            f"#{reclaim.active.issue_number} after a WIP backup failure: {e}",
            file=sys.stderr,
        )
        if not released:
            _settle_reclaim(run_state, reclaim, config, open_prs, release_entry=False)
            return None
    return _reclaim_event(reclaim, "escalated_reclaim_limit_exceeded", True)


def _skip_backup_failure(
    run_state: RunState,
    reclaim: ZombieOrTimeoutReclaim,
    config: DispatcherConfig,
    backup_error: str,
    open_prs: Sequence[PrRecord] | None,
) -> None:
    _settle_reclaim(run_state, reclaim, config, open_prs, release_entry=False)
    try:
        config.resolved_forge.add_comment(
            reclaim.active.issue_number,
            f"タスク実行が {reclaim.reason} のためGCによる回収を試みましたが、"
            "WIPバックアップコミットの作成に失敗しました。\n"
            "未コミットの作業データ消失を防ぐため、今回のGC回収およびworktree削除処理を"
            "一時スキップしました。\n"
            f"エラー詳細:\n```\n{backup_error}\n```",
        )
    except Exception as e:  # noqa: BLE001 - 通知の失敗で確定を巻き戻さない
        print(
            f"Warning: skipped the GC reclaim of issue "
            f"#{reclaim.active.issue_number} but failed to post the reason: {e}",
            file=sys.stderr,
        )


def _apply_backup_failure(
    run_state: RunState,
    reclaim: ZombieOrTimeoutReclaim,
    config: DispatcherConfig,
    escalating: bool,
    backup_error: str,
    open_prs: Sequence[PrRecord] | None = None,
) -> dict | None:
    """WIPバックアップコミットの作成に失敗した場合の後始末。"""
    if escalating:
        return _escalate_backup_failure(
            run_state, reclaim, config, backup_error, open_prs
        )
    _skip_backup_failure(run_state, reclaim, config, backup_error, open_prs)
    return None


def _notify_escalated_reclaim(
    reclaim: ZombieOrTimeoutReclaim,
    config: DispatcherConfig,
    worktree_note: str,
    settle: Callable[[], None],
) -> None:
    active = reclaim.active
    reason = reclaim.reason
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
        on_label_applied=settle,
    )


def _notify_requeued_reclaim(
    reclaim: ZombieOrTimeoutReclaim,
    config: DispatcherConfig,
    worktree_note: str,
    settle: Callable[[], None],
) -> None:
    active = reclaim.active
    reason = reclaim.reason
    stale_labels = tuple(
        label
        for label in (StatusLabel.IN_PROGRESS, StatusLabel.BLOCKED)
        if label in reclaim.status_labels
    )
    transition_status_label(
        config.resolved_forge,
        active.issue_number,
        StatusLabel.QUEUED,
        stale_labels,
    )
    settle()
    try:
        config.resolved_forge.add_comment(
            active.issue_number,
            f"タスク実行が {reason} のため、GCにより{worktree_note}"
            "タスクを再キューイング（status:queued）しました"
            f"（回収{reclaim.reclaim_count}回目 / 上限{config.max_task_reclaims}回）。",
        )
    except Exception as e:  # noqa: BLE001 - 通知の失敗で回収をやり直さない
        print(
            f"Warning: requeued issue #{active.issue_number} but failed to post "
            f"the GC reclaim comment: {e}",
            file=sys.stderr,
        )


def _notify_reclaim(
    reclaim: ZombieOrTimeoutReclaim,
    config: DispatcherConfig,
    worktree_note: str,
    already_escalated: bool,
    escalating: bool,
    settle: Callable[[], None],
) -> None:
    """回収結果をGitHubへ反映する（ラベル遷移とコメント）。"""
    active = reclaim.active
    reason = reclaim.reason
    if already_escalated:
        config.resolved_forge.add_comment(
            active.issue_number,
            f"タスク実行が {reason} のため、GCにより{worktree_note}"
            "後始末しました。既に人間の確認が必要な状態のため、"
            "status:*ラベルは変更していません。",
        )
        settle()
        return
    if escalating:
        _notify_escalated_reclaim(reclaim, config, worktree_note, settle)
        return
    _notify_requeued_reclaim(reclaim, config, worktree_note, settle)


def _mark_reclaim_for_retry(
    run_state: RunState,
    reclaim: ZombieOrTimeoutReclaim,
    config: DispatcherConfig,
    open_prs: Sequence[PrRecord] | None,
    error: BaseException,
) -> None:
    """#512/PR#520レビュー7巡目対応(Codex P1): GitHubへの反映に失敗した回収を、
    次サイクルで必ず拾い直せる状態にして残す。
    """
    print(
        f"Warning: failed to apply the GC reclaim of issue "
        f"#{reclaim.active.issue_number} on GitHub: {error}",
        file=sys.stderr,
    )
    active = run_state.active_worktrees.get(reclaim.key)
    if active is None or active.started_at is None or is_process_alive(active.pid):
        return
    active.started_at = None
    try:
        save_run_state(
            run_state,
            config.run_state_path,
            now=reclaim.now,
            launch_window_seconds=config.window_seconds,
            open_prs=open_prs,
        )
    except Exception as e:  # noqa: BLE001 - ベストエフォートの後始末
        print(
            f"Warning: failed to persist the retry marker for issue "
            f"#{reclaim.active.issue_number}: {e}",
            file=sys.stderr,
        )


def _settle_reclaim(
    run_state: RunState,
    reclaim: ZombieOrTimeoutReclaim,
    config: DispatcherConfig,
    open_prs: Sequence[PrRecord] | None,
    *,
    release_entry: bool,
) -> None:
    """今サイクル分の回収を確定させ、その場でディスクへ書く。"""
    record = run_state.task_reclaim_counts.get(reclaim.active.issue_number)
    if record is not None and record.pending:
        record.pending = False
    if release_entry:
        run_state.active_worktrees.pop(reclaim.key, None)
    try:
        save_run_state(
            run_state,
            config.run_state_path,
            now=reclaim.now,
            launch_window_seconds=config.window_seconds,
            open_prs=open_prs,
        )
    except Exception as e:  # noqa: BLE001 - サイクル終端の保存に委ねる
        print(
            f"Warning: failed to persist the settled GC reclaim of issue "
            f"#{reclaim.active.issue_number}: {e}",
            file=sys.stderr,
        )


def _stop_reclaimed_process(reclaim: ZombieOrTimeoutReclaim) -> None:
    if reclaim.active.pid and reclaim.process_alive:
        try:
            os.kill(reclaim.active.pid, 9)
        except Exception:
            pass


def _execute_reclaim_lifecycle(
    run_state: RunState,
    reclaim: ZombieOrTimeoutReclaim,
    config: DispatcherConfig,
    open_prs: Sequence[PrRecord] | None,
    escalating: bool,
    already_escalated: bool,
    settle_once: Callable[[], None],
    is_settled: Callable[[], bool],
) -> dict | None:
    active = reclaim.active
    worktree_exists = os.path.exists(active.worktree_path)
    try:
        if worktree_exists:
            backup_error = backup_wip_commit(
                active.worktree_path, f"WIP: backup by Orchestune GC ({reclaim.reason})"
            )
            if backup_error is not None:
                return _apply_backup_failure(
                    run_state, reclaim, config, escalating, backup_error, open_prs
                )
            remove_worktree(active.worktree_path)
        worktree_note = (
            "作業ブランチにWIPコミットを退避した上で、"
            if worktree_exists
            else "物理worktreeが見つからなかったため、"
        )
        _notify_reclaim(
            reclaim,
            config,
            worktree_note,
            already_escalated,
            escalating,
            settle_once,
        )
    except Exception as e:  # noqa: BLE001 - 次サイクルでの再回収へ委ねる
        if not is_settled():
            _mark_reclaim_for_retry(run_state, reclaim, config, open_prs, e)
            return None
        print(
            f"Warning: applied the GC reclaim of issue "
            f"#{active.issue_number} but a later step failed: {e}",
            file=sys.stderr,
        )
    settle_once()
    return _reclaim_event(
        reclaim,
        "escalated_reclaim_limit_exceeded" if escalating else "gc_reclaimed",
        not already_escalated,
    )


def _apply_zombie_or_timeout_reclaim(
    run_state: RunState,
    reclaim: ZombieOrTimeoutReclaim,
    config: DispatcherConfig,
    open_prs: Sequence[PrRecord] | None = None,
) -> dict | None:
    """decide層が判定した回収対象に基づき、安全に副作用を適用する。"""
    already_escalated = any(
        label in reclaim.status_labels for label in TERMINAL_ESCALATION_LABELS
    )
    counted = not already_escalated
    escalating = counted and reclaim.escalate
    if not config.apply:
        return _reclaim_event(
            reclaim,
            "escalated_reclaim_limit_exceeded" if escalating else "gc_reclaimed",
            counted,
        )

    if counted and not _record_reclaim(run_state, reclaim, config, open_prs):
        return None
    _stop_reclaimed_process(reclaim)

    settled = False

    def _settle_once() -> None:
        nonlocal settled
        if not settled:
            settled = True
            _settle_reclaim(run_state, reclaim, config, open_prs, release_entry=True)

    return _execute_reclaim_lifecycle(
        run_state,
        reclaim,
        config,
        open_prs,
        escalating,
        already_escalated,
        _settle_once,
        lambda: settled,
    )


def execute_reclaim_repair_command(
    command: RepairCommand,
    run_state: RunState,
    reclaim: ZombieOrTimeoutReclaim,
    config: DispatcherConfig,
    open_prs: Sequence[PrRecord] | None = None,
) -> RepairResult:
    """Apply one re-observed reclaim through the existing safe lifecycle."""
    if command.code != COMMAND_RECLAIM:
        return RepairResult(
            command=command,
            status=RepairStatus.FAILED,
            diagnostics=(f"unsupported GC repair command: {command.code}",),
        )
    if not config.apply:
        return RepairResult(command=command, status=RepairStatus.SKIPPED)
    active = run_state.active_worktrees.get(reclaim.key)
    finding_codes = frozenset(command_finding_codes(command))
    precondition_holds = (
        command.subject_id == str(reclaim.active.issue_number)
        and active is not None
        and active == reclaim.active
        and bool(finding_codes.intersection(reclaim.finding_codes))
    )
    if not precondition_holds:
        return RepairResult(
            command=command,
            status=RepairStatus.SKIPPED,
            diagnostics=(
                f"reclaim precondition no longer holds for subject "
                f"{command.subject_id}",
            ),
        )
    event = _apply_zombie_or_timeout_reclaim(run_state, reclaim, config, open_prs)
    return RepairResult(
        command=command,
        status=RepairStatus.APPLIED if event is not None else RepairStatus.SKIPPED,
        diagnostics=(
            ()
            if event is not None
            else ("reclaim deferred by existing safety boundary",)
        ),
    )


def _collect_zombies_and_timeouts(
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
    held_worktree_paths: set[str] | None = None,
    open_prs: Sequence[PrRecord] | None = None,
) -> list[dict]:
    """Plan, re-observe, and apply zombie/timeout repairs at the GC boundary."""
    reclaims = _decide_zombie_or_timeout_reclaims(
        run_state,
        tasks_by_issue,
        config,
        held_worktree_paths,
        time.time(),
        open_prs,
    )
    events: list[dict] = []
    for reclaim in reclaims:
        refreshed = _reobserve_reclaim(
            run_state,
            tasks_by_issue,
            config,
            reclaim,
            held_worktree_paths,
            open_prs,
        )
        if refreshed is None:
            continue
        event = _apply_zombie_or_timeout_reclaim(run_state, refreshed, config, open_prs)
        if event is not None:
            events.append(event)
    return events


def _reobserve_reclaim(
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
    previous: ZombieOrTimeoutReclaim,
    held_worktree_paths: set[str] | None,
    open_prs: Sequence[PrRecord] | None,
) -> ZombieOrTimeoutReclaim | None:
    """Require the same typed reclaim to survive a targeted fresh observation."""
    active = run_state.active_worktrees.get(previous.key)
    if active is None or active.issue_number != previous.active.issue_number:
        return None
    targeted_state = RunState(
        active_worktrees={previous.key: active},
        task_reclaim_counts=run_state.task_reclaim_counts,
    )
    task = tasks_by_issue.get(active.issue_number)
    targeted_tasks = {} if task is None else {active.issue_number: task}
    candidates = _decide_zombie_or_timeout_reclaims(
        targeted_state,
        targeted_tasks,
        config,
        held_worktree_paths,
        time.time(),
        open_prs,
    )
    return next(
        (
            candidate
            for candidate in candidates
            if candidate.key == previous.key
            and set(candidate.finding_codes) & set(previous.finding_codes)
        ),
        None,
    )
