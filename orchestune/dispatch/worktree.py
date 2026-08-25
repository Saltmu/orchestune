from __future__ import annotations

import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from orchestune.dispatch import gc as dispatch_gc
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.targets import (
    BranchReachabilityError,
    DispatchHandle,
    DispatchTarget,
)
from orchestune.infra.git_cli import resolve_local_or_remote_branch, run_git
from orchestune.infra.process_utils import file_lock as file_lock
from orchestune.validation import validate_ref_name

if TYPE_CHECKING:
    from orchestune.dispatch.execution_profiles import ExecutionSelection


@dataclass
class LaunchResult:
    issue_number: int
    branch: str
    worktree_path: str
    pid: int | None
    launched: bool
    error_message: str | None = None
    external_id: str | None = None
    external_url: str | None = None
    validation_error: bool = False
    # #262レビュー対応: worktreeのprune/backup/addにかかる時間を含めず、
    # 実際にdispatch_target.launch()を呼び出す直前の時刻。呼び出し元が
    # PR完了判定のstale境界（started_at）としてこれを使うことで、
    # worktree準備中に作成された無関係な既存PRを新sessionの成果物と
    # 誤認する窓を最小化する。launchが行われなかった場合はNone。
    dispatch_started_at: float | None = None
    execution_selection: ExecutionSelection | None = None


def _branch_exists(branch_name: str) -> bool:
    """指定されたブランチがローカルまたはリモート追跡ブランチとして存在するか確認する。"""
    res_local = run_git(
        ["show-ref", "--verify", f"refs/heads/{branch_name}"], cwd=None, check=False
    )
    if res_local.returncode == 0:
        return True

    res_remote = run_git(
        ["show-ref", "--verify", f"refs/remotes/origin/{branch_name}"],
        cwd=None,
        check=False,
    )
    if res_remote.returncode == 0:
        return True

    return False


def _resolve_worktree_path(worktree_root: str | Path, branch_name: str) -> Path:
    """ブランチ名のバリデーションを行い、worktreeの対象パスを計算する。"""
    validate_ref_name(branch_name)
    slug = branch_name.replace("/", "-")
    return Path(worktree_root) / slug


def _cleanup_existing_worktree(worktree_path: Path, issue_number: int) -> str | None:
    """すでにディレクトリが存在する場合、WIPコミットとして退避した上で削除する。
    退避に失敗した場合はエラー文字列を返し、削除を行わない（fail-closed）。"""
    if not worktree_path.exists():
        return None

    # #213: 未コミットの変更が残ったまま削除すると、前回のエージェント
    # 作業が黙って消失する。削除前にWIPコミットとして退避を試みる。
    # `backup_wip_commit`はfail-closed（dirty判定自体に失敗した場合も
    # 退避未完了扱い）なので、None以外が返れば削除せず起動を失敗させる。
    backup_error = dispatch_gc.backup_wip_commit(
        worktree_path,
        "WIP: backup by Orchestune before worktree recreation",
    )
    if backup_error is not None:
        return backup_error

    try:
        run_git(
            ["worktree", "remove", "--force", str(worktree_path)],
            cwd=None,
            check=False,
        )
        if worktree_path.exists():
            shutil.rmtree(worktree_path)
    except Exception:
        pass
    return None


def _create_worktree(
    worktree_path: Path,
    worktree_root: Path,
    branch_name: str,
    base_branch: str | None = None,
) -> None:
    """無効なworktreeを整理し、指定のブランチ/ベースブランチでworktreeを作成する。"""
    run_git(["worktree", "prune"], cwd=None, check=False)
    worktree_root.mkdir(parents=True, exist_ok=True)

    if _branch_exists(branch_name):
        cmd = ["worktree", "add", str(worktree_path), branch_name]
    else:
        cmd = ["worktree", "add", "-b", branch_name, str(worktree_path)]
        if base_branch:
            resolved_base = resolve_local_or_remote_branch(
                ".",
                base_branch,
                prefer_remote=base_branch.startswith("parent/"),
            )
            cmd.append(resolved_base)
    run_git(cmd, cwd=None, check=True)


def _provision_and_launch(
    dispatch_target: DispatchTarget,
    task: Task,
    branch_name: str,
    worktree_path: Path,
    *,
    force_push: bool = False,
    execution_selection: ExecutionSelection | None = None,
) -> tuple[DispatchHandle, float]:
    """#262レビュー対応: dispatch_target.launch()直前の時刻をstarted_atとして取得し起動する。"""
    dispatch_started_at = time.time()
    try:
        handle = dispatch_target.launch(
            task,
            branch_name,
            worktree_path,
            force_push=force_push,
            execution_selection=execution_selection,
        )
    except TypeError:
        handle = dispatch_target.launch(
            task,
            branch_name,
            worktree_path,
            force_push=force_push,
        )
    return handle, dispatch_started_at


