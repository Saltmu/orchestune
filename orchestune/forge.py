"""Forge contracts and the backwards-compatible composed GitHub facade."""

from __future__ import annotations

import shutil as shutil  # compatibility patch surface
import subprocess
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

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


class RelationshipUnavailableError(ForgeError):
    """#485: `add_sub_issue`/`set_blocked_by`/`update_issue_body`のような
    GitHub関係・metadata書き込み操作を、この`Forge`実装が構造的にサポート
    していないことを示す。

    `gh` CLIやGitHub MCPの通常のAPI呼び出し失敗（ネットワーク瞬断、権限、
    一時的なレート制限など）とは意味が異なる: そうした失敗は呼び出し元に
    伝播させ、再試行できるようにすべきもの。これは「そもそもこの機能を
    提供しない」実装（例: Sub-issue/Issue dependency書き込みを公開しない
    GitHub MCP）が明示的に送出する専用の例外で、`provisioning.py`はこれ
    だけを捕捉して本文metadataフォールバックへ縮退する。
    """


class MetadataSearchUnavailableError(ForgeError):
    """#485: `find_issues_by_parent_metadata`のような本文metadata検索を、
    この`Forge`実装が構造的にサポートしていないことを示す。

    `RelationshipUnavailableError`と同じ理由で、通常のAPI呼び出し失敗
    （`gh`認証切れ、レート制限、ネットワーク瞬断など）とは区別する:
    そうした失敗を黙って握りつぶすと、metadataでしか発見できないIssueが
    そのサイクル/実行から一時的に消え、`provisioning.py`のdedup fallback
    が誤って重複作成しかねない。`issue_parsing.find_children_by_parent`は
    これ（および未実装を示す`AttributeError`/`NotImplementedError`）だけを
    捕捉し、それ以外は呼び出し元に伝播させて再試行に委ねる。
    """


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

    def list_comments(self, issue_number: int | str) -> list[dict[str, Any]]: ...

    def get_issue_state(self, issue_number: int | str) -> str: ...

    def get_issue_labels(self, issue_number: int | str) -> tuple[str, ...]: ...

    def get_label_actor(self, issue_number: int | str, label: str) -> str: ...

    def get_actor_permission(self, username: str) -> str: ...

    def get_issue_last_reopened_at(self, issue_number: int | str) -> str | None: ...

    def create_issue(
        self, title: str, body: str, labels: Sequence[str] = ()
    ) -> int: ...

    def update_issue_body(self, issue_number: int | str, body: str) -> None: ...

    def update_issue_title(self, issue_number: int | str, title: str) -> None: ...

    def add_sub_issue(
        self, parent_issue_number: int | str, child_issue_number: int | str
    ) -> None: ...

    def set_blocked_by(
        self, issue_number: int | str, blocking_issue_number: int | str
    ) -> None: ...

    def find_open_issues_by_exact_title(self, title: str) -> list[IssueRecord]: ...

    def get_issue(self, issue_number: int | str) -> IssueRecord | None: ...

    def find_issues_by_parent_metadata(
        self, parent_issue_number: int | str
    ) -> list[IssueRecord]: ...


@runtime_checkable
class PullRequestForge(Protocol):
    def delete_branch(self, branch: str) -> None: ...

    def merge_pull_request(self, pr_number: int | str) -> None: ...

    def create_pull_request(
        self, head: str, base: str, title: str, body: str
    ) -> int: ...

    def update_pull_request(
        self, pr_number: int | str, title: str, body: str
    ) -> None: ...

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
        if input_text is not None:
            # Windows上のtext=Trueなsubprocess stdin書き込みは\nを
            # os.linesep(\r\n)へ変換する。既にCRLFを含む文字列(GitHubから
            # 再取得した既存Issue本文など)をそのまま渡すと、書き込みの
            # たびに\rが積み上がってしまうため、事前にLFへ正規化する。
            input_text = input_text.replace("\r\n", "\n").replace("\r", "\n")
        result = subprocess.run(
            args,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
    "MetadataSearchUnavailableError",
    "PullRequestForge",
    "RelationshipUnavailableError",
    "RepoAdminForge",
]
