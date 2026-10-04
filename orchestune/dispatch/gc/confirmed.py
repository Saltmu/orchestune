"""Dispatcher adapter for physical collection of verified journal completions."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from typing import Any, Literal, cast

from orchestune.claim.workspace import resolve_claim_workspace
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_events import (
    ActiveReservationHold,
    CompletionEvent,
    ForgeFailureCompletion,
    HandoffCollectionCompletion,
    HandoffPreviewCompletion,
    PolicyHoldCompletion,
)
from orchestune.dispatch.cycle_records import CompletionReceipt
from orchestune.dispatch.gc.collection import GcItemResult, _apply_candidate
from orchestune.dispatch.gc.external_guard import fresh_external_hold, same_execution
from orchestune.dispatch.gc.handoff import GcRequest, HandoffForge
from orchestune.dispatch.gc.policies import process_completion_policies
from orchestune.dispatch.gc.policy_discovery import (
    confirmed_records,
    journal_outcome,
    reclaim_completed_tokens,
)
from orchestune.dispatch.rules import ActiveWorktreeRuleOutcome
from orchestune.infra.process_utils import run_state_lock
from orchestune.ledger.run_state import (
    ActiveWorktree,
    RunState,
    load_run_state_readonly,
    save_run_state,
)
from orchestune.task_metadata import TaskMetadata


def _sync_after_gc(state: RunState, config: DispatcherConfig) -> None:
    saved = load_run_state_readonly(config.run_state_path)
    state.active_worktrees.clear()
    state.active_worktrees.update(saved.active_worktrees)
    state.completed_worktrees = saved.completed_worktrees
    state.completion_reservations = saved.completion_reservations
    state.completion_journal = saved.completion_journal
    state.completion_replay_receipts = saved.completion_replay_receipts
    state.task_reclaim_counts = saved.task_reclaim_counts
    state.recovery_receipts = saved.recovery_receipts


def collect_confirmed_completion(
    state: RunState,
    config: DispatcherConfig,
    key: str,
    active: ActiveWorktree,
    record_completion: Callable[[int], Any],
    task: TaskMetadata | None,
) -> ActiveWorktreeRuleOutcome:
    event: CompletionEvent
    try:
        with (
            run_state_lock(config.run_state_path.with_suffix(".lock"))
            if config.apply
            else nullcontext()
        ):
            saved = load_run_state_readonly(config.run_state_path)
            state.recovery_receipts = saved.recovery_receipts
            fresh = saved.active_worktrees.get(key)
            if (
                fresh is None
                or (
                    fresh.claim.claim_id,
                    fresh.completion.completion_id,
                )
                != (
                    active.claim.claim_id,
                    active.completion.completion_id,
                )
                or (fresh is not None and not same_execution(fresh, active))
            ):
                _sync_after_gc(state, config)
                event = ActiveReservationHold(
                    issue_number=active.core.issue_number,
                    worktree_path=active.core.worktree_path,
                    reason="state_changed",
                )
            else:
                event = _prepare_collection(
                    state, config, key, fresh, record_completion, task
                )
    except Exception as error:
        event = ForgeFailureCompletion(
            issue_number=active.core.issue_number,
            worktree_path=active.core.worktree_path,
            operation="completion_collection",
            error=str(error),
        )
    return ActiveWorktreeRuleOutcome(completion_event=event, terminal=True)


def _prepare_collection(
    state: RunState,
    config: DispatcherConfig,
    key: str,
    active: ActiveWorktree,
    record_completion: Callable[[int], Any],
    task: TaskMetadata | None,
) -> CompletionEvent:
    policies = process_completion_policies(state, config)
    if any(
        isinstance(e, PolicyHoldCompletion)
        and e.issue_number == active.core.issue_number
        for e in policies
    ):
        return ActiveReservationHold(
            issue_number=active.core.issue_number,
            worktree_path=active.core.worktree_path,
        )
    record = next(
        (
            r
            for r in confirmed_records(state)
            if r.issue_number == active.core.issue_number
        ),
        None,
    )
    if record is None or journal_outcome(record) is None:
        return ActiveReservationHold(
            issue_number=active.core.issue_number,
            worktree_path=active.core.worktree_path,
        )
    # #1154: journalが確認済みでも、外部実行の停止未確認なら物理回収・解放を保留する。
    hold = fresh_external_hold(active, config, "completion", state)
    if hold is not None:
        return hold.event(subtask_id=task.subtask_id if task else "")
    if not config.apply:
        return HandoffPreviewCompletion(
            issue_number=active.core.issue_number,
            worktree_path=active.core.worktree_path,
        )
    return _collect(state, config, key, active, record_completion, task)


def _collect(
    state: RunState,
    config: DispatcherConfig,
    key: str,
    active: ActiveWorktree,
    record_completion: Callable[[int], Any],
    task: TaskMetadata | None,
) -> CompletionEvent:
    workspace = resolve_claim_workspace(explicit_state_path=config.run_state_path)
    workspace = replace(workspace, worktree_root=config.worktree_root)
    items: list[GcItemResult] = []
    receipts: list[CompletionReceipt] = []
    event: CompletionEvent = HandoffPreviewCompletion(
        issue_number=active.core.issue_number, worktree_path=active.core.worktree_path
    )
    with run_state_lock(config.run_state_path.with_suffix(".lock")):
        state.recovery_receipts = load_run_state_readonly(
            config.run_state_path
        ).recovery_receipts
        save_run_state(state, config.run_state_path)
        _apply_candidate(
            key,
            active,
            GcRequest(state_path=config.run_state_path),
            workspace,
            cast(HandoffForge, config.resolved_forge),
            items,
            receipts,
            task=task,
            config=config,
        )
        _sync_after_gc(state, config)
        reclaim_completed_tokens(
            state, config.run_state_path, workspace.repository_identity
        )
    if items:
        event = _collection_event(active, items[0])
    for receipt in receipts:
        record_completion(receipt.issue_number)
    return event


def _collection_event(
    active: ActiveWorktree, item: GcItemResult
) -> HandoffCollectionCompletion:
    return HandoffCollectionCompletion(
        issue_number=active.core.issue_number,
        worktree_path=active.core.worktree_path,
        action=cast(
            Literal[
                "completion_handoff_held",
                "completion_handoff_failed",
                "completion_handoff_released",
            ],
            "completion_handoff_" + item.action,
        ),
        reason=item.reason,
    )
