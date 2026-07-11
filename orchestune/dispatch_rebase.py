"""footprint逸脱によるDAG再計算通知と、依存先PRマージ済み時の自動リベース処理。"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from orchestune import github
from orchestune.dag import (
    FootprintConflict,
    SubTask,
    recompute_dag_for_footprint_change,
)
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree

if TYPE_CHECKING:
    from orchestune.dispatch_state import RunState
    from orchestune.dispatcher import DispatcherConfig


def notify_recompute(
    conflict: FootprintConflict,
    work_summary: str,
    parent_issue_number: int | None,
    apply: bool,
    issue_number_by_subtask_id: dict[str, int],
) -> list[str]:
    detail = (
        "footprint逸脱によるDAG再計算が発生しました。\n\n"
        f"- 発覚したサブタスク: {conflict.subtask_id}\n"
        f"- 競合相手のサブタスク: {conflict.other_subtask_id}\n"
        f"- 結合度スコア: {conflict.similarity:.3f}\n"
        f"- ブロックされるサブタスク: {conflict.blocked_subtask_id}\n"
        f"- 発覚時点までの作業内容: {work_summary}\n"
    )
    bodies = [detail, detail]

    subtask_issue = issue_number_by_subtask_id.get(conflict.subtask_id)
    other_issue = issue_number_by_subtask_id.get(conflict.other_subtask_id)
    blocked_issue = issue_number_by_subtask_id.get(conflict.blocked_subtask_id)

    if parent_issue_number is not None:
        bodies.append(
            f"[自動記録] サブタスク {conflict.subtask_id} と {conflict.other_subtask_id} の"
            f"間でfootprint逸脱によるDAG再計算が発生しました。\n\n{detail}"
        )

    if apply:
        if subtask_issue is not None:
            github.add_comment(subtask_issue, detail)
        if other_issue is not None:
            github.add_comment(other_issue, detail)
        if parent_issue_number is not None:
            github.add_comment(parent_issue_number, bodies[-1])
        if blocked_issue is not None:
            github.add_label(blocked_issue, "status:blocked-recompute")

    return bodies


def notify_force_serial(
    subtask_id: str,
    issue_number: int,
    parent_issue_number: int | None,
    retry_count: int,
    apply: bool,
) -> str:
    """#200: DAG再計算のリトライ上限超過を親Issueへ通知し、強制直列化を告知する。"""
    body = (
        "footprint逸脱によるDAG再計算のリトライ上限に達しました。\n\n"
        f"- サブタスク: {subtask_id}\n"
        f"- 対象Issue: #{issue_number}\n"
        f"- 再計算試行回数: {retry_count}\n\n"
        "ライブロック（チャーン）を防ぐため、このサブタスクを単独で直列実行する"
        "フォールバックに切り替えます。新規タスクのdispatchは、このサブタスクが"
        "完了するまで一時停止します。\n"
    )
    if apply and parent_issue_number is not None:
        github.add_comment(parent_issue_number, body)
    return body


def _build_subtasks_for_recompute(
    tasks_by_issue: dict[int, Task],
) -> dict[str, SubTask]:
    return {
        task.subtask_id: SubTask(
            id=task.subtask_id,
            description="",
            footprint=task.footprint,
            symbols=task.symbols,
            depends_on=(),
            risk=task.risk,
            risk_reasons=(),
        )
        for task in tasks_by_issue.values()
        if task.subtask_id
    }


@dataclass
class FootprintDeviationDecision:
    action: str
    subtask_id: str = ""
    recompute_count: int = 0
    conflicts: list[FootprintConflict] = field(default_factory=list)


