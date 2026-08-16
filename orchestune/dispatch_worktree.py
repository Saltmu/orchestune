from __future__ import annotations

import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from orchestune import dispatch_gc
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_targets import BranchReachabilityError, DispatchTarget
from orchestune.git_cli import resolve_local_or_remote_branch, run_git
from orchestune.process_utils import file_lock as file_lock
from orchestune.validation import validate_ref_name


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


def create_worktree_and_launch(
    task: Task,
    branch_name: str,
    worktree_root: str | Path,
    dispatch_target: DispatchTarget,
    apply: bool,
    base_branch: str | None = None,
) -> LaunchResult:
    pid: int | None = None
    external_id: str | None = None
    external_url: str | None = None
    launched = False
    error_message: str | None = None
    dispatch_started_at: float | None = None

    try:
        validate_ref_name(branch_name)
        worktree_root = Path(worktree_root)
        slug = branch_name.replace("/", "-")
        worktree_path = worktree_root / slug
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
            external_id=None,
            external_url=None,
            validation_error=True,
        )

    if apply:
        worktree_created = False
        try:
            # 1. すでにディレクトリが存在する場合のクリーンアップ
            if worktree_path.exists():
                # #213: 未コミットの変更が残ったまま削除すると、前回のエージェント
                # 作業が黙って消失する。削除前にWIPコミットとして退避を試みる。
                # `backup_wip_commit`はfail-closed（dirty判定自体に失敗した場合も
                # 退避未完了扱い）なので、None以外が返れば削除せず起動を失敗させる。
                backup_error = dispatch_gc.backup_wip_commit(
                    worktree_path,
                    "WIP: backup by Orchestune before worktree recreation",
                )
                if backup_error is not None:
                    error_message = (
                        f"Uncommitted changes in {worktree_path} could not be "
                        f"backed up before recreation: {backup_error}"
                    )
                    print(
                        f"Error: Failed to back up uncommitted changes for issue "
                        f"#{task.issue_number} before recreation: {backup_error}",
                        file=sys.stderr,
                    )
                    return LaunchResult(
                        issue_number=task.issue_number,
                        branch=branch_name,
                        worktree_path=str(worktree_path),
                        pid=None,
                        launched=False,
                        error_message=error_message,
                        external_id=None,
                        external_url=None,
                    )
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

            # 2. 無効なworktreeの整理
            run_git(["worktree", "prune"], cwd=None, check=False)

            worktree_root.mkdir(parents=True, exist_ok=True)

            # 3. ブランチがすでに存在する場合はそのまま利用し、存在しない場合は新規作成する
            if _branch_exists(branch_name):
                cmd = ["worktree", "add", str(worktree_path), branch_name]
            else:
                cmd = ["worktree", "add", "-b", branch_name, str(worktree_path)]
                if base_branch:
                    base_branch = resolve_local_or_remote_branch(
                        ".",
                        base_branch,
                        prefer_remote=base_branch.startswith("parent/"),
                    )
                    cmd.append(base_branch)
            run_git(cmd, cwd=None, check=True)
            # #262レビュー対応: worktreeのprune/backup/add完了後、実際の
            # dispatch_target.launch()呼び出し直前に取得する。ここより前に
            # 取得すると、prune/backup/add中に作成された無関係なPRまで
            # 新sessionの成果物として誤認する窓が広がる。
            dispatch_started_at = time.time()
            worktree_created = True

            handle = dispatch_target.launch(task, branch_name, worktree_path)
            pid = handle.pid
            external_id = handle.external_id
            external_url = handle.external_url
            launched = True
        # #244: BranchReachabilityErrorは、cloud-routine起動前のリモートブランチ
        # 到達性検証（_push_branch_and_verify）の失敗。fireは行われていないため、
        # 通常の起動失敗として扱い、status:blocked化の既存経路へ乗せる。
        # #260レビュー対応: 汎用RuntimeErrorではなく専用型のみ捕捉し、
        # このチェック以外の実装バグまで握り潰さないようにする。
        except (subprocess.CalledProcessError, OSError, BranchReachabilityError) as e:
            if worktree_created:
                # launch失敗時の補償処理: 作成したworktreeをGit管理および物理ディスクから回収
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
            error_details = ""
            if isinstance(e, subprocess.CalledProcessError):
                error_details = f" (stderr: {e.stderr.strip() if e.stderr else ''})"
            print(
                f"Error: Failed to create worktree or launch for issue #{task.issue_number}: {e}{error_details}",
                file=sys.stderr,
            )
            error_message = f"{e}{error_details}"

    return LaunchResult(
        issue_number=task.issue_number,
        branch=branch_name,
        worktree_path=str(worktree_path),
        pid=pid,
        launched=launched,
        error_message=error_message,
        external_id=external_id,
        external_url=external_url,
        dispatch_started_at=dispatch_started_at,
    )
