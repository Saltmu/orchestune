from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

from orchestune.dispatch_scoring import Task
from orchestune.dispatch_targets import DispatchTarget
from orchestune.github import _validate_ref_name, resolve_local_or_remote_branch


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
    _validate_ref_name(branch_name)
    worktree_root = Path(worktree_root)
    slug = branch_name.replace("/", "-")
    worktree_path = worktree_root / slug

    pid: int | None = None
    external_id: str | None = None
    external_url: str | None = None
    launched = False
    error_message: str | None = None

    if apply:
        try:
            # 1. 無効なworktreeの整理
            subprocess.run(["git", "worktree", "prune"], capture_output=True, text=True)

            # 2. すでにディレクトリが存在する場合のクリーンアップ
            if worktree_path.exists():
                try:
                    shutil.rmtree(worktree_path)
                except Exception:
                    pass

            worktree_root.mkdir(parents=True, exist_ok=True)

            # 3. ブランチがすでに存在する場合はそのまま利用し、存在しない場合は新規作成する
            if _branch_exists(branch_name):
                cmd = ["git", "worktree", "add", str(worktree_path), branch_name]
            else:
                cmd = ["git", "worktree", "add", "-b", branch_name, str(worktree_path)]
                if base_branch:
                    base_branch = resolve_local_or_remote_branch(".", base_branch)
                    cmd.append(base_branch)
            subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
            )
            handle = dispatch_target.launch(task, branch_name, worktree_path)
            pid = handle.pid
            external_id = handle.external_id
            external_url = handle.external_url
            launched = True
        except (subprocess.CalledProcessError, OSError) as e:
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
    )


@contextlib.contextmanager
def file_lock(lock_path: Path) -> Iterator[None]:
    if fcntl is None:
        raise RuntimeError(
            "fcntl is not supported on this platform. File locking is required."
        )

    lock_fd = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if lock_fd:
            lock_fd.close()
        raise RuntimeError(
            f"Another instance is already running (locked on {lock_path})"
        ) from None
    except Exception:
        if lock_fd:
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
        if lock_fd:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            lock_fd.close()
