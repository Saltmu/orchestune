"""footprint逸脱によるDAG再計算通知と、依存先PRマージ済み時の自動リベース処理。"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import time
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


def _handle_footprint_deviation(
    active: ActiveWorktree,
    deviated: list[str],
    tasks_by_issue: dict[int, Task],
    issue_number_by_subtask_id: dict[str, int],
    config: DispatcherConfig,
) -> dict:
    """#192/#200: 1つのactive worktreeのfootprint逸脱を処理し、イベントを返す。

    既に強制直列化済みなら何もしない（チャーン防止）。リトライ上限超過なら
    強制直列化にフォールバックし、それ以外はDAG再計算・通知を実行する。
    """
    event: dict = {"issue_number": active.issue_number, "deviated_files": deviated}

    if active.forced_serial:
        event["action"] = "already_forced_serial"
        return event

    active_task = tasks_by_issue.get(active.issue_number)
    if active_task is None or not active_task.subtask_id:
        event["action"] = "skipped_unknown_subtask"
        return event

    if active.recompute_count >= config.max_recompute_retries:
        notify_force_serial(
            active_task.subtask_id,
            active.issue_number,
            config.parent_issue_number,
            active.recompute_count,
            apply=config.apply,
        )
        event["action"] = "forced_serial"
        event["recompute_count"] = active.recompute_count
        if config.apply:
            active.forced_serial = True
            github.add_label(active.issue_number, "status:force-serial")
        return event

    merged_footprint = tuple(dict.fromkeys([*active.declared_footprint, *deviated]))
    _, conflicts = recompute_dag_for_footprint_change(
        _build_subtasks_for_recompute(tasks_by_issue),
        active_task.subtask_id,
        updated_footprint=merged_footprint,
    )

    for conflict in conflicts:
        notify_recompute(
            conflict,
            work_summary=f"{', '.join(deviated)} への逸脱を検知",
            parent_issue_number=config.parent_issue_number,
            apply=config.apply,
            issue_number_by_subtask_id=issue_number_by_subtask_id,
        )

    event["action"] = "recomputed"
    event["conflicts"] = [dataclasses.asdict(c) for c in conflicts]
    if config.apply:
        active.recompute_count += 1
    return event


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


def _try_auto_rebase(
    active: ActiveWorktree,
    active_task: Task | None,
    key: str,
    run_state: RunState,
    ci_passed_pr_subtask_ids: set[str],
    subtask_branch_map: dict[str, str],
    config: DispatcherConfig,
) -> bool:
    """自動リベースを試行し、実行した場合は True を返す。"""
    if not active_task or not active_task.depends_on:
        return False

    for dep in active_task.depends_on:
        if dep in ci_passed_pr_subtask_ids:
            parent_branch = subtask_branch_map[dep]
            child_branch = active.branch

            needs_rebase = False
            try:
                res = subprocess.run(
                    [
                        "git",
                        "merge-base",
                        "--is-ancestor",
                        parent_branch,
                        child_branch,
                    ],
                    capture_output=True,
                    text=True,
                )
                if res.returncode != 0:
                    needs_rebase = True
            except OSError:
                pass

            if needs_rebase:
                if config.apply:
                    if active.pid:
                        try:
                            os.kill(active.pid, 9)
                        except Exception:
                            pass
                    try:
                        subprocess.run(
                            [
                                "git",
                                "-C",
                                active.worktree_path,
                                "rebase",
                                parent_branch,
                            ],
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
                                [
                                    "git",
                                    "-C",
                                    active.worktree_path,
                                    "rebase",
                                    "--abort",
                                ],
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
                return True
    return False
