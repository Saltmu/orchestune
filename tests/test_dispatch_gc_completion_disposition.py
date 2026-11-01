"""#1349: 完了イベントの後処理分類は`completion_disposition`の1箇所に集約する。

期待値は分類関数の結果から作らず、Issue本文の対応表を独立に書き下したものを
固定する。新しいvariant/actionを`CompletionEvent`へ足して分類を忘れると、
網羅性の検査で失敗する。
"""

from __future__ import annotations

import ast
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast, get_args, get_type_hints

import pytest

from orchestune.dispatch import cycle_events
from orchestune.dispatch.cycle_events import (
    CompletionEvent,
    DirtyWorktreeEscalatedCompletion,
    EarlyDeathRequeuedCompletion,
    ForgeFailureCompletion,
    PriorMergeEvidenceCompletion,
    ReviewTimeoutRequeuedCompletion,
    TaskWorktreeCompletion,
    WorktreeCompletion,
    WorktreeCompletionHold,
)
from orchestune.dispatch.gc import completion_disposition as disposition
from orchestune.dispatch.gc.completion_disposition import (
    CompletionHandling,
    PostCompletionRoute,
    UnclassifiedCompletionError,
    completion_handling,
    completion_handling_for,
    is_decision_hold_action,
)

HISTORY = "HISTORY"
RETIRE = "RETIRE"
KEEP = "KEEP"
DIRTY_HOLD = "DIRTY_HOLD"

# (variant class name, action) -> (route, may_issue_receipt, exclude_same_cycle_reclaim)
_EXPECTED: dict[tuple[str, str], tuple[str, bool, bool]] = {
    ("WorktreeCompletion", "completed"): (HISTORY, True, False),
    ("WorktreeCompletion", "already_merged"): (HISTORY, True, False),
    ("WorktreeCompletion", "escalated_token_limit_exceeded"): (HISTORY, False, False),
    ("WorktreeCompletion", "completed_no_commits"): (RETIRE, False, False),
    ("WorktreeCompletion", "completed_orphaned_pr"): (RETIRE, False, False),
    ("WorktreeCompletion", "completed_without_outcome"): (RETIRE, False, False),
    ("WorktreeCompletion", "blocked_base_branch_red"): (RETIRE, False, False),
    ("WorktreeCompletion", "escalated_base_branch_red"): (RETIRE, False, False),
    ("WorktreeCompletion", "blocked_review_timeout"): (RETIRE, False, False),
    ("WorktreeCompletion", "escalated_review_timeout"): (RETIRE, False, False),
    ("WorktreeCompletion", "blocked_unknown_reason"): (RETIRE, False, False),
    ("EarlyDeathRequeuedCompletion", "early_death_requeued"): (RETIRE, False, False),
    ("ReviewTimeoutRequeuedCompletion", "blocked_review_timeout"): (
        RETIRE,
        False,
        False,
    ),
    ("UsageLimitCompletion", "usage_limit_requeued"): (KEEP, False, False),
    ("UsageLimitCompletion", "usage_limit_escalated"): (KEEP, False, False),
    ("UsageLimitCompletion", "usage_limit_held"): (KEEP, False, False),
    ("WorktreeCompletionHold", "completion_skipped_forge_error"): (KEEP, False, True),
    ("WorktreeCompletionHold", "completion_skipped_prior_merge_indeterminate"): (
        KEEP,
        False,
        True,
    ),
    ("WorktreeCompletionHold", "completion_skipped_dirty_worktree"): (
        DIRTY_HOLD,
        False,
        True,
    ),
    ("TaskWorktreeCompletion", "not_needed"): (HISTORY, True, False),
    ("TaskWorktreeCompletion", "not_needed_review_dispatched"): (RETIRE, False, False),
    ("TaskWorktreeCompletion", "abandoned_pr_requeued"): (KEEP, False, False),
    ("TaskWorktreeCompletion", "completion_skipped_dirty_worktree"): (
        KEEP,
        False,
        True,
    ),
    ("TaskWorktreeCompletion", "escalated_reclaim_limit_exceeded"): (
        KEEP,
        False,
        False,
    ),
    ("DirtyWorktreeEscalatedCompletion", "escalated_reclaim_limit_exceeded"): (
        KEEP,
        False,
        False,
    ),
    ("ActiveReservationHold", "completion_reserved_hold"): (KEEP, False, False),
    ("UnclaimedReservationHold", "completion_reserved_hold"): (KEEP, False, False),
    ("ExternalExecutionHeldCompletion", "external_execution_held"): (
        KEEP,
        False,
        False,
    ),
    ("ConfirmedExternalExecutionHeldCompletion", "external_execution_held"): (
        KEEP,
        False,
        False,
    ),
    ("AbandonedExternalExecutionHeldCompletion", "external_execution_held"): (
        KEEP,
        False,
        False,
    ),
    ("ForgeFailureCompletion", "completion_skipped_forge_error"): (KEEP, False, True),
    ("HandoffPreviewCompletion", "completion_handoff_preview"): (KEEP, False, False),
    ("HandoffCollectionCompletion", "completion_handoff_held"): (KEEP, False, False),
    ("HandoffCollectionCompletion", "completion_handoff_failed"): (KEEP, False, False),
    ("HandoffCollectionCompletion", "completion_handoff_released"): (
        KEEP,
        False,
        False,
    ),
    ("PolicyProgressCompletion", "completion_policy_pending"): (KEEP, False, False),
    ("PolicyProgressCompletion", "completion_policy_applied"): (KEEP, False, False),
    ("PolicyHoldCompletion", "completion_policy_hold"): (KEEP, False, False),
    ("PolicyUnavailableCompletion", "completion_policy_hold"): (KEEP, False, False),
    ("TokenHoldCompletion", "completion_token_hold"): (KEEP, False, False),
    ("InteractiveExcludedCompletion", "gc_reclaim_excluded_interactive"): (
        KEEP,
        False,
        False,
    ),
    ("CompletingExcludedCompletion", "gc_reclaim_excluded_completing"): (
        KEEP,
        False,
        False,
    ),
    ("ReclaimedCompletion", "gc_reclaimed"): (KEEP, False, False),
    ("ReclaimedCompletion", "escalated_reclaim_limit_exceeded"): (KEEP, False, False),
    ("StaleEntryDiscardedCompletion", "stale_active_entry_discarded"): (
        KEEP,
        False,
        False,
    ),
    ("InteractiveClosedTargetReleased", "gc_released_interactive_closed_target"): (
        KEEP,
        False,
        False,
    ),
    (
        "InteractiveClosedTargetReleased",
        "gc_would_release_interactive_closed_target",
    ): (KEEP, False, False),
    ("InteractiveClosedTargetHeld", "gc_release_held_interactive_closed_target"): (
        KEEP,
        False,
        False,
    ),
    (
        "AbandonmentPersistenceFailureCompletion",
        "abandonment_skipped_persistence_failure",
    ): (
        KEEP,
        False,
        False,
    ),
    ("ChangesRequestedEscalationCompletion", "escalated_due_to_changes_requested"): (
        KEEP,
        False,
        False,
    ),
    ("PriorMergeEvidenceCompletion", "not_found"): (KEEP, False, False),
    ("PriorMergeEvidenceCompletion", "already_merged"): (KEEP, False, False),
    ("PriorMergeEvidenceCompletion", "already_merged_dry_run"): (KEEP, False, False),
    ("PriorMergeEvidenceCompletion", "already_merged_repair_pending"): (
        KEEP,
        False,
        False,
    ),
    ("PriorMergeEvidenceCompletion", "indeterminate"): (KEEP, False, False),
    ("PriorMergeEvidenceCompletion", "prior_merge_changed_before_repair"): (
        KEEP,
        False,
        False,
    ),
}


