"""Typed immutable payloads used by dispatch cycle reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Literal

from orchestune.dag.models import FootprintConflict
from orchestune.models import Usage


@dataclass(frozen=True, slots=True, kw_only=True)
class DeviationConflict:
    """Immutable report snapshot of a footprint conflict."""

    subtask_id: str
    other_subtask_id: str
    similarity: float
    blocked_subtask_id: str
    reason: str = "similarity"
    resources: tuple[str, ...] = ()

    @classmethod
    def from_conflict(cls, conflict: FootprintConflict) -> DeviationConflict:
        return cls(
            subtask_id=conflict.subtask_id,
            other_subtask_id=conflict.other_subtask_id,
            similarity=conflict.similarity,
            blocked_subtask_id=conflict.blocked_subtask_id,
            reason=conflict.reason,
            resources=tuple(conflict.resources),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "subtask_id": self.subtask_id,
            "other_subtask_id": self.other_subtask_id,
            "similarity": self.similarity,
            "blocked_subtask_id": self.blocked_subtask_id,
            "reason": self.reason,
            "resources": self.resources,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class AlreadyForcedSerialDeviation:
    issue_number: int
    deviated_files: tuple[str, ...]
    action: Literal["already_forced_serial"] = field(
        default="already_forced_serial", init=False
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "issue_number": self.issue_number,
            "deviated_files": list(self.deviated_files),
            "action": self.action,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class UnknownSubtaskDeviation:
    issue_number: int
    deviated_files: tuple[str, ...]
    action: Literal["skipped_unknown_subtask"] = field(
        default="skipped_unknown_subtask", init=False
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "issue_number": self.issue_number,
            "deviated_files": list(self.deviated_files),
            "action": self.action,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ForcedSerialDeviation:
    issue_number: int
    deviated_files: tuple[str, ...]
    recompute_count: int
    action: Literal["forced_serial"] = field(default="forced_serial", init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "issue_number": self.issue_number,
            "deviated_files": list(self.deviated_files),
            "action": self.action,
            "recompute_count": self.recompute_count,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RecomputedDeviation:
    issue_number: int
    deviated_files: tuple[str, ...]
    conflicts: tuple[DeviationConflict, ...]
    action: Literal["recomputed"] = field(default="recomputed", init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "issue_number": self.issue_number,
            "deviated_files": list(self.deviated_files),
            "action": self.action,
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
        }


type DeviationEvent = (
    AlreadyForcedSerialDeviation
    | UnknownSubtaskDeviation
    | ForcedSerialDeviation
    | RecomputedDeviation
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PromotionEvent:
    issue_number: int
    subtask_id: str

    def to_dict(self) -> dict[str, object]:
        return {"issue_number": self.issue_number, "subtask_id": self.subtask_id}


type FrozenJsonScalar = None | bool | int | float | str


@dataclass(frozen=True, slots=True, kw_only=True)
class FrozenJsonArray:
    values: tuple[FrozenJsonValue, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class FrozenJsonObject:
    items: tuple[tuple[str, FrozenJsonValue], ...]


type FrozenJsonValue = FrozenJsonScalar | FrozenJsonArray | FrozenJsonObject


def freeze_json(value: object) -> FrozenJsonValue:
    """Copy JSON-shaped data into immutable report-owned containers."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return FrozenJsonObject(
            items=tuple((key, freeze_json(item)) for key, item in value.items())
        )
    if isinstance(value, list | tuple):
        return FrozenJsonArray(values=tuple(freeze_json(item) for item in value))
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def thaw_json(value: FrozenJsonValue) -> object:
    if isinstance(value, FrozenJsonObject):
        return {key: thaw_json(item) for key, item in value.items}
    if isinstance(value, FrozenJsonArray):
        return [thaw_json(item) for item in value.values]
    return value


def _usage_dict(usage: Usage) -> dict[str, object]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "model": usage.model,
        "cost_usd": usage.cost_usd,
    }


def _event_value(value: object) -> object:
    if isinstance(value, Usage):
        return _usage_dict(value)
    if isinstance(value, FrozenJsonObject | FrozenJsonArray):
        return thaw_json(value)
    if isinstance(value, tuple):
        return [item.to_dict() if hasattr(item, "to_dict") else item for item in value]
    return value


class _SerializedCompletion:
    """Base for DTOs with an explicit, ordered serializer field list."""

    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]]
    _OMIT_NONE: ClassVar[frozenset[str]] = frozenset()

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for name in self._SERIALIZE_FIELDS:
            value = getattr(self, name)
            if value is None and name in self._OMIT_NONE:
                continue
            result[name] = _event_value(value)
        return result


