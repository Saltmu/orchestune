"""GCプロセスおよび完了時のGit・Worktree操作に関連するヘルパー関数群。"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orchestune.dispatch.claim_marker import read_claim_marker, remove_claim_marker
from orchestune.forge import Forge, GitHubForge
from orchestune.infra.git_cli import (
    fetch_remote_branch,
    normalize_remote_branch_name,
    resolve_local_or_remote_branch,
    run_git,
)

if TYPE_CHECKING:
    from orchestune.dispatch.state import ActiveWorktree


@dataclass(frozen=True)
class VerifiedWorktreeRemovalRequest:
    """安全条件の検証を通過した物理worktree削除要求。"""

    worktree_path: Path
    branch: str | None = None
    claim_id: str | None = None


@dataclass(frozen=True)
class WorktreeRemovalEvaluation:
    """worktree削除安全条件の評価結果。"""

    can_remove: bool
    rejection_reason: str | None = None
    request: VerifiedWorktreeRemovalRequest | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorktreeRemovalResult:
    """検証済みworktree削除の実行結果。"""

    success: bool
    removed: bool
    rejection_reason: str | None = None
    error: str | None = None


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


def _parse_worktree_list_porcelain(output: str) -> list[dict[str, str]]:
    worktrees: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line:
            if current:
                worktrees.append(current)
                current = {}
            continue
        parts = line.split(maxsplit=1)
        key = parts[0]
        val = parts[1] if len(parts) > 1 else ""
        current[key] = val
    if current:
        worktrees.append(current)
    return worktrees


def _is_primary_worktree(
    resolved_target: Path,
    repo_root: str | Path | None,
) -> bool:
    resolved_root = None
    if repo_root is not None:
        resolved_root = Path(repo_root).resolve()
    try:
        toplevel_res = run_git(["rev-parse", "--show-toplevel"], cwd=None, check=True)
        toplevel = Path(toplevel_res.stdout.strip()).resolve()
        if resolved_root is None:
            resolved_root = toplevel
    except (subprocess.CalledProcessError, OSError):
        pass

    return resolved_root is not None and resolved_target == resolved_root


def _find_registered_entry(
    target_path: Path,
    resolved_target: Path,
    entries: list[dict[str, str]],
) -> dict[str, str] | None:
    for entry in entries:
        raw = entry.get("worktree")
        if not raw:
            continue
        if raw == str(target_path) or Path(raw).resolve() == resolved_target:
            return entry
    return None


def _check_branch_and_owner_mismatch(
    branch: str | None,
    claim_id: str | None,
    matched_entry: dict[str, str],
    marker: dict[str, Any] | None,
) -> str | None:
    registered_ref = matched_entry.get("branch", "")
    registered_branch = (
        registered_ref.removeprefix("refs/heads/") if registered_ref else None
    )
    if (
        branch is not None
        and registered_branch is not None
        and registered_branch != branch
    ):
        return "branch_mismatch"

    if marker is not None:
        marker_branch = marker.get("branch")
        if branch is not None and marker_branch is not None and marker_branch != branch:
            return "branch_mismatch"
        if (
            registered_branch is not None
            and marker_branch is not None
            and registered_branch != marker_branch
        ):
            return "branch_mismatch"

        marker_claim_id = marker.get("claim_id")
        if (
            claim_id is not None
            and marker_claim_id is not None
            and marker_claim_id != claim_id
        ):
            return "owner_mismatch"

    return None


def _check_worktree_dirty(
    target_path: Path,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    if not target_path.exists():
        return True, None, None
    try:
        status_res = run_git(["status", "--porcelain"], cwd=target_path, check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        return False, "git_status_failed", {"error": str(exc)}
    if status_res.stdout.strip():
        return False, "dirty_worktree", None
    return True, None, None


def _resolve_target_info(
    active: ActiveWorktree | None,
    worktree_path: str | Path | None,
    expected_branch: str | None,
    expected_claim_id: str | None,
) -> tuple[Path, str | None, str | None]:
    branch: str | None
    claim_id: str | None
    if active is not None:
        target_path = Path(active.worktree_path)
        branch = active.branch if expected_branch is None else expected_branch
        claim_id = active.claim_id if expected_claim_id is None else expected_claim_id
    elif worktree_path is not None:
        target_path = Path(worktree_path)
        branch = expected_branch
        claim_id = expected_claim_id
    else:
        raise ValueError("Either active or worktree_path must be provided")
    return target_path, branch, claim_id


def _validate_worktree_registration(
    target_path: Path,
    resolved_target: Path,
    repo_root: str | Path | None,
) -> tuple[dict[str, str] | None, WorktreeRemovalEvaluation | None]:
    if _is_primary_worktree(resolved_target, repo_root):
        return None, WorktreeRemovalEvaluation(
            can_remove=False,
            rejection_reason="primary_worktree",
        )
    try:
        list_res = run_git(["worktree", "list", "--porcelain"], cwd=None, check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        return None, WorktreeRemovalEvaluation(
            can_remove=False,
            rejection_reason="git_worktree_list_failed",
            details={"error": str(exc)},
        )

    entries = _parse_worktree_list_porcelain(list_res.stdout)
    if entries and resolved_target == Path(entries[0].get("worktree", "")).resolve():
        return None, WorktreeRemovalEvaluation(
            can_remove=False,
            rejection_reason="primary_worktree",
        )

    matched_entry = _find_registered_entry(target_path, resolved_target, entries)
    if matched_entry is None:
        return None, WorktreeRemovalEvaluation(
            can_remove=False,
            rejection_reason="unregistered_worktree",
        )

    registered_raw = matched_entry.get("worktree", "")
    if (
        target_path.is_symlink()
        or Path(registered_raw).is_symlink()
        or resolved_target != Path(registered_raw).resolve()
    ):
        return None, WorktreeRemovalEvaluation(
            can_remove=False,
            rejection_reason="symlink_mismatch",
        )

    return matched_entry, None


def evaluate_worktree_removal(
    active: ActiveWorktree | None = None,
    *,
    worktree_path: str | Path | None = None,
    expected_branch: str | None = None,
    expected_claim_id: str | None = None,
    repo_root: str | Path | None = None,
) -> WorktreeRemovalEvaluation:
    """#1004: worktree 削除前の安全条件（primary, 未登録, symlink不一致, branch不一致, dirty, owner不一致）を検証する。"""
    target_path, branch, claim_id = _resolve_target_info(
        active, worktree_path, expected_branch, expected_claim_id
    )
    resolved_target = target_path.resolve()

    matched_entry, eval_err = _validate_worktree_registration(
        target_path, resolved_target, repo_root
    )
    if eval_err is not None or matched_entry is None:
        assert eval_err is not None
        return eval_err

    marker = read_claim_marker(target_path)
    if mismatch_reason := _check_branch_and_owner_mismatch(
        branch, claim_id, matched_entry, marker
    ):
        return WorktreeRemovalEvaluation(
            can_remove=False,
            rejection_reason=mismatch_reason,
        )

    is_clean, dirty_reason, dirty_details = _check_worktree_dirty(target_path)
    if not is_clean:
        return WorktreeRemovalEvaluation(
            can_remove=False,
            rejection_reason=dirty_reason,
            details=dirty_details or {},
        )

    return WorktreeRemovalEvaluation(
        can_remove=True,
        rejection_reason=None,
        request=VerifiedWorktreeRemovalRequest(
            worktree_path=target_path,
            branch=branch,
            claim_id=claim_id,
        ),
    )


