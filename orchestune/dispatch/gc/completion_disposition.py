"""Typed classification of what GC does after a completion event (#1349).

This module only describes how ``_handle_completed_event_outcome`` post-processes
an event. It has no side effects and depends on no config, Forge, or run state:
a report DTO never becomes proof of completion. ``may_issue_receipt`` marks an
action that *may* mint a receipt; external-execution stops, reservations and a
successful save are still verified on the existing paths.

Classification is keyed by ``(event variant, action)``. The same action name can
belong to different DTOs (``already_merged`` is both a worktree completion and a
prior-merge result), so strings alone never decide a route. Every
``CompletionEvent`` variant and action is declared explicitly: an unlisted
combination raises ``UnclassifiedCompletionError`` instead of falling back to a
default, and the tests enumerate the union to catch omissions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeGuard, get_args

from orchestune.dispatch.cycle_events import (
    AbandonedExternalExecutionHeldCompletion,
    AbandonmentPersistenceFailureCompletion,
    ActiveReservationHold,
    ChangesRequestedEscalationCompletion,
    CompletingExcludedCompletion,
    CompletionEvent,
    ConfirmedExternalExecutionHeldCompletion,
    DirtyWorktreeEscalatedCompletion,
    EarlyDeathRequeuedCompletion,
    ExternalExecutionHeldCompletion,
    ForgeFailureCompletion,
    HandoffCollectionCompletion,
    HandoffPreviewCompletion,
    InteractiveClosedTargetHeld,
    InteractiveClosedTargetReleased,
    InteractiveExcludedCompletion,
    PolicyHoldCompletion,
    PolicyProgressCompletion,
    PolicyUnavailableCompletion,
    PriorMergeEvidenceCompletion,
    ReclaimedCompletion,
    ReviewTimeoutRequeuedCompletion,
    StaleEntryDiscardedCompletion,
    TaskWorktreeCompletion,
    TokenHoldCompletion,
    UnclaimedReservationHold,
    UsageLimitCompletion,
    WorktreeCompletion,
    WorktreeCompletionHold,
    WorktreeCompletionHoldAction,
)


class PostCompletionRoute(Enum):
    """What ``_handle_completed_event_outcome`` adds for an event.

    ``KEEP`` does not undo what the event's producer already did: it only means
    the handler records no history entry and removes no active entry itself.
    """

    KEEP = "keep"
    HISTORY = "history"
    RETIRE = "retire"
    DIRTY_HOLD = "dirty_hold"


@dataclass(frozen=True, slots=True)
class CompletionHandling:
    route: PostCompletionRoute
    may_issue_receipt: bool = False
    exclude_same_cycle_reclaim: bool = False


class UnclassifiedCompletionError(LookupError):
    """A completion event variant/action has no declared post-processing."""


_KEEP = CompletionHandling(PostCompletionRoute.KEEP)
_KEEP_EXCLUDED = CompletionHandling(
    PostCompletionRoute.KEEP, exclude_same_cycle_reclaim=True
)
_HISTORY_RECEIPT = CompletionHandling(
    PostCompletionRoute.HISTORY, may_issue_receipt=True
)
_HISTORY = CompletionHandling(PostCompletionRoute.HISTORY)
_RETIRE = CompletionHandling(PostCompletionRoute.RETIRE)
_DIRTY_HOLD = CompletionHandling(
    PostCompletionRoute.DIRTY_HOLD, exclude_same_cycle_reclaim=True
)

_HANDLING: dict[tuple[type, str], CompletionHandling] = {}


def _declare(
    variant: type, actions: tuple[str, ...], handling: CompletionHandling
) -> None:
    for action in actions:
        key = (variant, action)
        if key in _HANDLING:
            raise ValueError(f"duplicate completion classification: {key}")
        _HANDLING[key] = handling


# --- Events that reach the post-completion handler -------------------------
_declare(WorktreeCompletion, ("completed", "already_merged"), _HISTORY_RECEIPT)
_declare(WorktreeCompletion, ("escalated_token_limit_exceeded",), _HISTORY)
_declare(
    WorktreeCompletion,
    (
        "completed_no_commits",
        "completed_orphaned_pr",
        "completed_without_outcome",
        "blocked_base_branch_red",
        "escalated_base_branch_red",
        "blocked_review_timeout",
        "escalated_review_timeout",
        "blocked_unknown_reason",
    ),
    _RETIRE,
)
_declare(EarlyDeathRequeuedCompletion, ("early_death_requeued",), _RETIRE)
_declare(ReviewTimeoutRequeuedCompletion, ("blocked_review_timeout",), _RETIRE)
_declare(
    WorktreeCompletionHold,
    ("completion_skipped_forge_error", "completion_skipped_prior_merge_indeterminate"),
    _KEEP_EXCLUDED,
)
_declare(WorktreeCompletionHold, ("completion_skipped_dirty_worktree",), _DIRTY_HOLD)
# The not-needed rule reports through the same handler (#1349).
_declare(TaskWorktreeCompletion, ("not_needed",), _HISTORY_RECEIPT)
_declare(TaskWorktreeCompletion, ("not_needed_review_dispatched",), _RETIRE)
# The task-level dirty hold takes a different path from WorktreeCompletionHold,
# so it is not routed to DIRTY_HOLD here.
_declare(TaskWorktreeCompletion, ("completion_skipped_dirty_worktree",), _KEEP_EXCLUDED)
_declare(
    TaskWorktreeCompletion,
    ("abandoned_pr_requeued", "escalated_reclaim_limit_exceeded"),
    _KEEP,
)
_declare(ForgeFailureCompletion, ("completion_skipped_forge_error",), _KEEP_EXCLUDED)

# --- Variants whose producers settle everything themselves: KEEP -----------
_declare(
    UsageLimitCompletion,
    ("usage_limit_requeued", "usage_limit_escalated", "usage_limit_held"),
    _KEEP,
)
_declare(DirtyWorktreeEscalatedCompletion, ("escalated_reclaim_limit_exceeded",), _KEEP)
_declare(ActiveReservationHold, ("completion_reserved_hold",), _KEEP)
_declare(UnclaimedReservationHold, ("completion_reserved_hold",), _KEEP)
_declare(ExternalExecutionHeldCompletion, ("external_execution_held",), _KEEP)
_declare(ConfirmedExternalExecutionHeldCompletion, ("external_execution_held",), _KEEP)
_declare(AbandonedExternalExecutionHeldCompletion, ("external_execution_held",), _KEEP)
_declare(HandoffPreviewCompletion, ("completion_handoff_preview",), _KEEP)
_declare(
    HandoffCollectionCompletion,
    (
        "completion_handoff_held",
        "completion_handoff_failed",
        "completion_handoff_released",
    ),
    _KEEP,
)
_declare(
    PolicyProgressCompletion,
    ("completion_policy_pending", "completion_policy_applied"),
    _KEEP,
)
_declare(PolicyHoldCompletion, ("completion_policy_hold",), _KEEP)
_declare(PolicyUnavailableCompletion, ("completion_policy_hold",), _KEEP)
_declare(TokenHoldCompletion, ("completion_token_hold",), _KEEP)
_declare(InteractiveExcludedCompletion, ("gc_reclaim_excluded_interactive",), _KEEP)
_declare(CompletingExcludedCompletion, ("gc_reclaim_excluded_completing",), _KEEP)
_declare(
    ReclaimedCompletion, ("gc_reclaimed", "escalated_reclaim_limit_exceeded"), _KEEP
)
_declare(StaleEntryDiscardedCompletion, ("stale_active_entry_discarded",), _KEEP)
# #1526: the closed-target release already removed (or previewed) the entry.
_declare(
    InteractiveClosedTargetReleased,
    (
        "gc_released_interactive_closed_target",
        "gc_would_release_interactive_closed_target",
    ),
    _KEEP,
)
_declare(
    InteractiveClosedTargetHeld, ("gc_release_held_interactive_closed_target",), _KEEP
)
_declare(
    AbandonmentPersistenceFailureCompletion,
    ("abandonment_skipped_persistence_failure",),
    _KEEP,
)
_declare(
    ChangesRequestedEscalationCompletion, ("escalated_due_to_changes_requested",), _KEEP
)
_declare(
    PriorMergeEvidenceCompletion,
    (
        "not_found",
        "already_merged",
        "already_merged_dry_run",
        "already_merged_repair_pending",
        "indeterminate",
        "prior_merge_changed_before_repair",
    ),
    _KEEP,
)

_HOLD_ACTIONS: frozenset[str] = frozenset(
    get_args(WorktreeCompletionHoldAction.__value__)
)


def completion_handling_for(variant: type, action: str) -> CompletionHandling:
    """Return the declared post-processing for one event variant and action."""
    try:
        return _HANDLING[(variant, action)]
    except KeyError:
        raise UnclassifiedCompletionError(
            f"unclassified completion event: {variant.__name__}.{action!r}"
        ) from None


def completion_handling(event: CompletionEvent) -> CompletionHandling:
    """Classify a typed event; raise before any side effect if it is unknown."""
    return completion_handling_for(type(event), event.action)


def is_decision_hold_action(action: str) -> TypeGuard[WorktreeCompletionHoldAction]:
    """Return whether a decision action is a shared same-cycle hold action."""
    return action in _HOLD_ACTIONS