type WorktreeCompletionAction = Literal[
    "completed",
    "completed_no_commits",
    "completed_without_outcome",
    "already_merged",
    "blocked_base_branch_red",
    "escalated_base_branch_red",
    "blocked_review_timeout",
    "escalated_review_timeout",
    "blocked_unknown_reason",
    "escalated_token_limit_exceeded",
]
type WorktreeCompletionHoldAction = Literal[
    "completion_skipped_dirty_worktree",
    "completion_skipped_forge_error",
    "completion_skipped_prior_merge_indeterminate",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class WorktreeCompletion(_SerializedCompletion):
    issue_number: int
    worktree_path: str
    action: WorktreeCompletionAction
    usage: Usage | None = None
    subtask_id: str | None = None
    commit_sha: str | None = None
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "worktree_path",
        "action",
        "usage",
        "subtask_id",
        "commit_sha",
    )
    _OMIT_NONE: ClassVar[frozenset[str]] = frozenset({"usage", "subtask_id"})


@dataclass(frozen=True, slots=True, kw_only=True)
class EarlyDeathRequeuedCompletion(_SerializedCompletion):
    issue_number: int
    worktree_path: str
    early_death_retry_at: float
    usage: Usage | None = None
    subtask_id: str | None = None
    action: Literal["early_death_requeued"] = field(
        default="early_death_requeued", init=False
    )
    commit_sha: None = field(default=None, init=False)
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "worktree_path",
        "action",
        "usage",
        "subtask_id",
        "commit_sha",
        "early_death_retry_at",
    )
    _OMIT_NONE: ClassVar[frozenset[str]] = frozenset({"usage", "subtask_id"})


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewTimeoutRequeuedCompletion(_SerializedCompletion):
    issue_number: int
    worktree_path: str
    review_timeout_retry_at: float
    usage: Usage | None = None
    subtask_id: str | None = None
    action: Literal["blocked_review_timeout"] = field(
        default="blocked_review_timeout", init=False
    )
    commit_sha: None = field(default=None, init=False)
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "worktree_path",
        "action",
        "usage",
        "subtask_id",
        "commit_sha",
        "review_timeout_retry_at",
    )
    _OMIT_NONE: ClassVar[frozenset[str]] = frozenset({"usage", "subtask_id"})


type UsageLimitAction = Literal[
    "usage_limit_requeued",
    "usage_limit_escalated",
    "usage_limit_held",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageLimitCompletion(_SerializedCompletion):
    """#1270: a claude-cli run ended on its session usage limit.

    ``reset_known`` says whether the message gave a reset time that could be resolved
    (``reset_at`` is then UTC epoch seconds and ``timezone`` the zone it was read in);
    otherwise ``retry_at`` comes from the finite backoff. ``retries_remaining`` counts
    the additional launches still allowed after this one. ``usage_limit_held`` means
    nothing was changed and the exit is reconsidered on the next cycle.
    """

    issue_number: int
    action: UsageLimitAction
    target: str
    reset_known: bool
    retries_remaining: int
    subtask_id: str | None = None
    reset_at: float | None = None
    timezone: str | None = None
    retry_at: float | None = None
    reason: str | None = None
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "subtask_id",
        "action",
        "target",
        "reset_known",
        "reset_at",
        "timezone",
        "retry_at",
        "retries_remaining",
        "reason",
    )
    _OMIT_NONE: ClassVar[frozenset[str]] = frozenset(
        {"subtask_id", "reset_at", "timezone", "retry_at", "reason"}
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class WorktreeCompletionHold(_SerializedCompletion):
    issue_number: int
    worktree_path: str
    action: WorktreeCompletionHoldAction
    usage: Usage | None = None
    operation: str | None = None
    error: str | None = None
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "worktree_path",
        "action",
        "usage",
        "operation",
        "error",
    )
    _OMIT_NONE: ClassVar[frozenset[str]] = frozenset({"usage", "operation", "error"})


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskWorktreeCompletion(_SerializedCompletion):
    issue_number: int
    subtask_id: str
    worktree_path: str
    action: Literal[
        "not_needed",
        "not_needed_review_dispatched",
        "abandoned_pr_requeued",
        "completion_skipped_dirty_worktree",
        "escalated_reclaim_limit_exceeded",
    ]
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "subtask_id",
        "worktree_path",
        "action",
    )