def _cleanup_failed_worktree(worktree_path: Path) -> None:
    """launch失敗時の補償処理: 作成したworktreeをGit管理および物理ディスクから回収する。"""
    try:
        run_git(
            ["worktree", "remove", "--force", str(worktree_path)],
            cwd=None,
            check=False,
        )
        if worktree_path.exists():
            shutil.rmtree(worktree_path)
        run_git(["worktree", "prune"], cwd=None, check=False)
    except Exception:
        pass


def _prepare_and_launch(
    task: Task,
    branch_name: str,
    worktree_path: Path,
    worktree_root: str | Path,
    dispatch_target: DispatchTarget,
    base_branch: str | None,
    execution_selection: ExecutionSelection | None = None,
) -> LaunchResult:
    worktree_created = False
    try:
        _create_worktree(worktree_path, Path(worktree_root), branch_name, base_branch)
        worktree_created = True
        handle, dispatch_started_at = _provision_and_launch(
            dispatch_target,
            task,
            branch_name,
            worktree_path,
            execution_selection=execution_selection,
        )
        return LaunchResult(
            issue_number=task.issue_number,
            branch=branch_name,
            worktree_path=str(worktree_path),
            pid=handle.pid,
            launched=True,
            external_id=handle.external_id,
            external_url=handle.external_url,
            dispatch_started_at=dispatch_started_at,
            execution_selection=execution_selection,
        )
    except (subprocess.CalledProcessError, OSError, BranchReachabilityError) as e:
        if worktree_created:
            _cleanup_failed_worktree(worktree_path)
        error_details = ""
        if isinstance(e, subprocess.CalledProcessError) and e.stderr:
            error_details = f" (stderr: {e.stderr.strip()})"
        print(
            f"Error: Failed to create worktree or launch for issue #{task.issue_number}: {e}{error_details}",
            file=sys.stderr,
        )
        return LaunchResult(
            issue_number=task.issue_number,
            branch=branch_name,
            worktree_path=str(worktree_path),
            pid=None,
            launched=False,
            error_message=f"{e}{error_details}",
            execution_selection=execution_selection,
        )


def _handle_backup_error(
    worktree_path: Path, issue_number: int, branch_name: str, backup_error: str
) -> LaunchResult:
    error_message = (
        f"Uncommitted changes in {worktree_path} could not be "
        f"backed up before recreation: {backup_error}"
    )
    print(
        f"Error: Failed to back up uncommitted changes for issue "
        f"#{issue_number} before recreation: {backup_error}",
        file=sys.stderr,
    )
    return LaunchResult(
        issue_number=issue_number,
        branch=branch_name,
        worktree_path=str(worktree_path),
        pid=None,
        launched=False,
        error_message=error_message,
    )


def create_worktree_and_launch(
    task: Task,
    branch_name: str,
    worktree_root: str | Path,
    dispatch_target: DispatchTarget,
    apply: bool,
    base_branch: str | None = None,
    execution_selection: ExecutionSelection | None = None,
) -> LaunchResult:
    try:
        worktree_path = _resolve_worktree_path(worktree_root, branch_name)
    except ValueError as e:
        print(
            f"Error: Invalid branch name {branch_name!r} for issue #{task.issue_number}: {e}",
            file=sys.stderr,
        )
        return LaunchResult(
            issue_number=task.issue_number,
            branch=branch_name,
            worktree_path="",
            pid=None,
            launched=False,
            error_message=str(e),
            validation_error=True,
            execution_selection=execution_selection,
        )

    if not apply:
        return LaunchResult(
            issue_number=task.issue_number,
            branch=branch_name,
            worktree_path=str(worktree_path),
            pid=None,
            launched=False,
            execution_selection=execution_selection,
        )

    backup_error = _cleanup_existing_worktree(worktree_path, task.issue_number)
    if backup_error is not None:
        return _handle_backup_error(
            worktree_path, task.issue_number, branch_name, backup_error
        )

    return _prepare_and_launch(
        task,
        branch_name,
        worktree_path,
        worktree_root,
        dispatch_target,
        base_branch,
        execution_selection=execution_selection,
    )
