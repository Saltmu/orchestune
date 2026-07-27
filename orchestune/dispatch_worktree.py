from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

from orchestune import dispatch_gc
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_targets import BranchReachabilityError, DispatchTarget
from orchestune.github import _validate_ref_name


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
    res_local = subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{branch_name}"],
        capture_output=True,
    )
    if res_local.returncode == 0:
        return True

    res_remote = subprocess.run(
        ["git", "show-ref", "--verify", f"refs/remotes/origin/{branch_name}"],
        capture_output=True,
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
        _validate_ref_name(branch_name)
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
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(worktree_path)],
                        capture_output=True,
                    )
                    if worktree_path.exists():
                        shutil.rmtree(worktree_path)
                except Exception:
                    pass

            # 2. 無効なworktreeの整理
            subprocess.run(["git", "worktree", "prune"], capture_output=True, text=True)

            worktree_root.mkdir(parents=True, exist_ok=True)

            # 3. ブランチがすでに存在する場合はそのまま利用し、存在しない場合は新規作成する
            if _branch_exists(branch_name):
                cmd = ["git", "worktree", "add", str(worktree_path), branch_name]
            else:
                cmd = ["git", "worktree", "add", "-b", branch_name, str(worktree_path)]
                if base_branch:
                    from orchestune.github import resolve_local_or_remote_branch

                    base_branch = resolve_local_or_remote_branch(
                        ".",
                        base_branch,
                        prefer_remote=base_branch.startswith("parent/"),
                    )
                    cmd.append(base_branch)
            subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
            )
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
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(worktree_path)],
                        capture_output=True,
                    )
                    if worktree_path.exists():
                        shutil.rmtree(worktree_path)
                    subprocess.run(["git", "worktree", "prune"], capture_output=True)
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


@contextlib.contextmanager
def file_lock(lock_path: Path) -> Iterator[None]:
    if fcntl is None and msvcrt is None:
        raise RuntimeError(
            "Neither fcntl nor msvcrt is supported on this platform. File locking is required."
        )

    # #274レビュー対応(P2): mkdir/openの失敗(ディスクフルやACLによる
    # PermissionError等)を、ロック競合(flock/lockingのPermissionError・
    # BlockingIOError)と同じ「他インスタンス実行中」扱いにしてしまうと、
    # 本当の設定・環境エラーが握り潰されてしまう。ロック取得(mkdir/open)と
    # ロック競合判定(flock/locking)を別のtry/exceptに分離し、後者でのみ
    # RuntimeErrorへ変換する。
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(lock_path, "w" if fcntl is not None else "a+")

    try:
        if fcntl is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
            except BlockingIOError:
                raise RuntimeError(
                    f"Another instance is already running (locked on {lock_path})"
                ) from None
        else:
            assert msvcrt is not None
            lock_fd.write(" ")
            lock_fd.flush()
            lock_fd.seek(0)
            try:
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
            except PermissionError:
                raise RuntimeError(
                    f"Another instance is already running (locked on {lock_path})"
                ) from None
    except Exception:
        lock_fd.close()
        raise

    # #227: ロック取得成功後のbody実行は別のtry/finallyに分離する。
    # ロック取得(mkdir/open/flock)の例外処理と同じtry内でyieldしていると、
    # body側で発生した例外がこのgeneratorへ再スローされ、下のexcept Exceptionに
    # 捕捉されて再度yieldしてしまい、Pythonが
    # `RuntimeError: generator didn't stop after throw()` を送出して
    # 元の例外を握り潰してしまう（body側の例外はロック取得の失敗ではないため
    # ここで処理すべきではない）。
    try:
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
            except Exception:
                pass
        else:
            assert msvcrt is not None
            try:
                lock_fd.seek(0)
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        lock_fd.close()
