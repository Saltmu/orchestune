"""footprint逸脱によるDAG再計算通知と、依存先PRマージ済み時の自動リベース処理。"""

from __future__ import annotations

import dataclasses
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from orchestune import dispatch_gc
from orchestune.bounded_limit import exceeds_limit
from orchestune.dag_graph import recompute_dag_for_footprint_change
from orchestune.dag_models import FootprintConflict, SubTask
from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_labels import transition_status_label
from orchestune.dispatch_locks import check_footprint_deviation
from orchestune.dispatch_rules import ActiveWorktreeRuleOutcome, CycleContext
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree, RunState
from orchestune.forge import Forge, GitHubForge
from orchestune.git_cli import resolve_local_or_remote_branch, run_git
from orchestune.issue_parsing import backfill_recovery_counters
from orchestune.process_utils import default_ci_command, is_process_alive

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RebaseContext:
    """State shared by automatic rebase decision and application steps."""

    active: ActiveWorktree
    active_task: Task | None
    key: str
    run_state: RunState
    done_subtask_ids: set[str]
    ci_passed_pr_subtask_ids: set[str]
    subtask_branch_map: dict[str, str]
    config: DispatcherConfig


def notify_recompute(
    conflict: FootprintConflict,
    work_summary: str,
    parent_issue_number: int | None,
    apply: bool,
    issue_number_by_subtask_id: dict[str, int],
    forge: Forge | None = None,
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
        forge = forge or GitHubForge()
        if subtask_issue is not None:
            forge.add_comment(subtask_issue, detail)
        if other_issue is not None:
            forge.add_comment(other_issue, detail)
        if parent_issue_number is not None:
            forge.add_comment(parent_issue_number, bodies[-1])
        if blocked_issue is not None:
            transition_status_label(
                forge, blocked_issue, "status:blocked", ("status:queued",)
            )
            forge.add_label(blocked_issue, "status:blocked-recompute")

    return bodies


def notify_force_serial(
    subtask_id: str,
    issue_number: int,
    parent_issue_number: int | None,
    retry_count: int,
    apply: bool,
    forge: Forge | None = None,
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
        forge = forge or GitHubForge()
        forge.add_comment(parent_issue_number, body)
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


def _persist_recovery_counters(
    active: ActiveWorktree, config: DispatcherConfig
) -> None:
    """#513: recompute_count/forced_serialをIssue本文のFootprintフェンスへ
    書き戻す。`run_state.json`消失時、自己修復（`dispatch_recovery.py`の
    `recovery_counters_from_body`）がここから復元する。

    逸脱イベント発生時（DAG再計算・forced_serial遷移）にのみ呼ばれるため、
    毎ディスパッチサイクルではなく、頻度としては`notify_recompute`/
    `notify_force_serial`のコメント投稿と同程度——追加のAPI呼び出しは
    このイベントに比例する。
    """
    issue = config.resolved_forge.get_issue(active.issue_number)
    if issue is None:
        return
    patched_body = backfill_recovery_counters(
        issue.body, active.recompute_count, active.forced_serial
    )
    if patched_body is not None:
        config.resolved_forge.update_issue_body(active.issue_number, patched_body)


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

    if exceeds_limit(active.recompute_count + 1, config.max_recompute_retries):
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
        threshold=config.dag_similarity_threshold,
        ignore_patterns=config.dag_ignore_patterns,
    )
    return FootprintDeviationDecision(
        action="recomputed",
        subtask_id=active_task.subtask_id,
        conflicts=list(conflicts),
    )


def _apply_forced_serial_event(
    active: ActiveWorktree,
    decision: FootprintDeviationDecision,
    config: DispatcherConfig,
) -> dict:
    notify_force_serial(
        decision.subtask_id,
        active.issue_number,
        config.parent_issue_number,
        decision.recompute_count,
        apply=config.apply,
        forge=config.resolved_forge,
    )
    if config.apply:
        active.forced_serial = True
        _persist_recovery_counters(active, config)
        config.resolved_forge.add_label(active.issue_number, "status:force-serial")
    return {"recompute_count": decision.recompute_count}


def _apply_recomputed_event(
    active: ActiveWorktree,
    deviated: list[str],
    decision: FootprintDeviationDecision,
    issue_number_by_subtask_id: dict[str, int],
    config: DispatcherConfig,
) -> dict:
    for conflict in decision.conflicts:
        notify_recompute(
            conflict,
            work_summary=f"{', '.join(deviated)} への逸脱を検知",
            parent_issue_number=config.parent_issue_number,
            apply=config.apply,
            issue_number_by_subtask_id=issue_number_by_subtask_id,
            forge=config.resolved_forge,
        )
    if config.apply:
        active.recompute_count += 1
        _persist_recovery_counters(active, config)
    return {"conflicts": [dataclasses.asdict(c) for c in decision.conflicts]}


def _apply_footprint_deviation_outcome(
    active: ActiveWorktree,
    deviated: list[str],
    decision: FootprintDeviationDecision,
    issue_number_by_subtask_id: dict[str, int],
    config: DispatcherConfig,
) -> dict:
    event: dict = {
        "issue_number": active.issue_number,
        "deviated_files": deviated,
        "action": decision.action,
    }
    if decision.action in ("already_forced_serial", "skipped_unknown_subtask"):
        return event

    if decision.action == "forced_serial":
        event.update(_apply_forced_serial_event(active, decision, config))
        return event

    event.update(
        _apply_recomputed_event(
            active, deviated, decision, issue_number_by_subtask_id, config
        )
    )
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
        if not is_process_alive(pid):
            return
        time.sleep(0.1)


def _decide_rebase_target(
    active_task: Task | None,
    done_subtask_ids: set[str],
    ci_passed_pr_subtask_ids: set[str],
    subtask_branch_map: dict[str, str],
) -> str | None:
    """起動時のスタッキング制約に合わせて、自動リベース対象を1件に絞れる場合のみ
    その依存先ブランチを返す（副作用なし）。"""
    if not active_task or not active_task.depends_on:
        return None
    stackable_deps = []
    for dep in active_task.depends_on:
        if dep in done_subtask_ids:
            continue
        if dep in ci_passed_pr_subtask_ids:
            stackable_deps.append(dep)
            continue
        return None

    if len(stackable_deps) != 1:
        return None
    return subtask_branch_map.get(stackable_deps[0])


def _decide_rebase_needed(
    parent_branch: str, child_branch: str, worktree_path: str | Path
) -> bool:
    """`parent_branch`が`child_branch`の祖先でない（＝リベースが必要）かを、
    読み取り専用の`git merge-base --is-ancestor`で判定する。"""

    try:
        resolved_parent = resolve_local_or_remote_branch(
            worktree_path,
            parent_branch,
            prefer_remote=parent_branch.startswith("parent/"),
        )
    except Exception as e:
        logger.warning(
            f"Failed to resolve branch '{parent_branch}' in {worktree_path}: {e}"
        )
        return False

    try:
        res = run_git(
            ["merge-base", "--is-ancestor", resolved_parent, child_branch],
            cwd=worktree_path,
            check=False,
        )
        if res.returncode == 0:
            return False
        elif res.returncode == 1:
            return True
        else:
            logger.warning(
                f"git merge-base failed with returncode {res.returncode}: {res.stderr.strip()}"
            )
            return False
    except OSError as e:
        logger.warning(f"git merge-base failed with OSError: {e}")
        return False


def _prepare_wip_backup_for_rebase(
    active: ActiveWorktree, config: DispatcherConfig, ctx: RebaseContext
) -> bool:
    if active.pid:
        try:
            os.kill(active.pid, 9)
            _wait_for_process_terminate(active.pid)
        except Exception:
            pass

    backup_error = dispatch_gc.backup_wip_commit(
        active.worktree_path, "WIP: backup by Orchestune auto-rebase"
    )
    if backup_error is not None:
        transition_status_label(
            config.resolved_forge,
            active.issue_number,
            "status:manual-merge-required",
            ("status:in-progress",),
        )
        config.resolved_forge.add_comment(
            active.issue_number,
            "自動リベース前のWIPバックアップコミットの作成に失敗しました。\n"
            f"未コミットの作業データが worktree（{active.worktree_path}）に残っている"
            "可能性があるため、削除・再作成される前に手動で確認してください。\n"
            f"エラー詳細:\n```\n{backup_error}\n```",
        )
        del ctx.run_state.active_worktrees[ctx.key]
        return False
    return True


def _handle_rebase_failure(
    active: ActiveWorktree,
    parent_branch: str,
    e: Exception,
    config: DispatcherConfig,
    ctx: RebaseContext,
) -> None:
    try:
        run_git(["rebase", "--abort"], cwd=active.worktree_path, check=False)
    except Exception:
        pass

    transition_status_label(
        config.resolved_forge,
        active.issue_number,
        "status:manual-merge-required",
        ("status:in-progress",),
    )
    cmd_args = getattr(e, "cmd", [])
    if cmd_args == default_ci_command():
        msg = "自動リベース後のローカルCI実行に失敗しました。手動で修正を行ってください。\n"
    elif cmd_args and "push" in cmd_args:
        msg = (
            "自動リベース後のブランチpushに失敗しました"
            "（rebase自体はコンフリクトなく成功しています）。"
            "手動でpushと後続の対応を行ってください。\n"
        )
    else:
        msg = "自動リベース中にコンフリクトが発生しました。手動でマージを行ってください。\n"

    config.resolved_forge.add_comment(
        active.issue_number,
        f"{msg}対象の依存元ブランチ: {parent_branch}",
    )
    del ctx.run_state.active_worktrees[ctx.key]


def _apply_auto_rebase(ctx: RebaseContext, parent_branch: str) -> None:
    active = ctx.active
    active_task = ctx.active_task
    assert active_task is not None
    config = ctx.config
    if not config.apply:
        return

    if not _prepare_wip_backup_for_rebase(active, config, ctx):
        return

    resolved_parent = resolve_local_or_remote_branch(
        active.worktree_path,
        parent_branch,
        prefer_remote=parent_branch.startswith("parent/"),
    )

    try:
        run_git(["rebase", resolved_parent], cwd=active.worktree_path, check=True)
        env = _get_ci_env(Path(config.worktree_root).resolve().parent)
        ci_res = subprocess.run(
            default_ci_command(),
            cwd=active.worktree_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
            active_task, active.branch, Path(active.worktree_path), force_push=True
        )
        active.pid = handle.pid
        active.external_id = handle.external_id
        active.external_url = handle.external_url
        active.started_at = time.time()
        active.base_branch = parent_branch
    except (subprocess.CalledProcessError, OSError) as e:
        _handle_rebase_failure(active, parent_branch, e, config, ctx)


def _try_auto_rebase(ctx: RebaseContext) -> bool:
    """decide+applyの薄いラッパー。自動リベースを試行し、実際にリベースを
    実行した場合は True を返す。リベースが不要、あるいは対象がない場合は
    False を返す（呼び出し元が footprint 逸脱チェック等の後続処理へ
    フォールスルーできるようにするため）。"""
    parent_branch = _decide_rebase_target(
        ctx.active_task,
        ctx.done_subtask_ids,
        ctx.ci_passed_pr_subtask_ids,
        ctx.subtask_branch_map,
    )
    if parent_branch is None:
        return False

    if _decide_rebase_needed(
        parent_branch, ctx.active.branch, ctx.active.worktree_path
    ):
        _apply_auto_rebase(ctx, parent_branch)
        return True
    return False


def _rule_auto_rebase(
    ctx: CycleContext, key: str, active: ActiveWorktree, active_task: Task | None
) -> ActiveWorktreeRuleOutcome | None:
    """#201: 自動リベース判定＆実行。"""
    if not dispatch_gc.is_process_alive(active.pid):
        return None
    rebase_ctx = RebaseContext(
        active=active,
        active_task=active_task,
        key=key,
        run_state=ctx.run_state,
        done_subtask_ids=ctx.done_subtask_ids,
        ci_passed_pr_subtask_ids=ctx.ci_passed_pr_subtask_ids,
        subtask_branch_map=ctx.subtask_branch_map,
        config=ctx.config,
    )
    if not _try_auto_rebase(rebase_ctx):
        return None
    return ActiveWorktreeRuleOutcome(terminal=True)


def _rule_footprint_deviation(
    ctx: CycleContext, key: str, active: ActiveWorktree, active_task: Task | None
) -> ActiveWorktreeRuleOutcome:
    """フォールバックルール: 他のどのルールにも該当しなかったactive worktreeに
    ついて、footprint逸脱の有無を判定する。ルールチェーンの末尾として、常に
    非Noneの結果を返し必ずこのactive worktreeの処理を終える。
    """
    deviated = check_footprint_deviation(
        active.worktree_path,
        active.declared_footprint,
        base=active.base_branch,
        min_changed_lines=ctx.config.deviation_buffer_lines,
    )
    if deviated is None:
        deviated = []
    if not deviated:
        return ActiveWorktreeRuleOutcome(terminal=True)

    event = _handle_footprint_deviation(
        active, deviated, ctx.tasks_by_issue, ctx.issue_number_by_subtask_id, ctx.config
    )
    forced_serial = event["action"] in ("forced_serial", "already_forced_serial")
    return ActiveWorktreeRuleOutcome(
        deviation_event=event, forced_serial=forced_serial, terminal=True
    )
