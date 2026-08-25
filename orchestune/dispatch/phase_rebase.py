"""Rebase/Sync Phase コーディネーター。

外部ブランチ・PRとのfootprint衝突検知によるロック同期、および
`--parent-issue`指定時の親ブランチ検証・作成を担う。
"""

from __future__ import annotations

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.locks import (
    ExternalLockScanResult,
    _strip_remote_prefix,
    scan_external_locks,
)
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import RunState
from orchestune.infra.git_cli import (
    branch_changed_files,
    ensure_parent_branch,
    list_remote_branches,
)
from orchestune.issue_parsing import is_epic_issue
from orchestune.models import PrRecord


def _is_base_or_parent_branch(
    branch_name: str, config: DispatcherConfig | None = None
) -> bool:
    name = _strip_remote_prefix(branch_name)
    if name in {"main", "master", "HEAD"} or name.endswith("/HEAD"):
        return True
    if name.startswith("parent/issue-"):
        return True
    if config and config.parent_issue_number is not None:
        if name == f"parent/issue-{config.parent_issue_number}":
            return True
    return False


def _decide_external_lock_sync(
    tasks_by_issue: dict[int, Task],
    prs: list[PrRecord],
    run_state: RunState,
    config: DispatcherConfig | None = None,
) -> ExternalLockScanResult:
    """githubからの読み取り(list_remote_branches/branch_changed_files)と
    scan_external_locksの純粋計算のみを行い、ラベルの書き込みは行わない。"""
    remote_branch_names = list_remote_branches()
    active_branches = [aw.branch for aw in run_state.active_worktrees.values()]
    pr_head_refs = {pr.head_ref for pr in prs}
    bare_branches = [
        b
        for b in remote_branch_names
        if _strip_remote_prefix(b) not in pr_head_refs
        and _strip_remote_prefix(b) not in active_branches
        and not _is_base_or_parent_branch(b, config)
    ]
    # #245: 差分取得不能(None)はtupleへ潰さずそのまま渡し、
    # scan_external_locks側でfail closed（lock維持・新規lock）に判定させる。
    remote_branch_footprints: list[tuple[str, tuple[str, ...] | None]] = []
    for branch in bare_branches:
        changed_files = branch_changed_files(branch)
        remote_branch_footprints.append(
            (
                _strip_remote_prefix(branch),
                tuple(changed_files) if changed_files is not None else None,
            )
        )

    all_tasks = list(tasks_by_issue.values())
    return scan_external_locks(
        all_tasks, remote_branch_footprints, prs, active_branches
    )


def _apply_external_lock_sync(
    lock_result: ExternalLockScanResult, config: DispatcherConfig
) -> None:
    if not config.apply:
        return
    for task in lock_result.to_lock:
        config.resolved_forge.add_label(task.issue_number, "status:external-lock")
    for task in lock_result.to_unlock:
        config.resolved_forge.remove_label(task.issue_number, "status:external-lock")
        # #197 / #214: ロック解除時、Taskの現在のラベル状態に基づき status:queued を冪等に再付与・同期する。
        # 既に Task オブジェクトが status:queued を持つ場合でも、GitHub上の実ラベル状態を確実に同期するための明示的処理。
        if (
            "status:queued" in task.status_labels
            and "status:done" not in task.status_labels
        ):
            config.resolved_forge.add_label(task.issue_number, "status:queued")


def _sync_external_locks(
    tasks_by_issue: dict[int, Task],
    prs: list[PrRecord],
    run_state: RunState,
    config: DispatcherConfig,
) -> ExternalLockScanResult:
    """decide+applyの薄いラッパー（呼び出し互換のため維持）。"""
    lock_result = _decide_external_lock_sync(tasks_by_issue, prs, run_state, config)
    _apply_external_lock_sync(lock_result, config)
    return lock_result


def ensure_parent_branch_ready(config: DispatcherConfig) -> None:
    """`--parent-issue`指定時、対象IssueがEPICとして正しい構造を持つことを
    検証した上で、対応する`parent/issue-<N>`ブランチが存在することを保証する。

    `config.apply`がFalseの場合は何もしない（既存の`run_dispatch_cycle`の
    条件`config.parent_issue_number is not None and config.apply`と同一）。
    """
    if config.parent_issue_number is None or not config.apply:
        return
    issue = config.resolved_forge.get_issue(config.parent_issue_number)
    if issue is None:
        raise RuntimeError(
            f"--parent-issue {config.parent_issue_number} does not "
            "exist; refusing to create a parent branch for it."
        )
    if not is_epic_issue(issue):
        raise RuntimeError(
            f"--parent-issue {config.parent_issue_number} "
            f"('{issue.title}') does not look like an EPIC issue "
            "created by orchestune provision (expected a '[EPIC] ' "
            "title and the decomposition-plan-parent marker in its "
            "body); refusing to create/reuse "
            f"'parent/issue-{config.parent_issue_number}'."
        )
    ensure_parent_branch(config.parent_issue_number)