def _decide_footprint_deviation_outcome(
    active: ActiveWorktree,
    deviated: list[str],
    tasks_by_issue: dict[int, Task],
    config: DispatcherConfig,
) -> FootprintDeviationDecision:
    """#192/#200: footprint逸脱への対応方針を判定する（githubへの通知・
    active/run_stateの変更は行わない）。DAG再計算自体は純粋な計算のためここに含む。

    既に強制直列化済みなら何もしない（チャーン防止）。リトライ上限超過なら
    強制直列化にフォールバックし、それ以外はDAG再計算を行う。
    """
    if active.forced_serial:
        return FootprintDeviationDecision(action="already_forced_serial")

    active_task = tasks_by_issue.get(active.issue_number)
    if active_task is None or not active_task.subtask_id:
        return FootprintDeviationDecision(action="skipped_unknown_subtask")

    if active.recompute_count >= config.max_recompute_retries:
        return FootprintDeviationDecision(
            action="forced_serial",
            subtask_id=active_task.subtask_id,
            recompute_count=active.recompute_count,
        )

    merged_footprint = tuple(dict.fromkeys([*active.declared_footprint, *deviated]))
    _, conflicts = recompute_dag_for_footprint_change(
        _build_subtasks_for_recompute(tasks_by_issue),
        active_task.subtask_id,
        updated_footprint=merged_footprint,
    )
    return FootprintDeviationDecision(
        action="recomputed",
        subtask_id=active_task.subtask_id,
        conflicts=list(conflicts),
    )


def _apply_footprint_deviation_outcome(
    active: ActiveWorktree,
    deviated: list[str],
    decision: FootprintDeviationDecision,
    issue_number_by_subtask_id: dict[str, int],
    config: DispatcherConfig,
) -> dict:
    """decide層が判定した方針に基づき、通知・active/run_stateの更新を行う。"""
    event: dict = {
        "issue_number": active.issue_number,
        "deviated_files": deviated,
        "action": decision.action,
    }

    if decision.action in ("already_forced_serial", "skipped_unknown_subtask"):
        return event

    if decision.action == "forced_serial":
        notify_force_serial(
            decision.subtask_id,
            active.issue_number,
            config.parent_issue_number,
            decision.recompute_count,
            apply=config.apply,
        )
        event["recompute_count"] = decision.recompute_count
        if config.apply:
            active.forced_serial = True
            github.add_label(active.issue_number, "status:force-serial")
        return event

    # decision.action == "recomputed"
    for conflict in decision.conflicts:
        notify_recompute(
            conflict,
            work_summary=f"{', '.join(deviated)} への逸脱を検知",
            parent_issue_number=config.parent_issue_number,
            apply=config.apply,
            issue_number_by_subtask_id=issue_number_by_subtask_id,
        )

    event["conflicts"] = [dataclasses.asdict(c) for c in decision.conflicts]
    if config.apply:
        active.recompute_count += 1
    return event


def _handle_footprint_deviation(
    active: ActiveWorktree,
    deviated: list[str],
    tasks_by_issue: dict[int, Task],
    issue_number_by_subtask_id: dict[str, int],
    config: DispatcherConfig,
) -> dict:
    """decide+applyの薄いラッパー（呼び出し互換のため維持）。"""
    decision = _decide_footprint_deviation_outcome(
        active, deviated, tasks_by_issue, config
    )
    return _apply_footprint_deviation_outcome(
        active, deviated, decision, issue_number_by_subtask_id, config
    )