def _expand(annotation: Any) -> Any:
    """`type X = ...`のaliasを展開する。型aliasの解決はこのhelperへ閉じる。"""
    while hasattr(annotation, "__value__"):
        annotation = annotation.__value__
    return annotation


def _variants() -> list[type]:
    return list(get_args(_expand(CompletionEvent)))


def _actions(variant: type) -> list[str]:
    hints = get_type_hints(variant, vars(cycle_events))
    return list(get_args(_expand(hints["action"])))


def _all_combinations() -> set[tuple[str, str]]:
    return {(v.__name__, a) for v in _variants() for a in _actions(v)}


def test_every_event_variant_and_action_is_classified_exactly_once() -> None:
    declared = _all_combinations()
    classified = set(_EXPECTED)
    assert declared - classified == set(), "unclassified (variant, action) combinations"
    assert classified - declared == set(), "classification rows without a real event"


def test_union_is_not_empty_and_action_expansion_works() -> None:
    assert len(_variants()) >= 20
    assert set(_actions(WorktreeCompletionHold)) == {
        "completion_skipped_dirty_worktree",
        "completion_skipped_forge_error",
        "completion_skipped_prior_merge_indeterminate",
    }


@pytest.mark.parametrize(
    ("variant", "action"), sorted(_EXPECTED), ids=lambda value: str(value)
)
def test_classification_matches_the_issue_table(variant: str, action: str) -> None:
    cls = next(v for v in _variants() if v.__name__ == variant)
    route, receipt, exclude = _EXPECTED[(variant, action)]

    handling = completion_handling_for(cls, action)

    assert handling == CompletionHandling(
        route=PostCompletionRoute[route],
        may_issue_receipt=receipt,
        exclude_same_cycle_reclaim=exclude,
    )