type TaskWorktreeCompletionAction = Literal[
    "not_needed",
    "not_needed_review_dispatched",
    "abandoned_pr_requeued",
    "completion_skipped_dirty_worktree",
    "escalated_reclaim_limit_exceeded",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class DirtyWorktreeEscalatedCompletion(_SerializedCompletion):
    issue_number: int
    worktree_path: str
    action: Literal["escalated_reclaim_limit_exceeded"] = field(
        default="escalated_reclaim_limit_exceeded", init=False
    )
    usage: Usage | None = None
    operation: str | None = None
    error: str | None = None
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "worktree_path",
        "action",
        "usage",
        "operation",
        "error",
    )
    _OMIT_NONE: ClassVar[frozenset[str]] = frozenset({"usage", "operation", "error"})


@dataclass(frozen=True, slots=True, kw_only=True)
class ActiveReservationHold(_SerializedCompletion):
    issue_number: int
    worktree_path: str
    reason: str | None = None
    action: Literal["completion_reserved_hold"] = field(
        default="completion_reserved_hold", init=False
    )
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "worktree_path",
        "action",
        "reason",
    )
    _OMIT_NONE: ClassVar[frozenset[str]] = frozenset({"reason"})


@dataclass(frozen=True, slots=True, kw_only=True)
class UnclaimedReservationHold(_SerializedCompletion):
    issue_number: int
    completion_id: str
    generation_id: str
    reason: str
    downstream_policy_records: FrozenJsonValue
    action: Literal["completion_reserved_hold"] = field(
        default="completion_reserved_hold", init=False
    )
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "completion_id",
        "generation_id",
        "action",
        "reason",
        "downstream_policy_records",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ExternalExecutionHeldCompletion(_SerializedCompletion):
    issue_number: int
    subtask_id: str
    reason: str
    claim_id: str | None
    launch_attempt_id: str | None
    external_id: str | None
    runtime_state: str
    action: Literal["external_execution_held"] = field(
        default="external_execution_held", init=False
    )
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "subtask_id",
        "action",
        "reason",
        "claim_id",
        "launch_attempt_id",
        "external_id",
        "runtime_state",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfirmedExternalExecutionHeldCompletion(_SerializedCompletion):
    issue_number: int
    worktree_path: str
    subtask_id: str
    reason: str
    claim_id: str | None
    launch_attempt_id: str | None
    external_id: str | None
    runtime_state: str
    action: Literal["external_execution_held"] = field(
        default="external_execution_held", init=False
    )
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "worktree_path",
        "action",
        "subtask_id",
        "reason",
        "claim_id",
        "launch_attempt_id",
        "external_id",
        "runtime_state",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class AbandonedExternalExecutionHeldCompletion(_SerializedCompletion):
    issue_number: int
    subtask_id: str
    worktree_path: str
    action: Literal["external_execution_held"] = field(
        default="external_execution_held", init=False
    )
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "subtask_id",
        "worktree_path",
        "action",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ForgeFailureCompletion(_SerializedCompletion):
    issue_number: int
    worktree_path: str
    operation: str | None = None
    error: str | None = None
    action: Literal["completion_skipped_forge_error"] = field(
        default="completion_skipped_forge_error", init=False
    )
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "worktree_path",
        "action",
        "operation",
        "error",
    )
    _OMIT_NONE: ClassVar[frozenset[str]] = frozenset({"operation", "error"})


