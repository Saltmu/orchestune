"""Forge contracts and the backwards-compatible composed GitHub facade."""

from __future__ import annotations

import shutil as shutil  # compatibility patch surface
import subprocess
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from orchestune.forge_admin import _LABEL_LIST_LIMIT as _LABEL_LIST_LIMIT
from orchestune.forge_admin import (
    REQUIRED_LABELS,
    BootstrapResult,
    ForgeAuthError,
    ForgeError,
    GitHubRepoAdminMixin,
    LabelSpec,
)
from orchestune.forge_issues import GitHubIssueMixin
from orchestune.forge_prs import GitHubPullRequestMixin
from orchestune.models import IssueRecord, PrRecord


@runtime_checkable
class IssueForge(Protocol):
    def list_issues_by_label(
        self, label: str, state: str = "open", limit: int = 1000
    ) -> list[IssueRecord]: ...

    def list_sub_issues(self, parent_issue_number: int | str) -> list[IssueRecord]: ...

    def add_label(self, issue_number: int | str, label: str) -> None: ...

    def remove_label(self, issue_number: int | str, label: str) -> None: ...

    def close_issue(
        self, issue_number: int | str, reason: str, comment: str | None = None
    ) -> None: ...

    def add_comment(self, issue_number: int | str, body: str) -> None: ...

    def get_issue_state(self, issue_number: int | str) -> str: ...

    def get_issue_labels(self, issue_number: int | str) -> tuple[str, ...]: ...

    def get_label_actor(self, issue_number: int | str, label: str) -> str: ...

    def get_actor_permission(self, username: str) -> str: ...

    def get_issue_last_reopened_at(self, issue_number: int | str) -> str | None: ...

    def create_issue(
        self, title: str, body: str, labels: Sequence[str] = ()
    ) -> int: ...

    def add_sub_issue(
        self, parent_issue_number: int | str, child_issue_number: int | str
    ) -> None: ...

    def set_blocked_by(
        self, issue_number: int | str, blocking_issue_number: int | str
    ) -> None: ...

    def find_open_issues_by_exact_title(self, title: str) -> list[IssueRecord]: ...

    def get_issue(self, issue_number: int | str) -> IssueRecord | None: ...


@runtime_checkable
class PullRequestForge(Protocol):
    def merge_pull_request(self, pr_number: int | str) -> None: ...

    def create_pull_request(
        self, head: str, base: str, title: str, body: str
    ) -> int: ...

    def is_branch_merged_into(self, head: str, base: str) -> bool: ...

    def get_merged_pr_timestamp(self, head: str, base: str) -> str | None: ...

    def branch_exists(self, branch: str) -> bool: ...

    def is_current_branch_tip_merged_into(self, head: str, base: str) -> bool: ...

    def list_prs(
        self, state: str = "open", limit: int = 1000, paginate_files: bool = False
    ) -> list[PrRecord]: ...

    def list_open_prs(
        self, limit: int = 1000, paginate_files: bool = False
    ) -> list[PrRecord]: ...


@runtime_checkable
class RepoAdminForge(Protocol):
    def check_auth(self) -> None:
        """認証が利用できない場合はForgeAuthErrorを送出する。"""

    def ensure_labels(self, labels: tuple[LabelSpec, ...]) -> BootstrapResult:
        """未作成のラベルのみ作成する（既存ラベルは変更しない）。"""


@runtime_checkable
class Forge(IssueForge, PullRequestForge, RepoAdminForge, Protocol):
    """Orchestune が利用する分割済み Forge 契約の合成型。"""


class GitHubForge(GitHubIssueMixin, GitHubPullRequestMixin, GitHubRepoAdminMixin):
    """Compatibility facade composing focused GitHub Forge implementations."""

    def _run(self, args: list[str], input_text: str | None = None) -> str:
        result = subprocess.run(
            args,
            input=input_text,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout


__all__ = [
    "REQUIRED_LABELS",
    "BootstrapResult",
    "Forge",
    "ForgeAuthError",
    "ForgeError",
    "GitHubForge",
    "IssueForge",
    "LabelSpec",
    "PullRequestForge",
    "RepoAdminForge",
]
