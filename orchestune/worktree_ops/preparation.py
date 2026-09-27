"""Safe worktree preparation shared by claim and dispatch workflows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestune.infra.git_cli import GitResult, resolve_local_or_remote_branch, run_git
from orchestune.infra.process_utils import file_lock
from orchestune.validation import validate_ref_name
from orchestune.worktree_ops.claim_marker import (
    claim_lock_path,
    read_claim_marker,
    write_claim_marker,
)


@dataclass(frozen=True)
class WorktreePreparation:
    """Result of preparing a worktree, used to decide whether rollback is safe."""

    worktree_path: Path
    branch: str
    accepted: bool
    created: bool = False
    branch_created: bool = False
    base_sha: str | None = None
    rejection_reason: str | None = None


def _branch_exists(
    branch_name: str,
    cwd: str | Path | None = None,
    *,
    git_runner: Callable[..., GitResult] | None = None,
) -> bool:
    runner = run_git if git_runner is None else git_runner
    local = runner(
        ["show-ref", "--verify", f"refs/heads/{branch_name}"], cwd=cwd, check=False
    )
    if local.returncode == 0:
        return True
    remote = runner(
        ["show-ref", "--verify", f"refs/remotes/origin/{branch_name}"],
        cwd=cwd,
        check=False,
    )
    return remote.returncode == 0


def _resolve_worktree_path(worktree_root: str | Path, branch_name: str) -> Path:
    validate_ref_name(branch_name)
    return Path(worktree_root) / branch_name.replace("/", "-")


def _create_worktree(
    worktree_path: Path,
    worktree_root: Path,
    branch_name: str,
    base_branch: str | None = None,
    cwd: str | Path | None = None,
    *,
    git_runner: Callable[..., GitResult] | None = None,
    branch_exists: Callable[..., bool] | None = None,
    branch_resolver: Callable[..., str] | None = None,
) -> None:
    runner = run_git if git_runner is None else git_runner
    exists = _branch_exists if branch_exists is None else branch_exists
    resolve_branch = (
        resolve_local_or_remote_branch if branch_resolver is None else branch_resolver
    )
    runner(["worktree", "prune"], cwd=cwd, check=False)
    worktree_root.mkdir(parents=True, exist_ok=True)
    if exists(branch_name, cwd=cwd):
        command = ["worktree", "add", str(worktree_path), branch_name]
    else:
        command = ["worktree", "add", "-b", branch_name, str(worktree_path)]
        if base_branch:
            resolved_base = resolve_branch(
                cwd or ".",
                base_branch,
                prefer_remote=base_branch.startswith("parent/"),
            )
            command.append(resolved_base)
    runner(command, cwd=cwd, check=True)


def _resolve_worktree_head_sha(
    worktree_path: Path, *, git_runner: Callable[..., GitResult] | None = None
) -> str:
    runner = run_git if git_runner is None else git_runner
    return runner(["rev-parse", "HEAD"], cwd=worktree_path, check=True).stdout.strip()


def _worktree_checked_out_branch(
    worktree_path: Path, *, git_runner: Callable[..., GitResult] | None = None
) -> str | None:
    runner = run_git if git_runner is None else git_runner
    try:
        result = runner(
            ["symbolic-ref", "--short", "HEAD"], cwd=worktree_path, check=False
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_common_dir(
    path: Path, *, git_runner: Callable[..., GitResult] | None = None
) -> str | None:
    runner = run_git if git_runner is None else git_runner
    try:
        result = runner(["rev-parse", "--git-common-dir"], cwd=path, check=False)
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return str((path / result.stdout.strip()).resolve())


def _git_toplevel(
    path: Path, *, git_runner: Callable[..., GitResult] | None = None
) -> str | None:
    runner = run_git if git_runner is None else git_runner
    try:
        result = runner(["rev-parse", "--show-toplevel"], cwd=path, check=False)
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return str(Path(result.stdout.strip()).resolve())


def _verify_worktree_identity(
    worktree_path: Path,
    branch: str,
    repository_root: str | Path | None = None,
    *,
    git_runner: Callable[..., GitResult] | None = None,
) -> bool:
    if _worktree_checked_out_branch(worktree_path, git_runner=git_runner) != branch:
        return False
    if _git_toplevel(worktree_path, git_runner=git_runner) != str(
        worktree_path.resolve()
    ):
        return False
    root_git_dir = _git_common_dir(
        Path(repository_root) if repository_root else Path("."),
        git_runner=git_runner,
    )
    return root_git_dir is not None and root_git_dir == _git_common_dir(
        worktree_path, git_runner=git_runner
    )


def _create_and_claim_worktree(
    worktree_path: Path,
    worktree_root: Path,
    branch: str,
    base_branch: str | None,
    claim_id: str,
    cwd: str | Path | None = None,
) -> WorktreePreparation:
    branch_created = not _branch_exists(branch, cwd=cwd)
    _create_worktree(worktree_path, worktree_root, branch, base_branch, cwd=cwd)
    base_sha = _resolve_worktree_head_sha(worktree_path)
    write_claim_marker(
        worktree_path,
        claim_id=claim_id,
        branch=branch,
        base_sha=base_sha,
        branch_created=branch_created,
    )
    return WorktreePreparation(
        worktree_path=worktree_path,
        branch=branch,
        accepted=True,
        created=True,
        branch_created=branch_created,
        base_sha=base_sha,
    )


def _prepare_worktree_from_marker(
    worktree_path: Path,
    worktree_root: Path,
    branch: str,
    base_branch: str | None,
    marker: dict[str, Any],
    cwd: str | Path | None = None,
) -> WorktreePreparation:
    claim_id = marker["claim_id"]
    base_sha = marker.get("base_sha")
    if worktree_path.exists():
        if not _verify_worktree_identity(worktree_path, branch, repository_root=cwd):
            return WorktreePreparation(
                worktree_path=worktree_path,
                branch=branch,
                accepted=False,
                rejection_reason="stale_marker_unverified_worktree",
            )
        return WorktreePreparation(
            worktree_path=worktree_path,
            branch=branch,
            accepted=True,
            created=False,
            base_sha=base_sha,
        )
    if not _branch_exists(branch, cwd=cwd):
        return _create_and_claim_worktree(
            worktree_path, worktree_root, branch, base_branch, claim_id, cwd=cwd
        )
    run_git(["worktree", "prune"], cwd=cwd, check=False)
    worktree_root.mkdir(parents=True, exist_ok=True)
    run_git(["worktree", "add", str(worktree_path), branch], cwd=cwd, check=True)
    write_claim_marker(
        worktree_path,
        claim_id=claim_id,
        branch=branch,
        base_sha=base_sha,
        branch_created=False,
    )
    return WorktreePreparation(
        worktree_path=worktree_path,
        branch=branch,
        accepted=True,
        created=True,
        branch_created=False,
        base_sha=base_sha,
    )


def _prepare_worktree_unclaimed(
    worktree_path: Path,
    branch: str,
    worktree_root: Path,
    base_branch: str | None,
    claim_id: str,
    cwd: str | Path | None = None,
    *,
    trust_unclaimed_branch: bool = False,
) -> WorktreePreparation:
    if worktree_path.exists():
        return WorktreePreparation(
            worktree_path=worktree_path,
            branch=branch,
            accepted=False,
            rejection_reason="unclaimed_existing_worktree",
        )
    if _branch_exists(branch, cwd=cwd):
        if not trust_unclaimed_branch:
            return WorktreePreparation(
                worktree_path=worktree_path,
                branch=branch,
                accepted=False,
                rejection_reason="unclaimed_existing_branch",
            )
        run_git(["worktree", "prune"], cwd=cwd, check=False)
        worktree_root.mkdir(parents=True, exist_ok=True)
        run_git(["worktree", "add", str(worktree_path), branch], cwd=cwd, check=True)
        base_sha = _resolve_worktree_head_sha(worktree_path)
        write_claim_marker(
            worktree_path,
            claim_id=claim_id,
            branch=branch,
            base_sha=base_sha,
            branch_created=False,
        )
        return WorktreePreparation(
            worktree_path=worktree_path,
            branch=branch,
            accepted=True,
            created=True,
            branch_created=False,
            base_sha=base_sha,
        )
    return _create_and_claim_worktree(
        worktree_path, worktree_root, branch, base_branch, claim_id, cwd=cwd
    )


def prepare_task_worktree(
    branch: str,
    worktree_root: str | Path,
    base_branch: str | None,
    claim_id: str,
    *,
    cwd: str | Path | None = None,
    trust_unclaimed_branch: bool = False,
) -> WorktreePreparation:
    """Prepare a claim-owned worktree without force-removing unknown state."""
    worktree_root_path = Path(worktree_root)
    worktree_path = _resolve_worktree_path(worktree_root_path, branch)
    with file_lock(claim_lock_path(worktree_path)):
        marker = read_claim_marker(worktree_path)
        if marker is not None:
            if marker.get("claim_id") != claim_id or marker.get("branch") != branch:
                return WorktreePreparation(
                    worktree_path=worktree_path,
                    branch=branch,
                    accepted=False,
                    rejection_reason="claim_id_mismatch",
                )
            return _prepare_worktree_from_marker(
                worktree_path, worktree_root_path, branch, base_branch, marker, cwd=cwd
            )
        return _prepare_worktree_unclaimed(
            worktree_path,
            branch,
            worktree_root_path,
            base_branch,
            claim_id,
            cwd=cwd,
            trust_unclaimed_branch=trust_unclaimed_branch,
        )