@dataclass(frozen=True, slots=True, kw_only=True)
class HandoffPreviewCompletion(_SerializedCompletion):
    issue_number: int
    worktree_path: str
    action: Literal["completion_handoff_preview"] = field(
        default="completion_handoff_preview", init=False
    )
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "worktree_path",
        "action",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class HandoffCollectionCompletion(_SerializedCompletion):
    issue_number: int
    worktree_path: str
    action: Literal[
        "completion_handoff_held",
        "completion_handoff_failed",
        "completion_handoff_released",
    ]
    reason: str
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "worktree_path",
        "action",
        "reason",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyProgressCompletion(_SerializedCompletion):
    issue_number: int
    completion_id: str
    generation_id: str
    action: Literal["completion_policy_pending", "completion_policy_applied"]
    reason: str | None = None
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "completion_id",
        "generation_id",
        "action",
        "reason",
    )
    _OMIT_NONE: ClassVar[frozenset[str]] = frozenset({"reason"})


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyHoldCompletion(_SerializedCompletion):
    issue_number: int
    completion_id: str
    reason: str
    action: Literal["completion_policy_hold"] = field(
        default="completion_policy_hold", init=False
    )
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "completion_id",
        "action",
        "reason",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyUnavailableCompletion(_SerializedCompletion):
    issue_number: int
    reason: Literal["outcome_unknown"] = field(default="outcome_unknown", init=False)
    action: Literal["completion_policy_hold"] = field(
        default="completion_policy_hold", init=False
    )
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "action",
        "reason",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenHoldCompletion(_SerializedCompletion):
    issue_number: int
    reason: str
    action: Literal["completion_token_hold"] = field(
        default="completion_token_hold", init=False
    )
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "action",
        "reason",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class InteractiveExcludedCompletion(_SerializedCompletion):
    issue_number: int
    subtask_id: str
    reason: str
    owner_kind: str
    action: Literal["gc_reclaim_excluded_interactive"] = field(
        default="gc_reclaim_excluded_interactive", init=False
    )
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "subtask_id",
        "action",
        "reason",
        "owner_kind",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletingExcludedCompletion(_SerializedCompletion):
    issue_number: int
    subtask_id: str
    reason: str
    owner_kind: str
    completion_id: str
    completion_stage: str
    action: Literal["gc_reclaim_excluded_completing"] = field(
        default="gc_reclaim_excluded_completing", init=False
    )
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "subtask_id",
        "action",
        "reason",
        "owner_kind",
        "completion_id",
        "completion_stage",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ReclaimedCompletion(_SerializedCompletion):
    issue_number: int
    subtask_id: str
    reason: str
    reclaim_count: int
    action: Literal["gc_reclaimed", "escalated_reclaim_limit_exceeded"]
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "subtask_id",
        "action",
        "reason",
        "reclaim_count",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class StaleEntryDiscardedCompletion(_SerializedCompletion):
    issue_number: int
    subtask_id: str
    reason: str
    action: Literal["stale_active_entry_discarded"] = field(
        default="stale_active_entry_discarded", init=False
    )
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "subtask_id",
        "action",
        "reason",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class AbandonmentPersistenceFailureCompletion(_SerializedCompletion):
    issue_number: int
    subtask_id: str
    worktree_path: str
    action: Literal["abandonment_skipped_persistence_failure"] = field(
        default="abandonment_skipped_persistence_failure", init=False
    )
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "subtask_id",
        "worktree_path",
        "action",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ChangesRequestedEscalationCompletion(_SerializedCompletion):
    issue_number: int
    subtask_id: str
    action: Literal["escalated_due_to_changes_requested"] = field(
        default="escalated_due_to_changes_requested", init=False
    )
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "subtask_id",
        "action",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class PriorMergeEvidenceCompletion(_SerializedCompletion):
    issue_number: int
    pr_number: int | None
    base_ref: str
    merged_at: str | None
    reason: str
    action: Literal[
        "not_found",
        "already_merged",
        "already_merged_dry_run",
        "already_merged_repair_pending",
        "indeterminate",
        "prior_merge_changed_before_repair",
    ]
    _SERIALIZE_FIELDS: ClassVar[tuple[str, ...]] = (
        "issue_number",
        "action",
        "pr_number",
        "base_ref",
        "merged_at",
        "reason",
    )
    _OMIT_NONE: ClassVar[frozenset[str]] = frozenset()


type CompletionEvent = (
    WorktreeCompletion
    | EarlyDeathRequeuedCompletion
    | ReviewTimeoutRequeuedCompletion
    | UsageLimitCompletion
    | WorktreeCompletionHold
    | TaskWorktreeCompletion
    | DirtyWorktreeEscalatedCompletion
    | ActiveReservationHold
    | UnclaimedReservationHold
    | ExternalExecutionHeldCompletion
    | ConfirmedExternalExecutionHeldCompletion
    | AbandonedExternalExecutionHeldCompletion
    | ForgeFailureCompletion
    | HandoffPreviewCompletion
    | HandoffCollectionCompletion
    | PolicyProgressCompletion
    | PolicyHoldCompletion
    | PolicyUnavailableCompletion
    | TokenHoldCompletion
    | InteractiveExcludedCompletion
    | CompletingExcludedCompletion
    | ReclaimedCompletion
    | StaleEntryDiscardedCompletion
    | AbandonmentPersistenceFailureCompletion
    | ChangesRequestedEscalationCompletion
    | PriorMergeEvidenceCompletion
)
