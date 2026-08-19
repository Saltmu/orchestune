"""Zombie and timeout reclamation for active dispatcher worktrees."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Sequence
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
from orchestune.dispatch_state import (
    ActiveWorktree,
    RunState,
    TaskReclaimRecord,
    save_run_state,
)
from orchestune.models import PrRecord
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
        #
        # PR#520レビュー9巡目対応(Codex P2): 前回の回収がGitHubへ反映できないまま
        # 終わっていた場合（`pending`）は、その予約分を再利用して回数を進めない。
        # 一時的なAPI障害の再試行で`status:queued`への差し戻しが一度も起きないまま
        # 上限に達してしまうのを防ぐ（＝上限は「再投入の回数」を拘束する）。
        previous_record = run_state.task_reclaim_counts.get(active.issue_number)
        if previous_record is None:
            reclaim_count = 1
        elif previous_record.pending:
            reclaim_count = previous_record.count
        else:
            reclaim_count = previous_record.count + 1
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


def _apply_backup_failure(
    run_state: RunState,
    reclaim: ZombieOrTimeoutReclaim,
    config: DispatcherConfig,
    escalating: bool,
    backup_error: str,
    open_prs: Sequence[PrRecord] | None = None,
) -> dict | None:
    """WIPバックアップコミットの作成に失敗した場合の後始末。

    未コミットの作業データ消失を防ぐため、worktreeは削除せずに残す。

    #512/PR#520レビュー3巡目対応(Codex P1): 回収回数が上限を超えている場合は、
    毎サイクル「スキップした」とコメントし続ける代わりに
    `status:blocked-human-review`へ遷移させ、帳簿エントリも解放する。
    バックアップが恒常的に失敗するタスク（ディスク枯渇・破損したworktree等）は、
    この分岐が無いと`status:in-progress`のままクオータを占有し続け、回収回数と
    失敗コメントだけが無限に積み上がる——本Issueが塞ごうとしている終端の無い
    経路そのものになってしまう。worktreeは残したまま人間へ引き渡す。
    """
    if escalating:
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
        )
        _settle_reclaim(run_state, reclaim, config, open_prs, release_entry=True)
        return _reclaim_event(reclaim, "escalated_reclaim_limit_exceeded", True)

    config.resolved_forge.add_comment(
        reclaim.active.issue_number,
        f"タスク実行が {reclaim.reason} のためGCによる回収を試みましたが、"
        "WIPバックアップコミットの作成に失敗しました。\n"
        "未コミットの作業データ消失を防ぐため、今回のGC回収およびworktree削除処理を"
        "一時スキップしました。\n"
        f"エラー詳細:\n```\n{backup_error}\n```",
    )
    # スキップした回収も1回分として確定させる（予約のまま残すと、バックアップが
    # 失敗し続けるタスクが上限に到達できず終端しなくなる）。
    _settle_reclaim(run_state, reclaim, config, open_prs, release_entry=False)
    return None


def _notify_reclaim(
    reclaim: ZombieOrTimeoutReclaim,
    config: DispatcherConfig,
    worktree_note: str,
    already_escalated: bool,
    escalating: bool,
    settle: Callable[[], None],
) -> None:
    """回収結果をGitHubへ反映する（ラベル遷移とコメント）。

    #512/PR#520レビュー10巡目対応(Codex P2): 再キューイングでは、`status:queued`が
    見えた時点で`settle`（予約の確定と帳簿エントリの解放・永続化）を呼ぶ。
    後続のコメント投稿が失敗しても、再投入自体は既に成立しているためである。
    ここで予約を`pending`のまま残すと、次サイクルで`_rule_stale_entry`が
    エントリを破棄してタスクが再起動されたあと、その回収が同じ回数を再利用して
    しまい、コメント投稿の失敗1回につき起動枠が1回増えてしまう。

    エスカレーション（`apply_human_review_escalation`）はラベル遷移とコメントを
    まとめて行うため分割しない。コメントだけ失敗した場合は予約が`pending`のまま
    残るが、`status:blocked-human-review`が付いた時点で自動的な再起動は起きず、
    次サイクルの回収（`already_escalated`分岐）で予約が確定するため、起動枠が
    増えることはない。
    """
    active = reclaim.active
    reason = reclaim.reason
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
        settle()
        return
    if escalating:
        # #512: 回収・再投入の累計が上限を超えたタスクは、status:queuedへ
        # 戻さずstatus:blocked-human-reviewで停止させる（台帳の更新・永続化は
        # 呼び出し側でラベル遷移より先に済ませている）。
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
        settle()
        return
    # #381レビュー対応(Codex P2): stacked launch等の中断した遷移で
    # status:blockedが取り残されている場合も併せて除去し、
    # status:queuedへ確実に収束させる。
    stale_labels = tuple(
        label
        for label in ("status:in-progress", "status:blocked")
        if label in reclaim.status_labels
    )
    transition_status_label(
        config.resolved_forge,
        active.issue_number,
        "status:queued",
        stale_labels,
    )
    # status:queuedが見えた時点で再投入は成立している。コメントより先に確定させる。
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


def _mark_reclaim_for_retry(
    run_state: RunState,
    reclaim: ZombieOrTimeoutReclaim,
    config: DispatcherConfig,
    open_prs: Sequence[PrRecord] | None,
    error: BaseException,
) -> None:
    """#512/PR#520レビュー7巡目対応(Codex P1): GitHubへの反映に失敗した回収を、
    次サイクルで必ず拾い直せる状態にして残す。

    ここへ来た時点でworktreeは削除済みだが、GitHub側のラベルは
    `status:in-progress`のまま、`active_worktrees`のエントリも残る。この状態を
    そのまま次サイクルへ渡すと、対象プロセスが停止していることもあって
    次の2つの困った挙動になる:

    - タイムアウト判定が無効（`--task-timeout-seconds 0`、既定）なら、
      `_check_zombie_and_timeout`は「worktree不在 + started_at既知」を回収対象と
      判定せず、この帳簿エントリが同時実行スロットを永久に占有する。
    - タイムアウト判定が有効でも、GCフェーズより先に走る`_rule_completed`が
      「プロセス停止 + worktree不在」を完了とみなし、新規コミット無しの完了
      （`completed_no_commits`）として即座にエスカレーションしてしまう
      （PR#520レビュー8巡目対応(Codex P2)）——記録済みの回収の再試行にならない。

    そこで`started_at`を未知（None）へ落とし、#383のゾンビ復旧経路
    （プロセス不在 + worktree不在 + 開始時刻不明）へ載せる。`started_at`が未知の
    エントリは`_is_worktree_complete`が完了と判定しないため、完了処理にも
    さらわれない。

    ただし対象プロセスがまだ生きている場合（killに失敗した場合）は`started_at`を
    保つ: ゾンビ判定はプロセス停止が前提で拾えず、`started_at`を消すと
    タイムアウト判定まで無効化してしまうため、タイムアウト判定に委ねる方が安全。

    回収回数は既に永続化済みのため、次サイクルの再回収でも二重には数えない
    （増えるのは上限に対して厳しくなる方向のみ）。
    """
    print(
        f"Warning: failed to apply the GC reclaim of issue "
        f"#{reclaim.active.issue_number} on GitHub: {error}",
        file=sys.stderr,
    )
    active = run_state.active_worktrees.get(reclaim.key)
    if active is None or active.started_at is None:
        return
    if is_process_alive(active.pid):
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
    """今サイクル分の回収を確定させ、その場でディスクへ書く。

    #512/PR#520レビュー9巡目対応(Codex P1): `active_worktrees`からの削除を
    サイクル終端の`save_run_state`任せにすると、`status:blocked-human-review`を
    付与した直後にプロセスが止まった場合、次サイクルではそのIssueが
    `_fetch_issues`の取得対象（open な status:queued/in-progress/blocked/…）に
    含まれないため`tasks_by_issue`にも現れず、staleエントリ破棄ルールが
    働かない。タイムアウト判定が無効な既定構成では「worktree不在 +
    started_at既知」もゾンビ判定に該当しないため、この帳簿エントリが同時実行
    スロットを永久に占有しかねない。

    同(Codex P2): 併せて、予約中（`pending`）の回収回数を確定済みへ落とす。
    ここまで到達した回収は、再キューイング・エスカレーション・バックアップ失敗に
    よるスキップのいずれであれ「1回分を使い切った」ものとして扱う
    （バックアップが失敗し続けるタスクも上限で終端させるため）。
    """
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


def _apply_zombie_or_timeout_reclaim(
    run_state: RunState,
    reclaim: ZombieOrTimeoutReclaim,
    config: DispatcherConfig,
    open_prs: Sequence[PrRecord] | None = None,
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
    （active_worktreesは変更せず、次サイクルでの再試行に委ねる）。ただし回収回数が
    上限を超えている場合は、そのスキップ自体が終端の無いループになるため、
    worktreeを残したまま`status:blocked-human-review`へ遷移させる
    （`_apply_backup_failure`）。

    #512/PR#520レビュー2巡目対応(Codex P2): 回収回数の記録・永続化は、
    プロセスkill・worktree削除・ラベル遷移といったあらゆる副作用より**先**に行う。
    永続化に失敗した場合はfail-closedで、何も壊さずにNoneを返して次サイクルへ
    委ねる。この順序では、上記のWIPバックアップ失敗でスキップした回収も1回として
    数えられる（＝上限に対して厳しくなる方向の非対称）が、バックアップ失敗が
    毎サイクル続くようなタスクこそ人間の確認へ回すべきであり、副作用を先に
    実行して回数だけ失う（次サイクルで数えられないまま再起動できてしまう）よりも
    安全側である。

    #512/PR#520レビュー7巡目対応(Codex P1): GitHubへの反映（コメント・ラベル遷移・
    エスカレーション）が例外で失敗した場合は、帳簿エントリを消さずに
    `_mark_reclaim_for_retry`で次サイクルが拾い直せる状態にしてからNoneを返す。
    一時的なAPI障害でスロットを占有したままになるのを防ぐ。
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
        # #512: 破壊的な副作用より先に回数を確定・永続化する（ローカル先行）。
        if counted and not _record_reclaim(run_state, reclaim, config, open_prs):
            return None
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
        settled = False

        def _settle_once() -> None:
            nonlocal settled
            if settled:
                return
            settled = True
            _settle_reclaim(run_state, reclaim, config, open_prs, release_entry=True)

        try:
            if worktree_exists:
                backup_error = backup_wip_commit(
                    active.worktree_path, f"WIP: backup by Orchestune GC ({reason})"
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
                _settle_once,
            )
        except Exception as e:  # noqa: BLE001 - 次サイクルでの再回収へ委ねる
            if not settled:
                _mark_reclaim_for_retry(run_state, reclaim, config, open_prs, e)
                return None
            # GitHub側は適用済み（＝回収は成立）。以降の失敗は警告に留める。
            print(
                f"Warning: applied the GC reclaim of issue "
                f"#{active.issue_number} but a later step failed: {e}",
                file=sys.stderr,
            )
        _settle_once()

    return _reclaim_event(
        reclaim,
        "escalated_reclaim_limit_exceeded" if escalating else "gc_reclaimed",
        counted,
    )


def _collect_zombies_and_timeouts(
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
    held_worktree_paths: set[str] | None = None,
    open_prs: Sequence[PrRecord] | None = None,
) -> list[dict]:
    """Decide and apply zombie and timeout reclamations."""
    reclaims = _decide_zombie_or_timeout_reclaims(
        run_state, tasks_by_issue, config, held_worktree_paths, time.time()
    )
    events: list[dict] = []
    for reclaim in reclaims:
        event = _apply_zombie_or_timeout_reclaim(run_state, reclaim, config, open_prs)
        if event is not None:
            events.append(event)
    return events
