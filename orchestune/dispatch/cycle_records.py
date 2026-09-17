"""Frozen receipts issued by GC completion/transition decisions (#882, #883).

A `CompletionReceipt` is minted only by the two GC completion decisions that
are genuinely confirmed: a normal `"completed"` action (a `RESULT_DONE`
Outcome Record backed by new commits) and a *verified* `"already_merged"`
action (`decide_prior_parent_merge_completion`'s double-checked historical
merge). It must never be inferred from a `"completed"`-prefix match on the
action string (`"completed_no_commits"` / `"completed_without_outcome"` also
share that prefix but are not confirmed completions) or from diffing
`completed_worktrees` history.

`apply_verified_transition` is the analogous bridge for status/recovery
transitions: it consumes a `VerifiedStatusTransition` (live-verified state,
proven by a caller-specific mechanism — an intent journal for the status
executor, a direct live re-fetch for recompute/base-branch-red recovery) and
reflects it into `CycleContext` via `record_transition`, but only once the
caller can state `execution_active` authoritatively. `execution_active=None`
means the caller's execution observation is unknown this cycle, and the
receipt is held rather than guessed at.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from orchestune.consistency.invariants.status import primary_status_labels
from orchestune.dispatch.cycle_context_state import RecordResult, RecordStatus
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.status_repair import VerifiedStatusTransition
from orchestune.labels import StatusLabel


@dataclass(frozen=True, slots=True)
class CompletionReceipt:
    """Evidence that one Issue's GC completion decision is confirmed."""

    issue_number: int


def apply_verified_transition(
    ctx: CycleContext,
    receipt: VerifiedStatusTransition,
    *,
    execution_active: bool | None,
) -> RecordResult | None:
    """Reflect a live-verified status transition into `ctx`, or hold.

    `execution_active=None` (the caller's execution observation is unknown
    this cycle) holds: `ctx.record_transition` is never called, so an
    uncertain execution claim can never retire or assert a launch fact. Every
    other outcome (`APPLIED`/`NOOP`/`CONFLICT`) comes straight from
    `record_transition`'s existing, already-tested semantics — this function
    does not reinterpret them.
    """
    if execution_active is None:
        return None
    return ctx.record_transition(
        receipt.issue_number,
        expected_labels=receipt.before_labels,
        verified_labels=receipt.verified_labels,
        execution_active=execution_active,
    )


# #883: targets a verified status transition can claim `execution_active=True`
# for. `record_transition` structurally rejects `execution_active=True` for
# any other target, but only these three ever *need* to assert it -- every
# other target is deterministically `False` (never claims live execution).
_EXECUTION_ACTIVE_TARGETS = frozenset(
    {
        StatusLabel.IN_PROGRESS,
        StatusLabel.BLOCKED_HUMAN_REVIEW,
        StatusLabel.MANUAL_MERGE_REQUIRED,
    }
)


def _authoritative_execution_active(
    ctx: CycleContext,
    receipt: VerifiedStatusTransition,
    *,
    has_active_entry: Callable[[int], bool] | None = None,
) -> bool | None:
    """#883: derive `execution_active` from `ctx`'s own authoritative launch view.

    Codex #899 review: a target outside `_EXECUTION_ACTIVE_TARGETS` does not
    itself claim live execution, but that alone does not make `False` safe --
    `False` is a positive claim that execution has *stopped*, which
    `record_transition` uses to retire any existing launch fact. An unrelated
    repair (e.g. resolving a `PRIMARY_STATUS_CONFLICT` down to `status:queued`
    on an Issue whose worktree is genuinely still running) must not silently
    reclaim that launch out from under it -- that would let
    `ctx.queued_tasks()` offer the Issue to scheduling again while the first
    run is still active. So: any active-worktree bookkeeping entry (even a
    handleless/ambiguous one `ctx.launch_fact` reports as `None`, per
    `cycle_context_state.py`'s `_LaunchState(fact=None, active=True,
    indeterminate=True)`) makes any target *outside* `_EXECUTION_ACTIVE_TARGETS`
    unknown (hold) rather than `False`; only the complete absence of one makes
    `False` deterministically safe. Symmetrically, a target *inside*
    `_EXECUTION_ACTIVE_TARGETS` can assert `True` only when a *clean, single*
    launch fact exists (`ctx.launch_fact` itself, not just entry presence);
    without one it is unknown (hold) rather than a guess in either direction.
    """
    primaries = primary_status_labels(receipt.verified_labels)
    target = primaries[0] if len(primaries) == 1 else None
    if target in _EXECUTION_ACTIVE_TARGETS:
        return True if ctx.launch_fact(receipt.issue_number) is not None else None
    active_entry_exists = (
        has_active_entry(receipt.issue_number)
        if has_active_entry is not None
        else ctx.launch_fact(receipt.issue_number) is not None
    )
    return None if active_entry_exists else False


def _on_status_transition_verified(
    ctx: CycleContext,
    *,
    has_active_entry: Callable[[int], bool] | None = None,
) -> Callable[[VerifiedStatusTransition], None]:
    """#883: bridge `execute_status_repair_command`'s `on_verified` into `ctx`.

    A `CONFLICT` is raised rather than swallowed: `_execute_with_pending_intent`
    already calls this callback inside its own `try/except`, converting the
    raise into `RepairStatus.FAILED` with the conflict reason as a diagnostic
    -- without rolling back the Forge label change already applied. This
    reuses that existing, already-tested behavior instead of reimplementing
    it here.
    """

    def _callback(receipt: VerifiedStatusTransition) -> None:
        result = apply_verified_transition(
            ctx,
            receipt,
            execution_active=_authoritative_execution_active(
                ctx, receipt, has_active_entry=has_active_entry
            ),
        )
        if result is not None and result.status is RecordStatus.CONFLICT:
            raise RuntimeError(
                "verified status transition conflict for issue "
                f"#{receipt.issue_number}: {result.reason}"
            )

    return _callback


__all__ = ["CompletionReceipt", "apply_verified_transition"]
