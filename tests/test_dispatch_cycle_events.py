from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from orchestune.dag.models import FootprintConflict
from orchestune.dispatch.cycle_events import (
    AbandonedExternalExecutionHeldCompletion,
    AbandonmentPersistenceFailureCompletion,
    ActiveReservationHold,
    AlreadyForcedSerialDeviation,
    ChangesRequestedEscalationCompletion,
    CompletingExcludedCompletion,
    ConfirmedExternalExecutionHeldCompletion,
    DeviationConflict,
    DeviationEvent,
    DirtyWorktreeEscalatedCompletion,
    EarlyDeathRequeuedCompletion,
    ExternalExecutionHeldCompletion,
    ForcedSerialDeviation,
    ForgeFailureCompletion,
    HandoffCollectionCompletion,
    HandoffPreviewCompletion,
    InteractiveExcludedCompletion,
    PolicyHoldCompletion,
    PolicyProgressCompletion,
    PolicyUnavailableCompletion,
    PriorMergeEvidenceCompletion,
    PromotionEvent,
    ReclaimedCompletion,
    RecomputedDeviation,
    ReviewTimeoutRequeuedCompletion,
    StaleEntryDiscardedCompletion,
    TaskWorktreeCompletion,
    TokenHoldCompletion,
    UnclaimedReservationHold,
    UnknownSubtaskDeviation,
    UsageLimitCompletion,
    WorktreeCompletion,
    WorktreeCompletionHold,
    freeze_json,
)
from orchestune.models import Usage


def test_deviation_serializers_preserve_schema_and_key_order() -> None:
    cases = [
        (
            AlreadyForcedSerialDeviation(issue_number=1, deviated_files=("a.py",)),
            {
                "issue_number": 1,
                "deviated_files": ["a.py"],
                "action": "already_forced_serial",
            },
        ),
        (
            UnknownSubtaskDeviation(issue_number=2, deviated_files=()),
            {
                "issue_number": 2,
                "deviated_files": [],
                "action": "skipped_unknown_subtask",
            },
        ),
        (
            ForcedSerialDeviation(
                issue_number=3, deviated_files=("b.py",), recompute_count=4
            ),
            {
                "issue_number": 3,
                "deviated_files": ["b.py"],
                "action": "forced_serial",
                "recompute_count": 4,
            },
        ),
        (
            RecomputedDeviation(issue_number=4, deviated_files=(), conflicts=()),
            {
                "issue_number": 4,
                "deviated_files": [],
                "action": "recomputed",
                "conflicts": [],
            },
        ),
    ]

    for event, expected in cases:
        actual = cast(DeviationEvent, event).to_dict()
        assert actual == expected
        assert list(actual) == list(expected)


def test_deviation_conflict_is_a_detached_immutable_snapshot() -> None:
    resources = ["file:a.py"]
    source = FootprintConflict(
        subtask_id="a",
        other_subtask_id="b",
        similarity=0.5,
        blocked_subtask_id="b",
        resources=resources,  # type: ignore[arg-type]
    )
    conflict = DeviationConflict.from_conflict(source)
    resources.append("file:b.py")

    assert conflict.to_dict() == {
        "subtask_id": "a",
        "other_subtask_id": "b",
        "similarity": 0.5,
        "blocked_subtask_id": "b",
        "reason": "similarity",
        "resources": ("file:a.py",),
    }
    with pytest.raises(FrozenInstanceError):
        conflict.blocked_subtask_id = "changed"  # type: ignore[misc]


def test_deviation_and_promotion_events_are_frozen_and_rebuild_output() -> None:
    event = RecomputedDeviation(
        issue_number=5,
        deviated_files=("c.py",),
        conflicts=(
            DeviationConflict(
                subtask_id="a",
                other_subtask_id="b",
                similarity=1.0,
                blocked_subtask_id="b",
            ),
        ),
    )
    first = event.to_dict()
    cast(list[str], first["deviated_files"]).append("mutated.py")
    cast(list[object], first["conflicts"]).clear()
    assert event.to_dict()["deviated_files"] == ["c.py"]
    assert len(cast(list[object], event.to_dict()["conflicts"])) == 1

    promotion = PromotionEvent(issue_number=6, subtask_id="task-z")
    assert promotion.to_dict() == {"issue_number": 6, "subtask_id": "task-z"}
    assert list(promotion.to_dict()) == ["issue_number", "subtask_id"]
    with pytest.raises(FrozenInstanceError):
        promotion.issue_number = 7  # type: ignore[misc]


