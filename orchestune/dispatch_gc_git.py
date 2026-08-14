"""GCプロセスおよび完了時のGit・Worktree操作に関連するヘルパー関数群。"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from orchestune.forge import Forge, GitHubForge
from orchestune.git_cli import (
    fetch_remote_branch,
    normalize_remote_branch_name,
    resolve_local_or_remote_branch,
    run_git,
)


def worktree_has_uncommitted_changes(worktree_path: str | Path) -> bool:
    """#193: worktree削除前の未コミット変更確認。

    `git status`自体が失敗する場合（worktreeが既に手動削除済み等）は、
    クオータ解放を優先し安全側でクリーン（変更なし）として扱う。
    """
    try:
        result = run_git(["status", "--porcelain"], cwd=worktree_path, check=True)
    except (subprocess.CalledProcessError, OSError):
        return False
    return bool(result.stdout.strip())


def _describe_git_error(e: subprocess.CalledProcessError | OSError) -> str:
    stderr = getattr(e, "stderr", None)
    return stderr.strip() if stderr else str(e)


def backup_wip_commit(worktree_path: str | Path, commit_message: str) -> str | None:
    """#213: worktreeを指定のコミットメッセージでWIP退避する
    （ゾンビGCと自動リベース/worktree再作成で共通化）。

    削除・rebase等の破壊的操作の直前に呼ばれる想定のため、fail-closedとする:
    - `git status`で確認できてcleanな場合のみ、退避不要としてNoneを返す。
    - dirty判定でadd/commitが成功した場合もNoneを返す。
    - `git status`自体が失敗し安全性が確認できない場合、およびadd/commit自体が
      失敗した場合は、いずれもエラー詳細の文字列を返す（`worktree_has_uncommitted_changes`
      と異なり、確認不能を「clean」とはみなさない。呼び出し側は非Noneが返った場合、
      削除・rebaseを中止して退避未完了として扱うこと）。
    """
    try:
        status = run_git(["status", "--porcelain"], cwd=worktree_path, check=True)
    except (subprocess.CalledProcessError, OSError) as e:
        return _describe_git_error(e)

    if not status.stdout.strip():
        return None

    try:
        run_git(["add", "-A"], cwd=worktree_path, check=True)
        run_git(["commit", "-m", commit_message], cwd=worktree_path, check=True)
    except (subprocess.CalledProcessError, OSError) as e:
        return _describe_git_error(e)
    return None


def worktree_has_new_commits(worktree_path: str | Path, base_branch: str) -> bool:
    """#74: base_branchに対して実コミットが積まれているかの確認。

    プロセス終了+cleanなworktreeというだけでは、権限拒否等で何も実装されずに
    終了したケースと本当に完了したケースを区別できない。#135: 比較に失敗した場合
    （`base_branch`参照が解決できない等）は「新規コミットが確認できた」わけでは
    ないため、安全側に倒し「新規コミット無し」と同じ`False`を返す（既存の
    `completed_no_commits`エスカレーション経路に合流させ、実体のない完了確定を防ぐ）。
    """
    try:
        # #172: 親ブランチがリモート追跡ブランチとしてのみ存在する場合に対応するため、
        # 比較前に解決を試みる（デフォルトでローカル優先、なければリモートにフォールバック）。
        resolved_base = resolve_local_or_remote_branch(
            worktree_path,
            base_branch,
        )
        result = run_git(
            ["rev-list", "--count", f"{resolved_base}..HEAD"],
            cwd=worktree_path,
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


def remote_branch_commit_sha_if_ahead(
    repository_root: str | Path, branch: str, base_branch: str
) -> str | None:
    """#177: 外部実行ブランチがベースより進んでいれば、そのhead SHAを返す。

    クラウドルーチンは起動時に作成したローカルworktreeを更新しないため、完了時は
    作業・ベース両ブランチのリモート追跡参照を fetch して比較する。fetch・比較・
    SHA取得のいずれかに失敗した場合、または差分がない場合は、実コミットを証明でき
    ないため安全側の ``None`` を返す。
    """
    try:
        remote_branch = fetch_remote_branch(repository_root, branch)
        remote_base = fetch_remote_branch(
            repository_root,
            normalize_remote_branch_name(base_branch),
        )
        result = run_git(
            ["rev-list", "--count", f"{remote_base}..{remote_branch}"],
            cwd=repository_root,
            check=True,
        )
        if int(result.stdout.strip() or "0") == 0:
            return None
        sha_result = run_git(
            ["rev-parse", remote_branch], cwd=repository_root, check=True
        )
        return sha_result.stdout.strip() or None
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        print(
            f"Warning: failed to check remote branch {branch!r} against "
            f"{base_branch!r}: {exc}",
            file=sys.stderr,
        )
        return None


def remove_worktree(worktree_path: str | Path) -> None:
    """#193: 完了したworktreeを撤去する。既に手動削除済み等の失敗は無視する
    （run_stateからのクオータ解放を妨げないことを優先する）。"""
    try:
        run_git(["worktree", "remove", str(worktree_path)], cwd=None, check=True)
    except (subprocess.CalledProcessError, OSError):
        pass


def prune_stale_integration_temp_branches(
    repository_root: str | Path,
    *,
    forge: Forge | None = None,
    now: float | None = None,
    max_age_seconds: float = 24 * 60 * 60,
) -> list[str]:
    """PRに紐づかない古い ``integration/temp-*`` remote branchを回収する。

    作成直後の並行runを削除しないよう、指定時間より古く、かつopen PRのhead
    ではないbranchだけを対象にする。列挙に失敗した場合は何も削除しない。
    """
    forge = forge or GitHubForge()
    root = Path(repository_root)
    try:
        run_git(
            [
                "fetch",
                "--prune",
                "origin",
                "+refs/heads/integration/temp-*:refs/remotes/origin/integration/temp-*",
            ],
            cwd=root,
            check=True,
        )
        refs = run_git(
            [
                "for-each-ref",
                "--format=%(refname:short) %(committerdate:unix)",
                "refs/remotes/origin/integration/temp-",
            ],
            cwd=root,
            check=True,
        )
        protected_heads = {pr.head_ref for pr in forge.list_open_prs()}
    except Exception as error:
        print(
            f"Warning: Failed to enumerate stale integration temp branches: {error}",
            file=sys.stderr,
        )
        return []

    cutoff = (time.time() if now is None else now) - max_age_seconds
    deleted: list[str] = []
    for line in refs.stdout.splitlines():
        try:
            remote_name, timestamp = line.rsplit(maxsplit=1)
            branch = remote_name.removeprefix("origin/")
            if not branch.startswith("integration/temp-"):
                continue
            if branch in protected_heads or float(timestamp) > cutoff:
                continue
        except (TypeError, ValueError):
            continue

        try:
            forge.delete_branch(branch)
            deleted.append(branch)
        except Exception as error:
            print(
                f"Warning: Failed to delete stale integration branch '{branch}': {error}",
                file=sys.stderr,
            )
    return deleted
