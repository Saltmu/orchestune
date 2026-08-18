"""Tests for git hook setup scripts (Issue #507)."""

from __future__ import annotations

import subprocess
from pathlib import Path


def test_setup_git_hooks_ps1_content_contains_git_env_unset():
    """Verify setup-git-hooks.ps1 configures pre-push hook with GIT_* unset and uses rev-parse for hooks dir."""
    script_path = (
        Path(__file__).resolve().parent.parent / "scripts" / "setup-git-hooks.ps1"
    )
    content = script_path.read_text(encoding="utf-8")

    # Should use dynamic hooks dir resolution via git rev-parse
    assert "git rev-parse --git-path hooks" in content or "git rev-parse" in content
    # Should contain git env cleanup in pre-push hook or script
    assert "GIT_DIR" in content


def test_setup_git_hooks_sh_content_contains_git_env_unset():
    """Verify setup-git-hooks.sh configures pre-push hook with GIT_* unset and uses rev-parse for hooks dir."""
    script_path = (
        Path(__file__).resolve().parent.parent / "scripts" / "setup-git-hooks.sh"
    )
    content = script_path.read_text(encoding="utf-8")

    # Should use dynamic hooks dir resolution via git rev-parse
    assert "git rev-parse --git-path hooks" in content or "git rev-parse" in content
    # Should contain git env cleanup in pre-push hook or script
    assert "GIT_DIR" in content


def test_local_ci_scripts_unset_git_env():
    """Verify local-ci scripts unset GIT_* environment variables."""
    ps1_path = Path(__file__).resolve().parent.parent / "scripts" / "local-ci.ps1"
    sh_path = Path(__file__).resolve().parent.parent / "scripts" / "local-ci.sh"

    ps1_content = ps1_path.read_text(encoding="utf-8")
    sh_content = sh_path.read_text(encoding="utf-8")

    assert "GIT_DIR" in ps1_content
    assert "GIT_DIR" in sh_content


def test_scripts_contain_all_dangerous_git_env_vars():
    """Verify all 4 shell/powershell scripts contain every variable from DANGEROUS_GIT_ENV_VARS (SSOT)."""
    from orchestune.git_cli import DANGEROUS_GIT_ENV_VARS

    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    target_scripts = [
        "local-ci.ps1",
        "local-ci.sh",
        "setup-git-hooks.ps1",
        "setup-git-hooks.sh",
    ]

    for script_name in target_scripts:
        script_file = scripts_dir / script_name
        content = script_file.read_text(encoding="utf-8")
        for var in DANGEROUS_GIT_ENV_VARS:
            assert (
                var in content
            ), f"Variable {var!r} from DANGEROUS_GIT_ENV_VARS is missing in scripts/{script_name}"


def test_git_rev_parse_hooks_in_worktree(tmp_path: Path):
    """Verify git rev-parse --git-path hooks resolves to the common repository hooks directory inside a worktree."""
    from tests.conftest import get_clean_git_env

    clean_env = get_clean_git_env()
    main_repo = tmp_path / "main_repo"
    main_repo.mkdir()

    # Initialize main repo
    subprocess.run(
        ["git", "init"],
        cwd=str(main_repo),
        check=True,
        capture_output=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(main_repo),
        check=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(main_repo),
        check=True,
        env=clean_env,
    )

    # Initial commit so HEAD is valid for worktree creation
    readme = main_repo / "README.md"
    readme.write_text("Hello", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=str(main_repo),
        check=True,
        capture_output=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(main_repo),
        check=True,
        capture_output=True,
        env=clean_env,
    )

    # Create worktree
    worktree_path = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", "feature-wt"],
        cwd=str(main_repo),
        check=True,
        capture_output=True,
        env=clean_env,
    )

    # Check rev-parse in main repo
    res_main = subprocess.run(
        ["git", "rev-parse", "--git-path", "hooks"],
        cwd=str(main_repo),
        check=True,
        capture_output=True,
        text=True,
        env=clean_env,
    )
    hooks_path_main = res_main.stdout.strip()

    # Check rev-parse in worktree
    res_wt = subprocess.run(
        ["git", "rev-parse", "--git-path", "hooks"],
        cwd=str(worktree_path),
        check=True,
        capture_output=True,
        text=True,
        env=clean_env,
    )
    hooks_path_wt = res_wt.stdout.strip()

    # Normalize paths for comparison
    p_main = (
        Path(hooks_path_main)
        if Path(hooks_path_main).is_absolute()
        else (main_repo / hooks_path_main).resolve()
    )
    p_wt = (
        Path(hooks_path_wt)
        if Path(hooks_path_wt).is_absolute()
        else (worktree_path / hooks_path_wt).resolve()
    )

    assert (
        p_main.resolve() == p_wt.resolve()
    ), f"Worktree hooks path ({p_wt}) must resolve to common hooks path ({p_main})"


