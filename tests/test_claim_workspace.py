"""Tests for claim workspace identity, common-dir resolution, and run_state paths (#934)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestune.claim.workspace import (
    ClaimWorkspace,
    assert_repository_identity_match,
    check_repository_identity_match,
    resolve_claim_workspace,
)
from orchestune.infra.git_cli import run_git


@pytest.fixture
def git_repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Create a primary git repo and a linked worktree for testing."""
    primary_repo = tmp_path / "primary_repo"
    primary_repo.mkdir()

    # git init
    run_git(["init", "-b", "main"], cwd=primary_repo)
    run_git(["config", "user.name", "Test User"], cwd=primary_repo)
    run_git(["config", "user.email", "test@example.com"], cwd=primary_repo)

    # Initial commit so HEAD exists
    (primary_repo / "README.md").write_text("hello")
    run_git(["add", "README.md"], cwd=primary_repo)
    run_git(["commit", "-m", "initial commit"], cwd=primary_repo)

    # Create a linked worktree
    linked_worktree = tmp_path / "linked_worktree"
    run_git(
        ["worktree", "add", "-b", "feat/test", str(linked_worktree), "main"],
        cwd=primary_repo,
    )

    return primary_repo, linked_worktree


class TestResolveClaimWorkspace:
    def test_resolve_from_primary_checkout(
        self, git_repo_with_worktree: tuple[Path, Path]
    ) -> None:
        primary_repo, _ = git_repo_with_worktree
        ws = resolve_claim_workspace(cwd=primary_repo)

        assert isinstance(ws, ClaimWorkspace)
        assert ws.repository_root == primary_repo.resolve()
        assert ws.common_dir == (primary_repo / ".git").resolve()
        assert ws.repository_identity == (primary_repo / ".git").resolve().as_posix()
        assert ws.run_state_path == primary_repo.resolve() / "run_state.json"
        assert ws.lock_path == primary_repo.resolve() / "run_state.lock"
        assert ws.worktree_root == primary_repo.resolve() / "worktrees"

    def test_resolve_from_linked_worktree(
        self, git_repo_with_worktree: tuple[Path, Path]
    ) -> None:
        primary_repo, linked_worktree = git_repo_with_worktree
        ws_primary = resolve_claim_workspace(cwd=primary_repo)
        ws_linked = resolve_claim_workspace(cwd=linked_worktree)

        # Linked worktree must resolve to the exact same repository identity, run_state_path, lock_path, and worktree_root
        assert ws_linked.repository_identity == ws_primary.repository_identity
        assert ws_linked.common_dir == ws_primary.common_dir
        assert ws_linked.run_state_path == ws_primary.run_state_path
        assert ws_linked.lock_path == ws_primary.lock_path
        assert ws_linked.worktree_root == ws_primary.worktree_root

        # Only repository_root distinguishes the current checkout
        assert ws_linked.repository_root == linked_worktree.resolve()

    def test_resolve_from_subdirectory(
        self, git_repo_with_worktree: tuple[Path, Path]
    ) -> None:
        primary_repo, linked_worktree = git_repo_with_worktree
        sub_primary = primary_repo / "subdir" / "nested"
        sub_primary.mkdir(parents=True)
        sub_linked = linked_worktree / "pkg" / "inner"
        sub_linked.mkdir(parents=True)

        ws_primary = resolve_claim_workspace(cwd=primary_repo)
        ws_from_sub_primary = resolve_claim_workspace(cwd=sub_primary)
        ws_from_sub_linked = resolve_claim_workspace(cwd=sub_linked)

        # All subdirectories must normalize to the canonical paths
        assert ws_from_sub_primary.repository_identity == ws_primary.repository_identity
        assert ws_from_sub_primary.run_state_path == ws_primary.run_state_path
        assert ws_from_sub_primary.lock_path == ws_primary.lock_path
        assert ws_from_sub_primary.worktree_root == ws_primary.worktree_root
        assert ws_from_sub_primary.repository_root == primary_repo.resolve()

        assert ws_from_sub_linked.repository_identity == ws_primary.repository_identity
        assert ws_from_sub_linked.run_state_path == ws_primary.run_state_path
        assert ws_from_sub_linked.lock_path == ws_primary.lock_path
        assert ws_from_sub_linked.worktree_root == ws_primary.worktree_root
        assert ws_from_sub_linked.repository_root == linked_worktree.resolve()

    def test_resolve_with_explicit_relative_state_path(
        self, git_repo_with_worktree: tuple[Path, Path]
    ) -> None:
        primary_repo, linked_worktree = git_repo_with_worktree
        sub_linked = linked_worktree / "some" / "deep" / "dir"
        sub_linked.mkdir(parents=True)

        rel_path = Path("custom_dir/custom_state.json")
        ws_primary = resolve_claim_workspace(
            cwd=primary_repo, explicit_state_path=rel_path
        )
        ws_linked_sub = resolve_claim_workspace(
            cwd=sub_linked, explicit_state_path=rel_path
        )

        # Relative paths must be anchored to primary repository root, NOT to cwd
        expected_path = (primary_repo / rel_path).resolve()
        assert ws_primary.run_state_path == expected_path
        assert ws_linked_sub.run_state_path == expected_path
        assert ws_primary.lock_path == expected_path.with_suffix(".lock")
        assert ws_linked_sub.lock_path == expected_path.with_suffix(".lock")

    def test_resolve_with_explicit_absolute_state_path(
        self, git_repo_with_worktree: tuple[Path, Path], tmp_path: Path
    ) -> None:
        primary_repo, linked_worktree = git_repo_with_worktree
        abs_path = (tmp_path / "shared" / "global_state.json").resolve()

        ws_primary = resolve_claim_workspace(
            cwd=primary_repo, explicit_state_path=abs_path
        )
        ws_linked = resolve_claim_workspace(
            cwd=linked_worktree, explicit_state_path=abs_path
        )

        assert ws_primary.run_state_path == abs_path
        assert ws_linked.run_state_path == abs_path
        assert ws_primary.lock_path == abs_path.with_suffix(".lock")
        assert ws_linked.lock_path == abs_path.with_suffix(".lock")

    def test_resolve_with_explicit_worktree_root(
        self, git_repo_with_worktree: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """#943レビュー対応(Codex P1, round4): dispatchが`--worktree-root`で
        既定値以外を設定した場合、claimも同じディレクトリを使わないと、
        agentの起動先とdispatch自身が参照するディレクトリが食い違う。"""
        primary_repo, linked_worktree = git_repo_with_worktree
        custom_root = (tmp_path / "custom" / "worktrees").resolve()

        ws_primary = resolve_claim_workspace(
            cwd=primary_repo, explicit_worktree_root=custom_root
        )
        ws_linked = resolve_claim_workspace(
            cwd=linked_worktree, explicit_worktree_root=custom_root
        )

        assert ws_primary.worktree_root == custom_root
        assert ws_linked.worktree_root == custom_root

    def test_resolve_outside_git_repository_raises(self, tmp_path: Path) -> None:
        not_a_repo = tmp_path / "not_git"
        not_a_repo.mkdir()

        with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
            resolve_claim_workspace(cwd=not_a_repo)

    def test_resolve_with_external_git_dir_anchors_to_checkout(
        self, tmp_path: Path
    ) -> None:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        external_git_dir = tmp_path / "git-dir"
        run_git(
            ["init", "--separate-git-dir", str(external_git_dir), str(checkout)],
            cwd=tmp_path,
        )

        ws = resolve_claim_workspace(cwd=checkout)

        assert ws.repository_root == checkout.resolve()
        assert ws.run_state_path == checkout / "run_state.json"
        assert ws.worktree_root == checkout / "worktrees"

    def test_claim_workspace_is_frozen(
        self, git_repo_with_worktree: tuple[Path, Path]
    ) -> None:
        primary_repo, _ = git_repo_with_worktree
        ws = resolve_claim_workspace(cwd=primary_repo)
        with pytest.raises((AttributeError, TypeError)):
            ws.repository_identity = "modified"  # type: ignore[misc]


class TestRepositoryIdentityValidation:
    def test_check_repository_identity_match(self) -> None:
        ident_a = "/path/to/repo/.git"
        ident_b = "/path/to/other/.git"

        # None recorded (legacy or uninitialized state) is permitted
        assert check_repository_identity_match(ident_a, None) is True

        # Matching identity
        assert check_repository_identity_match(ident_a, ident_a) is True

        # Mismatch
        assert check_repository_identity_match(ident_a, ident_b) is False

    def test_assert_repository_identity_match(self) -> None:
        ident_a = "/path/to/repo/.git"
        ident_b = "/path/to/other/.git"

        # Should not raise when matched or None
        assert_repository_identity_match(ident_a, None)
        assert_repository_identity_match(ident_a, ident_a)

        # Should raise on mismatch (fail-closed)
        with pytest.raises(ValueError, match="Repository identity mismatch"):
            assert_repository_identity_match(ident_a, ident_b)
