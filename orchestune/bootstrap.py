from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from orchestune.forge import REQUIRED_LABELS, ForgeError, GitHubForge, RepoAdminForge
from orchestune.git_cli import run_git


def _is_git_repo_empty(cwd: Path) -> bool:
    try:
        run_git(["rev-parse", "--is-inside-work-tree"], cwd=cwd, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

    res = run_git(["rev-parse", "HEAD"], cwd=cwd, check=False)
    return res.returncode != 0


def _initialize_empty_repo(cwd: Path) -> None:
    res_staged = run_git(["ls-files", "--stage"], cwd=cwd, check=True)
    if res_staged.stdout.strip():
        raise RuntimeError("Staged files already exist in index before initial commit.")

    print(
        "Detected an empty Git repository. Initializing with initial commit...",
        file=sys.stderr,
    )

    try:
        run_git(["config", "user.name"], cwd=cwd, check=True)
    except subprocess.CalledProcessError:
        run_git(["config", "user.name", "orchestune-bootstrap"], cwd=cwd, check=True)

    try:
        run_git(["config", "user.email"], cwd=cwd, check=True)
    except subprocess.CalledProcessError:
        run_git(
            ["config", "user.email", "orchestune-bootstrap@users.noreply.github.com"],
            cwd=cwd,
            check=True,
        )

    readme_path = cwd / "README.md"
    if not readme_path.exists():
        readme_path.write_text("# Initialized by Orchestune\n")

    run_git(["add", "README.md"], cwd=cwd, check=True)
    run_git(["commit", "-m", "Initial commit"], cwd=cwd, check=True)

    run_git(["branch", "-M", "main"], cwd=cwd, check=True)

    res_remote = run_git(["remote"], cwd=cwd, check=True)
    if "origin" in res_remote.stdout.splitlines():
        print("Pushing initial commit to origin/main...", file=sys.stderr)
        run_git(["push", "-u", "origin", "main"], cwd=cwd, check=True)


def run_bootstrap(forge: RepoAdminForge | None = None, cwd: Path = Path(".")) -> int:
    forge = forge or GitHubForge()

    if _is_git_repo_empty(cwd):
        try:
            _initialize_empty_repo(cwd)
        except Exception as e:
            print(f"Error initializing empty repository: {e}", file=sys.stderr)
            return 1

    try:
        forge.check_auth()
        result = forge.ensure_labels(REQUIRED_LABELS)
    except ForgeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"Labels created: {len(result.created_labels)}")
    for name in result.created_labels:
        print(f"  + {name}")
    print(f"Labels already present: {len(result.existing_labels)}")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="orchestune bootstrap: gh認証確認と必須ラベルの起票を行う"
    )
    parser.parse_args(argv)
    sys.exit(run_bootstrap())
