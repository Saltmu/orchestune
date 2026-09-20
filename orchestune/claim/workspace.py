"""Workspace identity and root path resolution for task claim operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orchestune.infra.git_cli import get_git_repository_paths


@dataclass(frozen=True)
class ClaimWorkspace:
    """Canonical workspace identity and paths for task claim operations."""

    repository_identity: str
    run_state_path: Path
    lock_path: Path
    worktree_root: Path
    repository_root: Path
    common_dir: Path


def _resolve_relative_to(
    primary_root: Path, explicit_path: str | Path | None, default: str
) -> Path:
    if explicit_path is None:
        return primary_root / default
    path_obj = Path(explicit_path)
    return (
        path_obj.resolve()
        if path_obj.is_absolute()
        else (primary_root / path_obj).resolve()
    )


def resolve_claim_workspace(
    cwd: str | Path | None = None,
    *,
    explicit_state_path: str | Path | None = None,
    explicit_worktree_root: str | Path | None = None,
) -> ClaimWorkspace:
    """Resolve the repository root and shared state directory for claim operations.

    Resolves primary checkout, linked worktrees, and subdirectories to the same
    canonical repository identity, run_state.json path, and run_state.lock path.

    `explicit_worktree_root`（#943レビュー対応(Codex P1, round4)）: dispatchは
    `DispatcherConfig.worktree_root`をデフォルト値（`<repo>/worktrees`）以外へ
    設定できる。指定しない場合、claimは既定値へ固定してしまい、実際にagentが
    起動されるディレクトリと、dispatch自身のjournal復元・GCが参照する
    `config.worktree_root`が食い違ってしまう。

    Note:
        Assumes common_dir is located directly inside the primary checkout
        (e.g., `<primary_root>/.git`). Repositories with detached or external git
        directories are not relocated.
    """
    toplevel, common_dir = get_git_repository_paths(cwd)
    primary_root = common_dir.parent
    repository_identity = common_dir.as_posix()

    run_state_path = _resolve_relative_to(
        primary_root, explicit_state_path, "run_state.json"
    )
    lock_path = run_state_path.with_suffix(".lock")
    worktree_root = _resolve_relative_to(
        primary_root, explicit_worktree_root, "worktrees"
    )

    return ClaimWorkspace(
        repository_identity=repository_identity,
        run_state_path=run_state_path,
        lock_path=lock_path,
        worktree_root=worktree_root,
        repository_root=toplevel,
        common_dir=common_dir,
    )


def check_repository_identity_match(
    expected_identity: str,
    recorded_identity: str | None,
) -> bool:
    """Check if the recorded repository identity matches the current workspace.

    A None recorded_identity is treated as compatible (legacy / uninitialized state).
    """
    if recorded_identity is None:
        return True
    return expected_identity == recorded_identity


def assert_repository_identity_match(
    expected_identity: str,
    recorded_identity: str | None,
) -> None:
    """Assert that the recorded repository identity matches the current workspace.

    Raises:
        ValueError: If recorded_identity is not None and does not match expected_identity.
    """
    if not check_repository_identity_match(expected_identity, recorded_identity):
        raise ValueError(
            f"Repository identity mismatch: expected '{expected_identity}', got '{recorded_identity}'"
        )