def _get_ci_env(repository_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    venv_path = repository_root / ".venv"
    if "tools/orchestune" in str(venv_path):
        parent_venv = venv_path.parent.parent.parent / ".venv"
        if parent_venv.exists():
            venv_path = parent_venv

    if venv_path.exists():
        env["VIRTUAL_ENV"] = str(venv_path.resolve())
        bin_path = venv_path / "bin"
        if bin_path.exists():
            env["PATH"] = f"{bin_path.resolve()}{os.pathsep}{env.get('PATH', '')}"
    return env


def _wait_for_process_terminate(pid: int, timeout: float = 5.0) -> None:
    """指定されたPIDのプロセスが終了するまで待機する。"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        except OSError:
            return
        time.sleep(0.1)


def _decide_rebase_target(
    active_task: Task | None,
    ci_passed_pr_subtask_ids: set[str],
    subtask_branch_map: dict[str, str],
) -> str | None:
    """CIが通過済みの依存先ブランチ（自動リベース対象）があれば、その最初の1件を
    返す（副作用なし）。"""
    if not active_task or not active_task.depends_on:
        return None
    for dep in active_task.depends_on:
        if dep in ci_passed_pr_subtask_ids:
            return subtask_branch_map[dep]
    return None


def _decide_rebase_needed(parent_branch: str, child_branch: str) -> bool:
    """`parent_branch`が`child_branch`の祖先でない（＝リベースが必要）かを、
    読み取り専用の`git merge-base --is-ancestor`で判定する。"""
    try:
        res = subprocess.run(
            ["git", "merge-base", "--is-ancestor", parent_branch, child_branch],
            capture_output=True,
            text=True,
        )
        return res.returncode != 0
    except OSError:
        return False


def _apply_auto_rebase(
    active: ActiveWorktree,
    active_task: Task,
    key: str,
    run_state: RunState,
    parent_branch: str,
    config: DispatcherConfig,
) -> None:
    """実際にプロセス停止・git rebase・ローカルCI再実行・エージェント（LLM）の
    再起動を行う。コンフリクトやCI失敗時はstatus:manual-merge-requiredへ遷移する。"""
    if not config.apply:
        return

    if active.pid:
        try:
            os.kill(active.pid, 9)
            _wait_for_process_terminate(active.pid)
        except Exception:
            pass
    try:
        subprocess.run(
            ["git", "-C", active.worktree_path, "rebase", parent_branch],
            capture_output=True,
            text=True,
            check=True,
        )

        env = _get_ci_env(Path(config.worktree_root).resolve().parent)
        ci_res = subprocess.run(
            ["./scripts/local-ci.sh"],
            cwd=active.worktree_path,
            capture_output=True,
            text=True,
            env=env,
        )
        if ci_res.returncode != 0:
            raise subprocess.CalledProcessError(
                ci_res.returncode,
                ci_res.args,
                output=ci_res.stdout,
                stderr=ci_res.stderr,
            )

        assert config.dispatch_target is not None
        handle = config.dispatch_target.launch(
            active_task, active.branch, Path(active.worktree_path)
        )
        active.pid = handle.pid
        active.external_id = handle.external_id
        active.external_url = handle.external_url
        active.started_at = time.time()
    except (subprocess.CalledProcessError, OSError) as e:
        try:
            subprocess.run(
                ["git", "-C", active.worktree_path, "rebase", "--abort"],
                capture_output=True,
                text=True,
            )
        except Exception:
            pass

        github.remove_label(active.issue_number, "status:in-progress")
        github.add_label(
            active.issue_number,
            "status:manual-merge-required",
        )

        msg = "自動リベース中にコンフリクトが発生しました。手動でマージを行ってください。\n"
        cmd_args = getattr(e, "cmd", [])
        if cmd_args and "local-ci.sh" in cmd_args[0]:
            msg = "自動リベース後のローカルCI実行に失敗しました。手動で修正を行ってください。\n"

        github.add_comment(
            active.issue_number,
            f"{msg}対象の依存元ブランチ: {parent_branch}",
        )
        del run_state.active_worktrees[key]


def _try_auto_rebase(
    active: ActiveWorktree,
    active_task: Task | None,
    key: str,
    run_state: RunState,
    ci_passed_pr_subtask_ids: set[str],
    subtask_branch_map: dict[str, str],
    config: DispatcherConfig,
) -> bool:
    """decide+applyの薄いラッパー（呼び出し互換のため維持）。自動リベースを
    試行対象と判定した場合は True を返す（実際にリベースを実行したかは問わない。
    呼び出し元はTrueを「このサイクルはこのactive worktreeの処理を終えた」の
    シグナルとして扱うため）。"""
    parent_branch = _decide_rebase_target(
        active_task, ci_passed_pr_subtask_ids, subtask_branch_map
    )
    if parent_branch is None:
        return False

    if _decide_rebase_needed(parent_branch, active.branch):
        assert active_task is not None
        _apply_auto_rebase(active, active_task, key, run_state, parent_branch, config)
    return True
