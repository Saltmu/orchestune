from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class IntegrationWorktree:
    """`temp_branch`用の一時git worktreeのパス計算と安全な回収を担う。"""

    def __init__(self, original_root: Path, temp_branch: str):
        self.original_root = original_root
        self.temp_branch = temp_branch

    def key(self) -> str:
        """`temp_branch`（親Issueごとに一意）を安全なファイル/ディレクトリ名に変換する。"""
        return self.temp_branch.replace("/", "-")

    def temp_path(self) -> Path:
        return self.original_root / "worktrees" / f"integration-temp-{self.key()}"

    def lock_path(self) -> Path:
        return self.original_root / "worktrees" / ".locks" / f"{self.key()}.lock"

    def reclaim(self, path: Path) -> None:
        """`path`に残存物があれば、所有権を確認した上でのみ除去する。

        `git worktree add`によって作成されたリンクワークツリーは、直下に
        gitdirへのポインタを記した`.git`ファイル（ディレクトリではない）を持つ。
        これを確認できた場合に限り`git worktree remove`で除去し、それ以外の
        予期しないディレクトリ（他プロセスの作業ディレクトリ等）は所有権を
        確認できないため一切削除しない。
        """
        if not path.exists():
            return
        git_marker = path / ".git"
        if not git_marker.is_file():
            raise RuntimeError(
                f"Refusing to remove unrecognized path (not a git worktree): {path}"
            )
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(path)],
            cwd=str(self.original_root),
            capture_output=True,
        )
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
