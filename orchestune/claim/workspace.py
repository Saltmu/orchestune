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


def _resolve_primary_root(toplevel: Path, common_dir: Path) -> Path:
    """Return the primary checkout root for normal and linked worktrees.

    ``git-common-dir`` points at the primary checkout's ``.git`` directory for
    linked worktrees. For submodules and repositories created with
    ``--separate-git-dir`` it points elsewhere, so their checkout ``toplevel``
    remains the only safe root for relative shared paths.
    """
    git_marker = toplevel / ".git"
    if not git_marker.is_file():
        return toplevel
    try:
        marker = git_marker.read_text(encoding="utf-8").strip()
        prefix, _, raw_git_dir = marker.partition(":")
        if prefix != "gitdir" or not raw_git_dir.strip():
            return toplevel
        git_dir = Path(raw_git_dir.strip())
        if not git_dir.is_absolute():
            git_dir = (toplevel / git_dir).resolve()
        else:
            git_dir = git_dir.resolve()
    except OSError:
        return toplevel

    worktree_metadata = common_dir / "worktrees"
    if git_dir.is_relative_to(worktree_metadata):
        configured_worktree = _read_core_worktree(common_dir, toplevel)
        if configured_worktree is not None:
            return configured_worktree
        if common_dir.name == ".git":
            return common_dir.parent
    return toplevel


def _read_core_worktree(common_dir: Path, toplevel: Path) -> Path | None:
    """Read an optional external-git-dir ``core.worktree`` declaration."""
    config_path = common_dir / "config"
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    in_core = False
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_core = line[1:-1].strip().lower() == "core"
            continue
        if not in_core or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key.lower() != "worktree" or not value:
            continue
        worktree = Path(value)
        return (
            (toplevel / worktree).resolve()
            if not worktree.is_absolute()
            else worktree.resolve()
        )
    return None


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

    Relative shared paths use the primary checkout for linked worktrees. For
    submodules and external git directories, they use the checkout returned by
    ``git rev-parse --show-toplevel`` so separate repositories do not share state.
    """
    toplevel, common_dir = get_git_repository_paths(cwd)
    primary_root = _resolve_primary_root(toplevel, common_dir)
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