def remove_verified_worktree(
    request: VerifiedWorktreeRemovalRequest,
) -> WorktreeRemovalResult:
    """#1004: 検証済みworktree削除要求に基づき物理削除を実行する。"""
    path = request.worktree_path
    try:
        run_git(["worktree", "remove", str(path)], cwd=None, check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        return WorktreeRemovalResult(
            success=False,
            removed=False,
            error=str(exc),
        )
    if path.exists():
        return WorktreeRemovalResult(
            success=False,
            removed=False,
            error="Worktree directory still exists after git worktree remove",
        )
    remove_claim_marker(path)
    return WorktreeRemovalResult(success=True, removed=True)


def remove_worktree(
    worktree_path: str | Path,
    *,
    active: ActiveWorktree | None = None,
    expected_branch: str | None = None,
    expected_claim_id: str | None = None,
    repo_root: str | Path | None = None,
) -> WorktreeRemovalResult:
    """#193 / #1004: 完了したworktreeを検証した上で安全に撤去する。"""
    path = Path(worktree_path)
    eval_res = evaluate_worktree_removal(
        active=active,
        worktree_path=path,
        expected_branch=expected_branch,
        expected_claim_id=expected_claim_id,
        repo_root=repo_root,
    )
    if not eval_res.can_remove or eval_res.request is None:
        return WorktreeRemovalResult(
            success=False,
            removed=False,
            rejection_reason=eval_res.rejection_reason,
            error=str(eval_res.details) if eval_res.details else None,
        )
    return remove_verified_worktree(eval_res.request)


def _list_remote_temp_refs(root: Path, forge: Forge) -> tuple[str, set[str]] | None:
    """remoteのintegration/temp-* refsとopen PRの保護head一覧を取得する。"""
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
        return refs.stdout, protected_heads
    except Exception as error:
        print(
            f"Warning: Failed to enumerate stale integration temp branches: {error}",
            file=sys.stderr,
        )
        return None


def _is_stale_temp_branch(
    line: str, protected_heads: set[str], cutoff: float
) -> str | None:
    """refs出力行が削除対象のstale integration branchであればbranch名を返す。"""
    try:
        remote_name, timestamp = line.rsplit(maxsplit=1)
        branch = remote_name.removeprefix("origin/")
        if not branch.startswith("integration/temp-"):
            return None
        if branch in protected_heads or float(timestamp) > cutoff:
            return None
        return branch
    except (TypeError, ValueError):
        return None


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
    ref_info = _list_remote_temp_refs(Path(repository_root), forge)
    if ref_info is None:
        return []

    refs_stdout, protected_heads = ref_info
    cutoff = (time.time() if now is None else now) - max_age_seconds
    deleted: list[str] = []
    for line in refs_stdout.splitlines():
        branch = _is_stale_temp_branch(line, protected_heads, cutoff)
        if branch is None:
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
