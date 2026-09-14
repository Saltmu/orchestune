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
    ctx: CycleContext, receipt: VerifiedStatusTransition
) -> bool | None:
    """#883: derive `execution_active` from `ctx`'s own authoritative launch view.

    A target outside `_EXECUTION_ACTIVE_TARGETS` never claims live execution,
    so `False` is always correct there -- no observation needed. For a target
    that does (typically `PRIMARY_STATUS_MISSING` repairing a missing
    `status:in-progress` label), `ctx.launch_fact` is the one authoritative
    per-Issue execution signal `CycleContext` exposes: a single valid launch
    handle justifies `True`; its absence could mean either "no launch" or an
    ambiguous/indeterminate one (`CycleContext` does not distinguish these
    over its public API), so this holds (`None`) rather than guessing.
    """
    primaries = primary_status_labels(receipt.verified_labels)
    target = primaries[0] if len(primaries) == 1 else None
    if target not in _EXECUTION_ACTIVE_TARGETS:
        return False
    return True if ctx.launch_fact(receipt.issue_number) is not None else None


def _on_status_transition_verified(
    ctx: CycleContext,
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
            execution_active=_authoritative_execution_active(ctx, receipt),
        )
        if result is not None and result.status is RecordStatus.CONFLICT:
            raise RuntimeError(
                "verified status transition conflict for issue "
                f"#{receipt.issue_number}: {result.reason}"
            )

    return _callback


__all__ = ["CompletionReceipt", "apply_verified_transition"]
