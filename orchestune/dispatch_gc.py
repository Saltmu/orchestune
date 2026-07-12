"""ゾンビタスク・タイムアウトタスクのGC回収と、完了worktreeの後片付け処理。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from orchestune import github
from orchestune.dispatch_escalation import apply_human_review_escalation
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree

if TYPE_CHECKING:
    from orchestune.dispatch_state import RunState
    from orchestune.dispatcher import DispatcherConfig


def is_process_alive(pid: int | None) -> bool:
    """#193: 記録済みpidのプロセス生存確認によるタスク完了判定。

    シグナル送信権限がない場合（別ユーザー所有のPID再利用等）は、
    安全側に倒し「生存している」とみなす。
    """
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def worktree_has_uncommitted_changes(worktree_path: str | Path) -> bool:
    """#193: worktree削除前の未コミット変更確認。

    `git status`自体が失敗する場合（worktreeが既に手動削除済み等）は、
    クオータ解放を優先し安全側でクリーン（変更なし）として扱う。
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return False
    return bool(result.stdout.strip())


def worktree_has_new_commits(worktree_path: str | Path, base_branch: str) -> bool:
    """#74: base_branchに対して実コミットが積まれているかの確認。

    プロセス終了+cleanなworktreeというだけでは、権限拒否等で何も実装されずに
    終了したケースと本当に完了したケースを区別できない。#135: 比較に失敗した場合
    （`base_branch`参照が解決できない等）は「新規コミットが確認できた」わけでは
    ないため、安全側に倒し「新規コミット無し」と同じ`False`を返す（既存の
    `completed_no_commits`エスカレーション経路に合流させ、実体のない完了確定を防ぐ）。
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "rev-list",
                "--count",
                f"{base_branch}..HEAD",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return int(result.stdout.strip() or "0") > 0
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        print(
            f"Warning: failed to check new commits for {worktree_path!r} against "
            f"{base_branch!r}: {exc}",
            file=sys.stderr,
        )
        return False


def remove_worktree(worktree_path: str | Path) -> None:
    """#193: 完了したworktreeを撤去する。既に手動削除済み等の失敗は無視する
    （run_stateからのクオータ解放を妨げないことを優先する）。"""
    try:
        subprocess.run(
            ["git", "worktree", "remove", str(worktree_path)],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        pass


@dataclass
class CompletedWorktreeDecision:
    action: str
    subtask_id: str = ""


def _decide_completed_worktree_outcome(
    active: ActiveWorktree,
    active_task: Task | None,
) -> CompletedWorktreeDecision:
    """#193/#74: プロセス終了を検知したactive worktreeへの対応方針を、副作用なし
    （worktree削除・githubラベル変更を行わない）で判定する。"""
    subtask_id = active_task.subtask_id if active_task else ""

    if worktree_has_uncommitted_changes(active.worktree_path):
        # 未コミットの変更が残っている場合は、削除・ラベル遷移を行わず人間の
        # 確認を待つ（安全側に倒し、作業内容の消失を防ぐ）。
        return CompletedWorktreeDecision(action="completion_skipped_dirty_worktree")

    if not worktree_has_new_commits(active.worktree_path, active.base_branch):
        # #74: プロセスは終了しworktreeもcleanだが、base_branchに対して新規コミットが
        # 1件も無い＝権限拒否等で実際には何も実装されず終了したと考えられる。
        # ここでstatus:doneを付与すると、依存先タスクが同一サイクル内で実体のない
        # 完了を根拠に誤ってstatus:queuedへ昇格してしまうため、completed扱いにしない。
        return CompletedWorktreeDecision(
            action="completed_no_commits", subtask_id=subtask_id
        )

    return CompletedWorktreeDecision(action="completed", subtask_id=subtask_id)


def _apply_completed_worktree_outcome(
    active: ActiveWorktree,
    decision: CompletedWorktreeDecision,
    config: DispatcherConfig,
) -> dict:
    """decide層が判定した方針に基づき、worktree撤去・githubラベル/コメント更新を行う。"""
    event: dict = {
        "issue_number": active.issue_number,
        "worktree_path": active.worktree_path,
        "action": decision.action,
    }

    if decision.action == "completion_skipped_dirty_worktree":
        return event

    if decision.action == "completed_no_commits":
        if config.apply:
            remove_worktree(active.worktree_path)
            apply_human_review_escalation(
                active.issue_number,
                ("status:in-progress",),
                "エージェントプロセスの終了を検知しましたが、ベースブランチ"
                f"(`{active.base_branch}`)に対する新規コミットが1件も検出できませんでした。"
                "権限拒否やエラーにより実際の作業が行われなかった可能性があるため、"
                "自動的な完了・依存タスクの昇格を見送り、`status:blocked-human-review`に"
                "変更しました。ログを確認の上、必要であれば`status:queued`へ再設定してください。",
            )
        event["subtask_id"] = decision.subtask_id
        event["commit_sha"] = None
        return event

    # decision.action == "completed"
    commit_sha = None
    if config.apply:
        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=active.worktree_path,
                capture_output=True,
                text=True,
                check=True,
            )
            commit_sha = res.stdout.strip()
        except Exception:
            pass

        remove_worktree(active.worktree_path)
        github.remove_label(active.issue_number, "status:in-progress")
        github.add_label(active.issue_number, "status:done")

    event["subtask_id"] = decision.subtask_id
    event["commit_sha"] = commit_sha
    return event


def _finalize_completed_worktree(
    active: ActiveWorktree,
    active_task: Task | None,
    config: DispatcherConfig,
) -> dict:
    """decide+applyの薄いラッパー（呼び出し互換のため維持）。"""
    decision = _decide_completed_worktree_outcome(active, active_task)
    return _apply_completed_worktree_outcome(active, decision, config)


def _decide_not_needed_dirty_worktree(active: ActiveWorktree) -> bool:
    """worktreeに未コミットの変更が残っているか（副作用なし）を判定する。"""
    return worktree_has_uncommitted_changes(active.worktree_path)


def _finalize_not_needed_worktree(
    active: ActiveWorktree,
    active_task: Task | None,
    config: DispatcherConfig,
) -> dict:
    """#280/#282: `status:not-needed`ラベル検知による完了後処理。

    セッションが「既に要件を満たしており対応不要」と判断した場合、コミット・PRを
    作らないためclosingIssuesReferences等の完了シグナルが一切発生せず、
    `_finalize_completed_worktree`の通常経路では永遠に完了検知されない。
    このラベルを検知した時点で即座に完了とみなし、worktree撤去・quota解放を行う。

    クラウドルーチンが利用可能な場合（#282）は、誤った対応不要判定で本来必要な
    作業が埋もれるリスクを避けるため、即座にクローズせず独立した検証レビューを
    fireし、その判定結果をポーリングして後続サイクルでクローズする
    （`process_pending_not_needed_reviews`が担う）。クラウドルーチン未設定
    （ローカル/テスト環境）では検証レビューを起動できないため、従来通り
    即座にクローズする。
    """
    from orchestune.dispatch_targets import ClaudeCodeCloudRoutineDispatchTarget
    from orchestune.integration_coordinator import (
        IntegrationCoordinator,
        record_pending_not_needed_review,
    )

    event: dict = {
        "issue_number": active.issue_number,
        "worktree_path": active.worktree_path,
    }

    # decide: dirty worktreeかどうかの判定は副作用を持たない。
    if _decide_not_needed_dirty_worktree(active):
        event["action"] = "completion_skipped_dirty_worktree"
        return event

    subtask_id = active_task.subtask_id if active_task else ""

    # 以降はact: worktree撤去・githubラベル/クローズ・検証レビューの起動を行う。
    if config.apply:
        remove_worktree(active.worktree_path)
        github.remove_label(active.issue_number, "status:in-progress")

        if isinstance(config.dispatch_target, ClaudeCodeCloudRoutineDispatchTarget):
            coordinator = IntegrationCoordinator(config.dispatch_target)
            handle = coordinator.dispatch_not_needed_review(
                active.issue_number, subtask_id
            )
            record_pending_not_needed_review(
                config.not_needed_review_state_path,
                issue_number=active.issue_number,
                subtask_id=subtask_id,
                session_handle=handle,
            )
            event["action"] = "not_needed_review_dispatched"
        else:
            github.close_issue(
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


def _collect_zombies_and_timeouts(
    run_state: RunState,
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
) -> list[dict]:
    """ゾンビプロセス（PID消失かつ未コミット変更あり）およびタイムアウトしたタスクをGC回収する。"""
    if config.task_timeout_seconds <= 0:
        return []
    events = []
    now = time.time()
    for key, active in list(run_state.active_worktrees.items()):
        active_task = tasks_by_issue.get(active.issue_number)

        is_zombie = False
        is_timeout = False

        process_alive = is_process_alive(active.pid)
        if not process_alive:
            if os.path.exists(
                active.worktree_path
            ) and worktree_has_uncommitted_changes(active.worktree_path):
                is_zombie = True

        if not is_zombie and active.started_at:
            timeout_limit = getattr(config, "task_timeout_seconds", 3600)
            if timeout_limit > 0 and now - active.started_at > timeout_limit:
                is_timeout = True

        if is_zombie or is_timeout:
            reason = "process disappeared" if is_zombie else "timeout exceeded"

            if config.apply and os.path.exists(active.worktree_path):
                backup_success = True
                if worktree_has_uncommitted_changes(active.worktree_path):
                    try:
                        subprocess.run(
                            ["git", "-C", active.worktree_path, "add", "-A"],
                            capture_output=True,
                            check=True,
                        )
                        subprocess.run(
                            [
                                "git",
                                "-C",
                                active.worktree_path,
                                "commit",
                                "-m",
                                f"WIP: backup by Orchestune GC ({reason})",
                            ],
                            capture_output=True,
                            check=True,
                        )
                    except subprocess.CalledProcessError as e:
                        backup_success = False
                        github.add_comment(
                            active.issue_number,
                            f"タスク実行が {reason} のためGCによる回収を試みましたが、WIPバックアップコミットの作成に失敗しました。\n"
                            f"未コミットの作業データ消失を防ぐため、今回のGC回収およびworktree削除処理を一時スキップしました。\n"
                            f"エラー詳細:\n```\n{e.stderr.strip() if e.stderr else str(e)}\n```",
                        )

                if not backup_success:
                    continue

                if is_timeout and active.pid and process_alive:
                    try:
                        os.kill(active.pid, 9)
                    except Exception:
                        pass

                remove_worktree(active.worktree_path)

                github.remove_label(active.issue_number, "status:in-progress")
                github.add_label(active.issue_number, "status:queued")
                github.add_comment(
                    active.issue_number,
                    f"タスク実行が {reason} のため、GCにより作業ブランチにWIPコミットを退避した上で、タスクを再キューイング（status:queued）しました。",
                )

            if config.apply:
                del run_state.active_worktrees[key]

            events.append(
                {
                    "issue_number": active.issue_number,
                    "subtask_id": active_task.subtask_id if active_task else "",
                    "action": "gc_reclaimed",
                    "reason": reason,
                }
            )

    return events
