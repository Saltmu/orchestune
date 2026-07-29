"""Compatibility shims delegating GitHub operations to the default Forge.

The production callers remain unchanged while adapter migration proceeds through
#295. New code should depend on the focused protocols in :mod:`orchestune.forge`.
"""

from __future__ import annotations

# tests/test_github.py historically patches this path. The subprocess module is a
# singleton, so patching this alias also patches the module used by GitHubForge.
import subprocess  # noqa: F401

# #284(git-adapter): git実行専任のorchestune.git_cliへ移設済み。呼び出し側
# （9参照）の付け替えは後続Issueに分離するため、同一オブジェクトへの
# 再エクスポートとして維持する。
from orchestune.forge import GitHubForge
from orchestune.git_cli import (
    branch_changed_files,  # noqa: F401
    ensure_parent_branch,  # noqa: F401
    fetch_remote_branch,  # noqa: F401
    list_remote_branches,  # noqa: F401
    resolve_local_or_remote_branch,  # noqa: F401
)
from orchestune.models import IssueRecord, PrRecord
from orchestune.validation import (  # noqa: F401
    validate_issue_number as _validate_issue_number,
)
from orchestune.validation import validate_label as _validate_label  # noqa: F401
from orchestune.validation import validate_ref_name as _validate_ref_name
from orchestune.validation import validate_username as _validate_username  # noqa: F401

_DEFAULT_FORGE = GitHubForge()


def _run(args: list[str], input_text: str | None = None) -> str:
    """Delegate the legacy private command runner during the shim period."""
    return _DEFAULT_FORGE._run(args, input_text=input_text)


def list_issues_by_label(
    label: str, state: str = "open", limit: int = 1000
) -> list[IssueRecord]:
    return _DEFAULT_FORGE.list_issues_by_label(label, state=state, limit=limit)


def list_sub_issues(parent_issue_number: int | str) -> list[IssueRecord]:
    return _DEFAULT_FORGE.list_sub_issues(parent_issue_number)


def add_label(issue_number: int | str, label: str) -> None:
    _DEFAULT_FORGE.add_label(issue_number, label)


def remove_label(issue_number: int | str, label: str) -> None:
    _DEFAULT_FORGE.remove_label(issue_number, label)


def close_issue(
    issue_number: int | str, reason: str, comment: str | None = None
) -> None:
    _DEFAULT_FORGE.close_issue(issue_number, reason, comment=comment)


def add_comment(issue_number: int | str, body: str) -> None:
    _DEFAULT_FORGE.add_comment(issue_number, body)


def merge_pull_request(pr_number: int | str) -> None:
    _DEFAULT_FORGE.merge_pull_request(pr_number)


def get_issue_state(issue_number: int | str) -> str:
    return _DEFAULT_FORGE.get_issue_state(issue_number)


def get_issue_labels(issue_number: int | str) -> tuple[str, ...]:
    return _DEFAULT_FORGE.get_issue_labels(issue_number)


def get_label_actor(issue_number: int | str, label: str) -> str:
    return _DEFAULT_FORGE.get_label_actor(issue_number, label)


def get_actor_permission(username: str) -> str:
    return _DEFAULT_FORGE.get_actor_permission(username)


def create_pull_request(head: str, base: str, title: str, body: str) -> int:
    return _DEFAULT_FORGE.create_pull_request(head, base, title, body)


def is_branch_merged_into(head: str, base: str) -> bool:
    return _DEFAULT_FORGE.is_branch_merged_into(head, base)


def get_merged_pr_timestamp(head: str, base: str) -> str | None:
    return _DEFAULT_FORGE.get_merged_pr_timestamp(head, base)


def get_issue_last_reopened_at(issue_number: int | str) -> str | None:
    return _DEFAULT_FORGE.get_issue_last_reopened_at(issue_number)


def branch_exists(branch: str) -> bool:
    return _DEFAULT_FORGE.branch_exists(branch)


def is_current_branch_tip_merged_into(head: str, base: str) -> bool:
    return _DEFAULT_FORGE.is_current_branch_tip_merged_into(head, base)


def _fetch_all_pr_files(pr_number: int) -> tuple[tuple[str, ...], bool]:
    return _DEFAULT_FORGE._fetch_all_pr_files(pr_number)


def list_prs(
    state: str = "open", limit: int = 1000, paginate_files: bool = False
) -> list[PrRecord]:
    return _DEFAULT_FORGE.list_prs(
        state=state, limit=limit, paginate_files=paginate_files
    )


def list_open_prs(limit: int = 1000, paginate_files: bool = False) -> list[PrRecord]:
    return _DEFAULT_FORGE.list_open_prs(limit=limit, paginate_files=paginate_files)


def _is_check_passing(check: dict[str, object]) -> bool:
    return GitHubForge._is_check_passing(check)


def _status_check_contexts(rollup: object) -> list[dict[str, object]]:
    return GitHubForge._status_check_contexts(rollup)


def normalize_remote_branch_name(branch: str) -> str:
    """`origin/` 接頭辞の有無を吸収し、リモートのブランチ名を返す。"""
    if branch.startswith("origin/"):
        branch = branch.removeprefix("origin/")
    return _validate_ref_name(branch)
