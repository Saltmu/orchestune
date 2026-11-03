from __future__ import annotations

import subprocess
from pathlib import Path

from orchestune.infra.execution_deadline import (
    ExecutionCommandTimeout,
    ExecutionDeadlineExceeded,
)
from orchestune.infra.git_cli import run_git


class MergeWorkspace:
    """Manages workspace-level git operations on the integration branch."""

    def __init__(self, repository_root: Path) -> None:
        self.repository_root = repository_root

    def create_temp_branch(
        self, temp_branch: str, base_branch: str, apply: bool
    ) -> bool:
        if not apply:
            return True
        try:
            run_git(
                ["checkout", "-B", temp_branch, base_branch],
                cwd=self.repository_root,
                check=True,
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def ensure_git_identity(self) -> None:
        # CI環境（actions/checkout等）ではgit committer identityが未設定のことがあり、
        # `git merge --no-ff`でマージコミットを作成する際に
        # "Committer identity unknown" で必ず失敗するため、事前に設定しておく。
        run_git(
            ["config", "user.name", "orchestune-integrator"],
            cwd=self.repository_root,
            check=False,
        )
        run_git(
            [
                "config",
                "user.email",
                "orchestune-integrator@users.noreply.github.com",
            ],
            cwd=self.repository_root,
            check=False,
        )

    def ensure_full_history(self, base_branch: str) -> None:
        # actions/checkout@v6 のデフォルト（浅い・単一ブランチのclone）のままだと、
        # タスクブランチをrefspec指定でfetchしても取得されるのはそのブランチの
        # 先端コミット1つのみ（親情報を持たない）になり、mainと共通の祖先が
        # 見つからず `refusing to merge unrelated histories` でmergeが必ず
        # 失敗する。浅いリポジトリの場合のみ、ベースブランチの履歴を深くしておく。
        try:
            result = run_git(
                ["rev-parse", "--is-shallow-repository"],
                cwd=self.repository_root,
                check=True,
            )
        except subprocess.CalledProcessError:
            return

        if result.stdout.strip() != "true":
            return

        base_branch_name = base_branch.removeprefix("origin/")
        try:
            run_git(
                ["fetch", "--unshallow", "origin", base_branch_name],
                cwd=self.repository_root,
                check=True,
            )
        except subprocess.CalledProcessError:
            pass

    def current_head_sha(self) -> str:
        result = run_git(["rev-parse", "HEAD"], cwd=self.repository_root, check=True)
        return result.stdout.strip()

    def rollback_to(self, sha: str) -> bool:
        try:
            run_git(["reset", "--hard", sha], cwd=self.repository_root, check=True)
            return True
        except (subprocess.CalledProcessError, OSError):
            return False

    def abort_merge(self) -> None:
        # マージ失敗時にMERGE_HEADを残したままにすると、後続タスクのマージが
        # 「進行中の未完了マージがある」ために巻き添えで失敗してしまうため、
        # 一時ブランチの直前の状態へ確実に戻す。
        try:
            run_git(["merge", "--abort"], cwd=self.repository_root, check=False)
        except (subprocess.CalledProcessError, OSError):
            pass

    def abort_merge_quietly(self) -> None:
        try:
            self.abort_merge()
        except (ExecutionDeadlineExceeded, ExecutionCommandTimeout):
            pass