def test_receipt_candidates_are_only_the_confirmed_completion_actions() -> None:
    candidates = {key for key, (_, receipt, _) in _EXPECTED.items() if receipt}
    assert candidates == {
        ("WorktreeCompletion", "completed"),
        ("WorktreeCompletion", "already_merged"),
        ("TaskWorktreeCompletion", "not_needed"),
    }
    for variant, action in _EXPECTED:
        cls = next(v for v in _variants() if v.__name__ == variant)
        handling = completion_handling_for(cls, action)
        if handling.may_issue_receipt:
            assert handling.route is PostCompletionRoute.HISTORY


def test_same_cycle_reclaim_exclusion_is_limited_to_the_known_holds() -> None:
    excluded = {key for key, (_, _, exclude) in _EXPECTED.items() if exclude}
    assert excluded == {
        ("WorktreeCompletionHold", "completion_skipped_forge_error"),
        ("WorktreeCompletionHold", "completion_skipped_prior_merge_indeterminate"),
        ("WorktreeCompletionHold", "completion_skipped_dirty_worktree"),
        ("TaskWorktreeCompletion", "completion_skipped_dirty_worktree"),
        ("ForgeFailureCompletion", "completion_skipped_forge_error"),
    }


def test_same_action_name_on_different_dtos_is_classified_by_variant() -> None:
    # Same string, different meaning: only the worktree completion is history.
    assert (
        completion_handling_for(WorktreeCompletion, "already_merged").route
        is PostCompletionRoute.HISTORY
    )
    assert (
        completion_handling_for(PriorMergeEvidenceCompletion, "already_merged").route
        is PostCompletionRoute.KEEP
    )
    # Same dirty action: only the hold DTO routes to the dirty-hold handling.
    assert (
        completion_handling_for(
            WorktreeCompletionHold, "completion_skipped_dirty_worktree"
        ).route
        is PostCompletionRoute.DIRTY_HOLD
    )
    assert (
        completion_handling_for(
            TaskWorktreeCompletion, "completion_skipped_dirty_worktree"
        ).route
        is PostCompletionRoute.KEEP
    )


def test_completion_handling_reads_the_typed_event() -> None:
    completed = WorktreeCompletion(
        issue_number=1, worktree_path="w", action="completed"
    )
    assert completion_handling(completed).route is PostCompletionRoute.HISTORY
    assert completion_handling(completed).may_issue_receipt is True
    assert (
        completion_handling(
            EarlyDeathRequeuedCompletion(
                issue_number=1, worktree_path="w", early_death_retry_at=1.0
            )
        ).route
        is PostCompletionRoute.RETIRE
    )
    assert (
        completion_handling(
            ReviewTimeoutRequeuedCompletion(
                issue_number=1, worktree_path="w", review_timeout_retry_at=1.0
            )
        ).route
        is PostCompletionRoute.RETIRE
    )
    forge = ForgeFailureCompletion(issue_number=1, worktree_path="w")
    assert completion_handling(forge).exclude_same_cycle_reclaim is True
    escalated = DirtyWorktreeEscalatedCompletion(issue_number=1, worktree_path="w")
    assert completion_handling(escalated).route is PostCompletionRoute.KEEP


def test_unknown_action_or_variant_is_an_explicit_error_not_a_silent_keep() -> None:
    bogus = WorktreeCompletion(
        issue_number=1,
        worktree_path="w",
        action=cast(typing.Any, "brand_new_action"),
    )
    with pytest.raises(UnclassifiedCompletionError, match="brand_new_action"):
        completion_handling(bogus)

    @dataclass(frozen=True)
    class BrandNewCompletion:
        action: str = "completed"

    with pytest.raises(UnclassifiedCompletionError, match="BrandNewCompletion"):
        completion_handling(cast(CompletionEvent, BrandNewCompletion()))
    # A known action name does not make an unknown variant classified.
    with pytest.raises(UnclassifiedCompletionError):
        completion_handling_for(BrandNewCompletion, "completed")


def test_hold_action_vocabulary_has_one_source() -> None:
    hold_actions = set(get_args(_expand(cycle_events.WorktreeCompletionHoldAction)))
    assert hold_actions == {
        "completion_skipped_dirty_worktree",
        "completion_skipped_forge_error",
        "completion_skipped_prior_merge_indeterminate",
    }
    for action in hold_actions:
        assert is_decision_hold_action(action) is True
    for action in (
        "completed",
        "not_needed",
        "escalated_reclaim_limit_exceeded",
        "external_execution_held",
        "",
    ):
        assert is_decision_hold_action(action) is False
    # TaskWorktreeCompletion reuses the shared alias rather than its own Literal.
    hints = get_type_hints(TaskWorktreeCompletion, vars(cycle_events))
    assert hints["action"] is cycle_events.TaskWorktreeCompletionAction


def test_disposition_module_has_no_effectful_dependencies() -> None:
    tree = ast.parse(Path(disposition.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    internal = {name for name in imported if name.startswith("orchestune")}
    assert internal <= {"orchestune.dispatch.cycle_events"}, internal
