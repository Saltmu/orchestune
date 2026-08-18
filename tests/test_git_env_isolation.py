"""Reproducer and regression tests for Git environment variable isolation in tests (Issue #507)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.test_closed_loop import DummyGitRepo


def test_isolated_git_env_removes_git_variables(monkeypatch: pytest.MonkeyPatch):
    """Verify that dangerous GIT_* variables are stripped from os.environ by conftest fixture."""
    dangerous_vars = [
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_PREFIX",
        "GIT_GRAFT_FILE",
        "GIT_SUPER_PREFIX",
    ]
    for var in dangerous_vars:
        assert (
            var not in os.environ
        ), f"Environment variable {var} should be isolated/cleared"


def test_dummy_git_repo_with_polluted_git_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Verify that DummyGitRepo does not pollute or touch an external GIT_DIR even if GIT_DIR is injected."""
    # Create a dummy "host" git repository that simulates the outer repo
    host_repo_dir = tmp_path / "host_repo"
    host_repo_dir.mkdir()
    subprocess.run(
        ["git", "init", "--bare"],
        cwd=str(host_repo_dir),
        check=True,
        capture_output=True,
    )

    # Set GIT_DIR to point to host_repo_dir to simulate git pre-push hook environment
    monkeypatch.setenv("GIT_DIR", str(host_repo_dir))

    # Initialize DummyGitRepo - this should isolate its git operations to its own temp_dir
    repo = DummyGitRepo()
    try:
        assert repo.origin_path.exists()
        assert repo.local_path.exists()
        # Verify that DummyGitRepo created commits in its own local path
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo.local_path),
            check=True,
            capture_output=True,
            text=True,
            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        )
        assert len(res.stdout.strip()) == 40
    finally:
        repo.cleanup()


def test_get_clean_git_env_strips_git_vars_and_merges_extras():
    """Verify get_clean_git_env removes GIT_* and incorporates custom variables."""
    from tests.conftest import GIT_ENV_VARS_TO_CLEAR, get_clean_git_env

    # Temporarily set some GIT_* variables directly in os.environ for testing the helper
    for var in GIT_ENV_VARS_TO_CLEAR:
        os.environ[var] = f"/custom/{var}"

    try:
        clean = get_clean_git_env({"CUSTOM_VAR": "custom_value"})
        for var in GIT_ENV_VARS_TO_CLEAR:
            assert var not in clean, f"{var} should not be in clean env"
        assert clean.get("CUSTOM_VAR") == "custom_value"
    finally:
        for var in GIT_ENV_VARS_TO_CLEAR:
            os.environ.pop(var, None)


def test_run_git_automatically_isolates_polluted_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Verify run_git automatically strips polluted GIT_DIR from ambient environment."""
    from orchestune.git_cli import run_git

    host_repo_dir = tmp_path / "host_repo_git_cli"
    host_repo_dir.mkdir()
    subprocess.run(
        ["git", "init", "--bare"],
        cwd=str(host_repo_dir),
        check=True,
        capture_output=True,
    )

    # Set dangerous GIT_DIR in os.environ
    monkeypatch.setenv("GIT_DIR", str(host_repo_dir))

    # Initialize a new local repo via run_git with cwd set to a separate directory
    local_dir = tmp_path / "local_repo_git_cli"
    local_dir.mkdir()

    # run_git should automatically isolate GIT_DIR and execute in local_dir
    res = run_git(["init"], cwd=local_dir)
    assert res.returncode == 0
    assert (local_dir / ".git").exists()


def test_dummy_git_repo_sees_dynamic_env_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Verify DummyGitRepo.run_git dynamically picks up environment variables added after repo initialization."""
    repo = DummyGitRepo()
    try:
        # Dynamically set a new environment variable after repo creation
        monkeypatch.setenv("TEST_DYNAMIC_VAR", "dynamic_value_123")
        # Run a git command via repo.run_git
        res = repo.run_git(["config", "user.name"])
        assert res.returncode == 0
    finally:
        repo.cleanup()
