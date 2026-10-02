"""Local claim generation checks; these are concurrency guards, not authentication."""

from __future__ import annotations

from pathlib import Path

from orchestune.claim.workspace import resolve_claim_workspace
from orchestune.infra.git_cli import run_git
from orchestune.ledger.run_state import ActiveWorktree
from orchestune.worktree_ops.claim_marker import read_claim_marker


def caller_claim_id(cwd: str | Path | None = None) -> str | None:
    """Read the caller's generation independently of the current ledger."""
    root = resolve_claim_workspace(cwd).repository_root
    marker = read_claim_marker(root)
    value = marker.get("claim_id") if marker else None
    return value if isinstance(value, str) and value.strip() else None


def registered_claim_path(active: ActiveWorktree, state_path: Path) -> Path:
    path = Path(active.core.worktree_path)
    return path if path.is_absolute() else state_path.parent / path


def validate_claim_worktree(
    active: ActiveWorktree, state_path: Path, *, require_marker: bool = True
) -> Path:
    """Verify the registered Git checkout and, normally, its generation marker."""
    if not active.core.worktree_path:
        raise ValueError(
            "Claim has no worktree; inspect it with orchestune recover --issue "
            + str(active.core.issue_number)
        )
    path = registered_claim_path(active, state_path)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Registered claim worktree is missing or a symlink")
    workspace = resolve_claim_workspace(path, explicit_state_path=state_path)
    if active.claim.repository_id != workspace.repository_identity:
        raise ValueError("Claim repository identity differs")
    if workspace.repository_root.resolve() != path.resolve():
        raise ValueError("Claim path is not a Git worktree root")
    branch = run_git(
        ["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=path, check=False
    )
    if branch.returncode or branch.stdout.strip() != active.core.branch:
        raise ValueError("Claim branch differs from the checked out branch")
    if require_marker:
        marker = read_claim_marker(path)
        if (
            not marker
            or marker.get("claim_id") != active.claim.claim_id
            or marker.get("branch") != active.core.branch
        ):
            raise ValueError(
                "Claim marker is missing or belongs to another generation; "
                f"run orchestune recover --issue {active.core.issue_number}"
            )
        if active.claim.base_sha and marker.get("base_sha") != active.claim.base_sha:
            raise ValueError("Claim marker base differs")
    return path


def validate_local_claim(
    active: ActiveWorktree,
    expected_claim_id: str | None,
    *,
    cwd: str | Path | None = None,
    state_path: Path,
    allow_primary: bool = False,
    allow_unprepared: bool = False,
) -> None:
    """Bind a caller's expected generation to the registered repository/worktree."""
    if not expected_claim_id or expected_claim_id != active.claim.claim_id:
        raise ValueError("Claim generation differs or was not supplied")
    workspace = resolve_claim_workspace(cwd, explicit_state_path=state_path)
    if active.claim.repository_id != workspace.repository_identity:
        raise ValueError("Claim belongs to another repository")
    target = registered_claim_path(active, state_path)
    caller = workspace.repository_root.resolve()
    is_primary = caller == workspace.common_dir.parent.resolve()
    if caller != target.resolve() and not (allow_primary and is_primary):
        raise ValueError("Operation must run from the claimed worktree")
    if (
        allow_unprepared
        and active.claim.claim_stage == "reserved"
        and (not active.core.worktree_path or not target.exists())
    ):
        if not is_primary:
            raise ValueError("Unprepared claim must resume from the primary checkout")
        return
    validate_claim_worktree(active, state_path)
    if caller == target.resolve() and caller_claim_id(cwd) != expected_claim_id:
        raise ValueError("Caller marker does not match the expected generation")
