from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from orchestune.forge import REQUIRED_LABELS, Forge, ForgeAuthError, GitHubForge


def _is_git_repo_empty(cwd: Path) -> bool:
    try:
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(cwd),
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

    res = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(cwd),
        capture_output=True,
    )
    return res.returncode != 0


def _initialize_empty_repo(cwd: Path) -> None:
    print(
        "Detected an empty Git repository. Initializing with initial commit...",
        file=sys.stderr,
    )

    try:
        subprocess.run(
            ["git", "config", "user.name"],
            cwd=str(cwd),
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        subprocess.run(
            ["git", "config", "user.name", "orchestune-bootstrap"],
            cwd=str(cwd),
            check=True,
        )

    try:
        subprocess.run(
            ["git", "config", "user.email"],
            cwd=str(cwd),
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        subprocess.run(
            [
                "git",
                "config",
                "user.email",
                "orchestune-bootstrap@users.noreply.github.com",
            ],
            cwd=str(cwd),
            check=True,
        )

    readme_path = cwd / "README.md"
    if not readme_path.exists():
        readme_path.write_text("# Initialized by Orchestune\n")

    subprocess.run(["git", "add", "README.md"], cwd=str(cwd), check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(cwd), check=True)

    subprocess.run(["git", "branch", "-M", "main"], cwd=str(cwd), check=True)

    res_remote = subprocess.run(
        ["git", "remote"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    if "origin" in res_remote.stdout.splitlines():
        print("Pushing initial commit to origin/main...", file=sys.stderr)
        subprocess.run(
            ["git", "push", "-u", "origin", "main"],
            cwd=str(cwd),
            check=True,
        )


def run_bootstrap(forge: Forge | None = None, cwd: Path = Path(".")) -> int:
    forge = forge or GitHubForge()

    if _is_git_repo_empty(cwd):
        try:
            _initialize_empty_repo(cwd)
        except Exception as e:
            print(f"Error initializing empty repository: {e}", file=sys.stderr)
            return 1

    try:
        forge.check_auth()
    except ForgeAuthError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    result = forge.ensure_labels(REQUIRED_LABELS)
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
