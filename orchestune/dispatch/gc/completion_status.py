"""Outcome, PR, and parent-merge status of an active dispatch task.

``completion`` imports the helpers its decision functions still call.
This module does not import ``completion``.
"""

from __future__ import annotations

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.gc.completion_contracts import (
    CompletedWorktreeDecision,
    ForgeFailure,
    warn_forge_failure,
)
from orchestune.dispatch.gc.prior_merge import decide_prior_parent_merge_completion
from orchestune.dispatch.targets import (
    DispatchHandle,
)
from orchestune.forge import Forge
from orchestune.ledger.run_state import (
    ActiveWorktree,
)
from orchestune.models import IssueRecord, PrRecord
from orchestune.outcome_record import (
    OutcomeLookupResult,
    OutcomeLookupState,
    parse_from_comments,
)
from orchestune.pr_freshness import should_exclude_pr
from orchestune.pr_link_notice import pr_matches_issue
from orchestune.task_metadata import TaskMetadata


def _fetch_outcome_for_active(
    active: ActiveWorktree, forge: Forge, error_sink: list[ForgeFailure] | None = None
) -> OutcomeLookupResult:
    """Read the active task's Issue-only Outcome declaration.

    `ABSENT` means the complete Issue comment history was read and contains no
    declaration for this session.  A failed read is deliberately `UNKNOWN`, so
    every GC caller can hold rather than turn an API outage into a missing
    Outcome escalation or a reclaim.
    """
    try:
        comments = list(forge.list_comments(active.core.issue_number))
    except Exception as error:  # noqa: BLE001 - 判定を保留し、事実だけ表に出す
        warn_forge_failure("list_comments", active.core.issue_number, error, error_sink)
        return OutcomeLookupResult(state=OutcomeLookupState.UNKNOWN)
    outcome = parse_from_comments(comments, since=active.launch.started_at)
    if outcome is None:
        return OutcomeLookupResult(state=OutcomeLookupState.ABSENT)
    return OutcomeLookupResult(state=OutcomeLookupState.FOUND, record=outcome)


def _prior_merge_decision(
    active: ActiveWorktree,
    active_task: TaskMetadata | None,
    forge: Forge | None,
    issue: IssueRecord | None,
) -> CompletedWorktreeDecision | None:
    prior_merge = decide_prior_parent_merge_completion(
        active.core.issue_number, active_task, forge, issue
    )
    if prior_merge is None:
        return None
    return CompletedWorktreeDecision(
        action=prior_merge.action,
        subtask_id=active_task.subtask_id if active_task else "",
        operation="prior_parent_merge",
        error=prior_merge.error,
    )


def _active_dispatch_handle(active: ActiveWorktree) -> DispatchHandle:
    return DispatchHandle(
        pid=active.launch.pid,
        external_id=active.launch.external_id,
        external_url=active.launch.external_url,
        branch_name=active.core.branch,
        issue_number=active.core.issue_number,
        started_at=active.launch.started_at,
    )


def _is_stale_pr_for_active(pr: PrRecord, active: ActiveWorktree) -> bool:
    """#1306: 起動前に作成された、または`created_at`を確認できないPRは現在の実行の
    証拠にしない。targets側と同じ`pr_freshness`の規則で、CLOSEDも`created_at`で
    判定する（起動前から存在したPRが起動後に閉じられても放棄の根拠にしない）。"""
    return should_exclude_pr(
        created_at=pr.created_at, started_at=active.launch.started_at
    )


def _local_pr_completion_status(
    active: ActiveWorktree,
    config: DispatcherConfig,
    error_sink: list[ForgeFailure] | None = None,
) -> str:
    lookup = _fetch_outcome_for_active(active, config.resolved_forge, error_sink)
    if lookup.state is OutcomeLookupState.FOUND:
        return "completed"
    if lookup.state is OutcomeLookupState.UNKNOWN:
        return "unknown"

    handle = _active_dispatch_handle(active)
    try:
        candidate_prs = config.resolved_forge.list_prs(state="all")
    except Exception as error:  # noqa: BLE001 - 判定を保留し、事実だけ表に出す
        warn_forge_failure("list_prs", active.core.issue_number, error, error_sink)
        return "unknown"
    matching_prs = [
        pr
        for pr in candidate_prs
        if (
            (handle.branch_name is not None and pr.head_ref == handle.branch_name)
            or (
                handle.issue_number is not None
                and pr_matches_issue(pr, handle.issue_number)
            )
        )
        and not _is_stale_pr_for_active(pr, active)
    ]
    for state, status in (
        ("MERGED", "completed"),
        ("OPEN", "pending"),
        ("CLOSED", "abandoned"),
    ):
        if any(pr.state == state for pr in matching_prs):
            return status
    return "pending"


def _call_is_complete(config: DispatcherConfig, handle: DispatchHandle) -> bool:
    """#315レビュー対応: 旧is_completeシグネチャとの互換性を保つ。"""
    assert config.dispatch_target is not None
    try:
        return config.dispatch_target.is_complete(handle, forge=config.resolved_forge)
    except TypeError:
        # `forge`引数なしの旧dispatch_target実装は、引数なしで再試行する。
        return config.dispatch_target.is_complete(handle)