def test_completion_variants_keep_their_distinct_key_order_and_null_contracts() -> None:
    cases = [
        (
            WorktreeCompletion(
                issue_number=1,
                worktree_path="w",
                action="completed",
                usage=Usage(1, 2, 3),
                subtask_id="task-a",
                commit_sha="abc",
            ),
            (
                "issue_number",
                "worktree_path",
                "action",
                "usage",
                "subtask_id",
                "commit_sha",
            ),
        ),
        (
            WorktreeCompletion(
                issue_number=2, worktree_path="w", action="completed_no_commits"
            ),
            ("issue_number", "worktree_path", "action", "commit_sha"),
        ),
        (
            EarlyDeathRequeuedCompletion(
                issue_number=3, worktree_path="w", early_death_retry_at=4.5
            ),
            (
                "issue_number",
                "worktree_path",
                "action",
                "commit_sha",
                "early_death_retry_at",
            ),
        ),
        (
            ReviewTimeoutRequeuedCompletion(
                issue_number=4, worktree_path="w", review_timeout_retry_at=5.5
            ),
            (
                "issue_number",
                "worktree_path",
                "action",
                "commit_sha",
                "review_timeout_retry_at",
            ),
        ),
        (
            WorktreeCompletionHold(
                issue_number=5,
                worktree_path="w",
                action="completion_skipped_forge_error",
                error="",
            ),
            ("issue_number", "worktree_path", "action", "error"),
        ),
        (
            TaskWorktreeCompletion(
                issue_number=6,
                subtask_id="task-a",
                worktree_path="w",
                action="not_needed",
            ),
            ("issue_number", "subtask_id", "worktree_path", "action"),
        ),
        (
            DirtyWorktreeEscalatedCompletion(issue_number=7, worktree_path="w"),
            ("issue_number", "worktree_path", "action"),
        ),
        (
            ActiveReservationHold(issue_number=8, worktree_path="w"),
            ("issue_number", "worktree_path", "action"),
        ),
        (
            UnclaimedReservationHold(
                issue_number=9,
                completion_id="c",
                generation_id="g",
                reason="pending",
                downstream_policy_records=freeze_json([{"id": "p", "items": [1]}]),  # type: ignore[arg-type]
            ),
            (
                "issue_number",
                "completion_id",
                "generation_id",
                "action",
                "reason",
                "downstream_policy_records",
            ),
        ),
        (
            ExternalExecutionHeldCompletion(
                issue_number=10,
                subtask_id="task-a",
                reason="timeout",
                claim_id=None,
                launch_attempt_id=None,
                external_id=None,
                runtime_state="unknown",
            ),
            (
                "issue_number",
                "subtask_id",
                "action",
                "reason",
                "claim_id",
                "launch_attempt_id",
                "external_id",
                "runtime_state",
            ),
        ),
        (
            ConfirmedExternalExecutionHeldCompletion(
                issue_number=11,
                worktree_path="w",
                subtask_id="task-a",
                reason="stale",
                claim_id="c",
                launch_attempt_id="l",
                external_id="x",
                runtime_state="running",
            ),
            (
                "issue_number",
                "worktree_path",
                "action",
                "subtask_id",
                "reason",
                "claim_id",
                "launch_attempt_id",
                "external_id",
                "runtime_state",
            ),
        ),
        (
            AbandonedExternalExecutionHeldCompletion(
                issue_number=12, subtask_id="task-a", worktree_path="w"
            ),
            ("issue_number", "subtask_id", "worktree_path", "action"),
        ),
        (
            ForgeFailureCompletion(issue_number=13, worktree_path="w"),
            ("issue_number", "worktree_path", "action"),
        ),
        (
            HandoffPreviewCompletion(issue_number=14, worktree_path="w"),
            ("issue_number", "worktree_path", "action"),
        ),
        (
            HandoffCollectionCompletion(
                issue_number=15,
                worktree_path="w",
                action="completion_handoff_released",
                reason="approved",
            ),
            ("issue_number", "worktree_path", "action", "reason"),
        ),
        (
            PolicyProgressCompletion(
                issue_number=16,
                completion_id="c",
                generation_id="g",
                action="completion_policy_pending",
                reason="preview",
            ),
            ("issue_number", "completion_id", "generation_id", "action", "reason"),
        ),
        (
            PolicyHoldCompletion(issue_number=17, completion_id="c", reason="invalid"),
            ("issue_number", "completion_id", "action", "reason"),
        ),
        (
            PolicyUnavailableCompletion(issue_number=18),
            ("issue_number", "action", "reason"),
        ),
        (
            TokenHoldCompletion(issue_number=19, reason="OSError"),
            ("issue_number", "action", "reason"),
        ),
        (
            InteractiveExcludedCompletion(
                issue_number=20,
                subtask_id="task-a",
                reason="owner_kind",
                owner_kind="interactive",
            ),
            ("issue_number", "subtask_id", "action", "reason", "owner_kind"),
        ),
        (
            CompletingExcludedCompletion(
                issue_number=21,
                subtask_id="task-a",
                reason="completing",
                owner_kind="dispatch",
                completion_id="c",
                completion_stage="publishing",
            ),
            (
                "issue_number",
                "subtask_id",
                "action",
                "reason",
                "owner_kind",
                "completion_id",
                "completion_stage",
            ),
        ),
        (
            ReclaimedCompletion(
                issue_number=22,
                subtask_id="task-a",
                action="gc_reclaimed",
                reason="stale",
                reclaim_count=2,
            ),
            ("issue_number", "subtask_id", "action", "reason", "reclaim_count"),
        ),
        (
            StaleEntryDiscardedCompletion(
                issue_number=23, subtask_id="task-a", reason="missing"
            ),
            ("issue_number", "subtask_id", "action", "reason"),
        ),
        (
            AbandonmentPersistenceFailureCompletion(
                issue_number=24, subtask_id="task-a", worktree_path="w"
            ),
            ("issue_number", "subtask_id", "worktree_path", "action"),
        ),
        (
            ChangesRequestedEscalationCompletion(issue_number=25, subtask_id="task-a"),
            ("issue_number", "subtask_id", "action"),
        ),
        (
            PriorMergeEvidenceCompletion(
                issue_number=26,
                action="indeterminate",
                pr_number=None,
                base_ref="main",
                merged_at=None,
                reason="forge_error",
            ),
            ("issue_number", "action", "pr_number", "base_ref", "merged_at", "reason"),
        ),
        (
            UsageLimitCompletion(
                issue_number=12,
                action="usage_limit_requeued",
                target="claude-cli",
                reset_known=True,
                reset_at=100.0,
                timezone="Asia/Tokyo",
                retry_at=130.0,
                retries_remaining=1,
                subtask_id="task-a",
            ),
            (
                "issue_number",
                "subtask_id",
                "action",
                "target",
                "reset_known",
                "reset_at",
                "timezone",
                "retry_at",
                "retries_remaining",
            ),
        ),
        (
            UsageLimitCompletion(
                issue_number=13,
                action="usage_limit_held",
                target="claude-cli",
                reset_known=False,
                retries_remaining=0,
                reason="ledger_save_failed",
            ),
            (
                "issue_number",
                "action",
                "target",
                "reset_known",
                "retries_remaining",
                "reason",
            ),
        ),
    ]

    for event, expected_keys in cases:
        actual = event.to_dict()
        assert tuple(actual) == expected_keys

    assert cases[0][0].to_dict()["usage"] == {
        "input_tokens": 1,
        "output_tokens": 2,
        "total_tokens": 3,
        "model": None,
        "cost_usd": None,
    }
    assert cases[1][0].to_dict()["commit_sha"] is None
    assert cases[9][0].to_dict()["external_id"] is None


def test_unclaimed_reservation_snapshot_is_detached_and_output_is_fresh() -> None:
    source = [{"stage": "review", "labels": ["queued"]}]
    event = UnclaimedReservationHold(
        issue_number=27,
        completion_id="c",
        generation_id="g",
        reason="pending",
        downstream_policy_records=freeze_json(source),  # type: ignore[arg-type]
    )
    source[0]["labels"] = ["queued", "changed"]
    first = event.to_dict()
    cast(list[dict[str, object]], first["downstream_policy_records"])[0]["labels"] = [
        "mutated"
    ]
    assert event.to_dict()["downstream_policy_records"] == [
        {"stage": "review", "labels": ["queued"]}
    ]
