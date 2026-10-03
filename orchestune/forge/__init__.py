"""Forge contracts and the backwards-compatible composed GitHub facade."""

from __future__ import annotations

import shutil as shutil  # compatibility patch surface
import subprocess
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from orchestune.infra.command_metrics import measure_gh_call
from orchestune.infra.execution_deadline import (
    command_timeout_signal,
    scope_bound_applies,
    scoped_command_timeout,
)
from orchestune.models import IssueRecord, PrRecord, normalize_newlines

from .admin import _LABEL_LIST_LIMIT as _LABEL_LIST_LIMIT
from .admin import (
    REQUIRED_LABELS,
    BootstrapResult,
    ForgeAuthError,
    ForgeError,
    GitHubRepoAdminMixin,
    LabelSpec,
    RelationshipUnavailableError,
)
from .issues import GitHubIssueMixin
from .prs import GitHubPullRequestMixin


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

    def remove_sub_issue(
        self, parent_issue_number: int | str, child_issue_number: int | str
    ) -> None: ...

    def set_blocked_by(
        self, issue_number: int | str, blocking_issue_number: int | str
    ) -> None: ...

    def remove_blocked_by(
        self, issue_number: int | str, blocking_issue_number: int | str
    ) -> None: ...

    def find_open_issues_by_exact_title(self, title: str) -> list[IssueRecord]: ...

    def get_issue(self, issue_number: int | str) -> IssueRecord | None: ...

    def find_issues_by_parent_metadata(
        self, parent_issue_number: int | str
    ) -> list[IssueRecord]: ...


@runtime_checkable
class PullRequestForge(Protocol):
    def get_authenticated_user(self) -> str: ...

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

    def get_current_branch_tip_sha_if_merged_into(
        self, head: str, base: str
    ) -> str | None: ...

    def is_merge_commit_reachable_from(self, commit_oid: str, base: str) -> bool: ...

    def list_prs(
        self,
        state: str = "open",
        limit: int = 1000,
        paginate_files: bool = False,
        head: str | None = None,
        include_files: bool = False,
    ) -> list[PrRecord]: ...

    def list_merged_prs_for_base(self, base: str) -> list[PrRecord]: ...

    def list_open_prs(
        self,
        limit: int = 1000,
        paginate_files: bool = False,
        include_files: bool = True,
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


def _decode(raw: bytes | str | None) -> str:
    """バイナリ実行の出力を文字列へ復号する（Noneは空文字へ倒す）。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-8", errors="replace")


#: A Forge that bounds every call by the active execution scope declares this
#: attribute as ``True``. The Integrator refuses to apply with a Forge that does not.
BOUNDED_EXECUTION_ATTRIBUTE = "supports_bounded_execution"


def forge_supports_bounded_execution(forge: object) -> bool:
    """Whether ``forge`` explicitly declares scope-bounded execution (#820)."""
    return getattr(forge, BOUNDED_EXECUTION_ATTRIBUTE, False) is True


class GitHubForge(GitHubIssueMixin, GitHubPullRequestMixin, GitHubRepoAdminMixin):
    """Compatibility facade composing focused GitHub Forge implementations."""

    supports_bounded_execution = True

    def __init__(self, *, timeout_seconds: float | None = None) -> None:
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("Forge timeout must be positive")
        self.timeout_seconds = timeout_seconds

    def _run(self, args: list[str], input_text: str | None = None) -> str:
        with measure_gh_call(enabled=bool(args) and args[0] == "gh"):
            timeout = scoped_command_timeout(self.timeout_seconds, "gh")
            try:
                return self._run_bounded(args, input_text, timeout)
            except subprocess.TimeoutExpired as error:
                # #820: a timeout set by the Integrator's scope must not be swallowed
                # by best-effort ``except Exception`` clauses; the caller's own
                # ``timeout_seconds`` keeps its existing ``TimeoutExpired`` behaviour.
                if timeout is not None and scope_bound_applies(
                    self.timeout_seconds, timeout
                ):
                    raise command_timeout_signal(
                        f"gh {args[1] if len(args) > 1 else ''}".strip(), timeout, error
                    ) from error
                raise

    def _run_bounded(
        self, args: list[str], input_text: str | None, timeout: float | None
    ) -> str:
        timeout_kwargs: dict[str, Any] = (
            {"timeout": timeout} if timeout is not None else {}
        )
        if input_text is None:
            return _decode(
                subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=True,
                    **timeout_kwargs,
                ).stdout
            )
        if timeout is not None:
            return self._run_with_stdin(args, input_text, timeout_seconds=timeout)
        return self._run_with_stdin(args, input_text)

    @staticmethod
    def _run_with_stdin(
        args: list[str], input_text: str, *, timeout_seconds: float | None = None
    ) -> str:
        """#664: 標準入力へ渡す本文をバイト列で書き込む。

        Windows上の`text=True`なsubprocess stdin書き込みは`\\n`を
        `os.linesep`(CRLF)へ変換するため、LFへ正規化した本文がGitHub上では
        CRLFへ戻ってしまい、次回読み戻したときに本文中のマーカーブロックが
        一致しなくなる（＝置換されず追記される）。バイト列で渡してOSによる
        改行変換そのものを回避する。
        """
        payload = normalize_newlines(input_text).encode("utf-8")
        timeout_kwargs: dict[str, Any] = (
            {"timeout": timeout_seconds} if timeout_seconds is not None else {}
        )
        result = subprocess.run(
            args, input=payload, capture_output=True, check=False, **timeout_kwargs
        )
        stdout = _decode(result.stdout)
        # 呼び出し側(`forge.issues`)がstderrを文字列として検査するため、
        # バイナリ実行でも文字列へ復号してから例外を組み立てる。
        stderr = _decode(result.stderr)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, args, output=stdout, stderr=stderr
            )
        return stdout


__all__ = [
    "BOUNDED_EXECUTION_ATTRIBUTE",
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
    "forge_supports_bounded_execution",
]