def test_setup_git_hooks_ps1_execution_in_worktree(tmp_path: Path):
    """Verify setup-git-hooks.ps1 executes successfully inside a worktree and creates hooks in the common hooks dir."""
    import shutil
    import sys

    from tests.conftest import get_clean_git_env

    clean_env = get_clean_git_env()
    main_repo = tmp_path / "repo_ps1"
    main_repo.mkdir()

    # Initialize main repo
    subprocess.run(
        ["git", "init"],
        cwd=str(main_repo),
        check=True,
        capture_output=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(main_repo),
        check=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(main_repo),
        check=True,
        env=clean_env,
    )

    # Initial commit
    readme = main_repo / "README.md"
    readme.write_text("Hello", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=str(main_repo),
        check=True,
        capture_output=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(main_repo),
        check=True,
        capture_output=True,
        env=clean_env,
    )

    # Create worktree
    worktree_path = tmp_path / "wt_ps1"
    subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", "feature-ps1"],
        cwd=str(main_repo),
        check=True,
        capture_output=True,
        env=clean_env,
    )

    # Copy real scripts directory to the worktree
    real_scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    target_scripts_dir = worktree_path / "scripts"
    shutil.copytree(real_scripts_dir, target_scripts_dir)

    # Determine powershell executable
    ps_exe = "powershell.exe" if sys.platform == "win32" else "pwsh"
    if not shutil.which(ps_exe):
        return
    # Execute setup-git-hooks.ps1 from inside the worktree
    res = subprocess.run(
        [
            ps_exe,
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(target_scripts_dir / "setup-git-hooks.ps1"),
        ],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        env=clean_env,
    )
    assert (
        res.returncode == 0
    ), f"setup-git-hooks.ps1 failed: {res.stderr}\n{res.stdout}"

    # Verify hooks exist in the common .git/hooks directory
    common_hooks_dir = main_repo / ".git" / "hooks"
    assert (
        common_hooks_dir / "pre-commit"
    ).exists(), "pre-commit hook was not created in common hooks dir"
    assert (
        common_hooks_dir / "pre-push"
    ).exists(), "pre-push hook was not created in common hooks dir"

    # Verify pre-push content in common hooks contains GIT_* unset
    pre_push_content = (common_hooks_dir / "pre-push").read_text(encoding="utf-8")
    assert "unset GIT_DIR" in pre_push_content


def test_setup_git_hooks_sh_execution_in_worktree(tmp_path: Path):
    """Verify setup-git-hooks.sh executes successfully inside a worktree and creates hooks in the common hooks dir."""
    import shutil

    from tests.conftest import get_clean_git_env

    # Check if bash is available
    bash_path = shutil.which("bash")
    if not bash_path:
        return

    clean_env = get_clean_git_env()
    main_repo = tmp_path / "repo_sh"
    main_repo.mkdir()

    # Initialize main repo
    subprocess.run(
        ["git", "init"],
        cwd=str(main_repo),
        check=True,
        capture_output=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(main_repo),
        check=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(main_repo),
        check=True,
        env=clean_env,
    )

    # Initial commit
    readme = main_repo / "README.md"
    readme.write_text("Hello", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=str(main_repo),
        check=True,
        capture_output=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(main_repo),
        check=True,
        capture_output=True,
        env=clean_env,
    )

    # Create worktree
    worktree_path = tmp_path / "wt_sh"
    subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", "feature-sh"],
        cwd=str(main_repo),
        check=True,
        capture_output=True,
        env=clean_env,
    )

    # Copy real scripts directory to the worktree
    real_scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    target_scripts_dir = worktree_path / "scripts"
    shutil.copytree(real_scripts_dir, target_scripts_dir)

    # Execute setup-git-hooks.sh from inside the worktree
    res = subprocess.run(
        ["bash", str(target_scripts_dir / "setup-git-hooks.sh")],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        env=clean_env,
    )
    assert res.returncode == 0, f"setup-git-hooks.sh failed: {res.stderr}\n{res.stdout}"

    # Verify hooks exist in the common .git/hooks directory
    common_hooks_dir = main_repo / ".git" / "hooks"
    assert (
        common_hooks_dir / "pre-commit"
    ).exists(), "pre-commit hook was not created in common hooks dir"
    assert (
        common_hooks_dir / "pre-push"
    ).exists(), "pre-push hook was not created in common hooks dir"

    # Verify pre-push content in common hooks contains GIT_* unset
    pre_push_content = (common_hooks_dir / "pre-push").read_text(encoding="utf-8")
    assert "unset GIT_DIR" in pre_push_content
