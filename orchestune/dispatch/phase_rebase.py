"""Rebase/Sync Phase コーディネーター。

外部ブランチ・PRとのfootprint衝突検知によるロック同期、および
`--parent-issue`指定時の親ブランチ検証・作成を担う。
"""

from __future__ import annotations

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.locks import (
    NOTICE_KIND_EXTERNAL_LOCK,
    ExternalLockScanResult,
    _strip_remote_prefix,
    render_external_lock_notice,
    render_external_lock_release_notice,
    scan_external_locks,
)
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import MAX_PENDING_LOCK_RELEASE_NOTICES, RunState
from orchestune.infra.git_cli import (
    branch_changed_files,
    ensure_parent_branch,
    list_remote_branches,
)
from orchestune.issue_notice import post_notice_if_changed
from orchestune.issue_parsing import is_epic_issue
from orchestune.labels import StatusLabel
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


def _release_notice_targets(
    lock_result: ExternalLockScanResult, run_state: RunState
) -> list[int]:
    """解除を伝えるべきIssue番号。今サイクルの解除と、前回書けなかった分。

    PR#789レビュー対応(Codex P2): ラベルは通知より先に外れるため、投稿に失敗
    したタスクは次サイクルの`to_unlock`には現れない。再試行キューを持たないと、
    Issue上の最後の通知が「ロック中」のまま永久に取り残される。
    再ロックされたタスクはロック理由の通知で本文が上書きされるので対象外。
    """
    targets = {task.issue_number for task in lock_result.to_unlock}
    targets |= set(run_state.pending_lock_release_notices)
    return sorted(targets - set(lock_result.conflicts))


def _notify_external_locks(
    lock_result: ExternalLockScanResult,
    config: DispatcherConfig,
    run_state: RunState | None = None,
) -> None:
    """#787: ロック理由と解除をIssueコメントへ残す。

    `conflicts`は新規ロックだけでなく継続ロック中のタスクも含むため、
    ロックが続いている間に衝突相手が変わってもIssue上の理由が追随する。
    """
    forge = config.resolved_forge
    pending = list(run_state.pending_lock_release_notices) if run_state else []
    for issue_number, conflicts in sorted(lock_result.conflicts.items()):
        post_notice_if_changed(
            forge,
            issue_number,
            NOTICE_KIND_EXTERNAL_LOCK,
            render_external_lock_notice(conflicts),
        )
        if issue_number in pending:
            pending.remove(issue_number)

    for issue_number in _release_notice_targets(lock_result, run_state or RunState()):
        outcome = post_notice_if_changed(
            forge,
            issue_number,
            NOTICE_KIND_EXTERNAL_LOCK,
            render_external_lock_release_notice(),
            update_only=True,
        )
        if outcome.settled:
            if issue_number in pending:
                pending.remove(issue_number)
        elif issue_number not in pending:
            pending.append(issue_number)

    if run_state is not None:
        run_state.pending_lock_release_notices = pending[
            -MAX_PENDING_LOCK_RELEASE_NOTICES:
        ]


def _apply_external_lock_sync(
    lock_result: ExternalLockScanResult,
    config: DispatcherConfig,
    run_state: RunState | None = None,
) -> None:
    if not config.apply:
        return
    for task in lock_result.to_lock:
        config.resolved_forge.add_label(task.issue_number, StatusLabel.EXTERNAL_LOCK)
    for task in lock_result.to_unlock:
        config.resolved_forge.remove_label(task.issue_number, StatusLabel.EXTERNAL_LOCK)
        # #197 / #214: ロック解除時、Taskの現在のラベル状態に基づき status:queued を冪等に再付与・同期する。
        # 既に Task オブジェクトが status:queued を持つ場合でも、GitHub上の実ラベル状態を確実に同期するための明示的処理。
        if (
            StatusLabel.QUEUED in task.status_labels
            and StatusLabel.DONE not in task.status_labels
        ):
            config.resolved_forge.add_label(task.issue_number, StatusLabel.QUEUED)
    _notify_external_locks(lock_result, config, run_state)


def _sync_external_locks(
    tasks_by_issue: dict[int, Task],
    prs: list[PrRecord],
    run_state: RunState,
    config: DispatcherConfig,
) -> ExternalLockScanResult:
    """decide+applyの薄いラッパー（呼び出し互換のため維持）。"""
    lock_result = _decide_external_lock_sync(tasks_by_issue, prs, run_state, config)
    _apply_external_lock_sync(lock_result, config, run_state)
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
