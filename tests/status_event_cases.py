"""Executed Event cases and test drivers table (#1264).

Extracted from status_event_test_support.py to comply with line bloat limits.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from orchestune.consistency.desired import TaskLifecycle
from orchestune.labels import StatusLabel
from orchestune.ledger.status_events import (
    BASE_BRANCH_RED_LABEL,
    BackoffState,
    BudgetCounts,
    ExecutionIdentity,
    ReclaimState,
    RetryStates,
    Stage,
    TaskModel,
)
from tests.status_event_test_support import (
    CaseDriver,
    base_branch_red_escalate,
    base_branch_red_policy_effect,
    base_branch_red_unmark,
    blocked_recompute_with_pending_dependencies,
    completion_adapter,
    cycle_record_completion,
    external_lock_sync,
    finalize_not_needed,
    forced_serial,
    gc_backoff_retry,
    gc_escalated_base_branch_red,
    gc_reclaim,
    launch_confirms_reservations,
    merge_failure,
    not_needed_review_verdict,
    prior_merge_dry_run,
    prior_parent_normalize,
    replan_retire,
    review_timeout_policy_effect,
    rule_not_needed_outcome,
    status_repair_add_remove,
)

#: A failed attempt that left no effect: the model applies nothing for it.
FAIL_BEFORE = "fail-before"
#: The execution identity of the run that is still active, and an older one.
CURRENT = ExecutionIdentity("launch-current")
STALE = ExecutionIdentity("launch-stale")
NOW = 1_700_000_000.0


@dataclass(frozen=True)
class EventCase:
    source: str
    condition: str
    held: tuple[str, ...]
    driver: CaseDriver
    params: Mapping[str, Any] = field(default_factory=dict)
    model: Mapping[str, Any] = field(default_factory=dict)
    inputs: Mapping[str, Any] = field(default_factory=dict)
    steps: tuple[Stage | str | None, ...] = (None,)

    @property
    def id(self) -> str:
        return f"{self.source.split('::')[1].strip('_')}-{self.condition}"

    def initial(self) -> TaskModel:
        return replace(TaskModel.from_labels(self.held), **self.model)


def _cases(source: str, driver: CaseDriver) -> Callable[..., EventCase]:
    def make(condition: str, held: tuple[str, ...], **fields: Any) -> EventCase:
        return EventCase(source, condition, held, driver, **fields)

    return make


Q, B, P = StatusLabel.QUEUED, StatusLabel.BLOCKED, StatusLabel.IN_PROGRESS
D, N, H = StatusLabel.DONE, StatusLabel.NOT_NEEDED, StatusLabel.BLOCKED_HUMAN_REVIEW
RC, FS, EL = (
    StatusLabel.BLOCKED_RECOMPUTE,
    StatusLabel.FORCE_SERIAL,
    StatusLabel.EXTERNAL_LOCK,
)
RED = BASE_BRANCH_RED_LABEL
_ACTIVE_RUN = {"execution_identity": CURRENT}
_THIRD_RED = {"counts": BudgetCounts(base_branch_red=2)}


def _direct_operation_cases() -> list[EventCase]:
    adapter = _cases(
        "complete/status_labels.py::_completion_mutate", completion_adapter
    )
    policy = "dispatch/gc/policy_effects.py::reconcile_labels"
    timeout = _cases(policy, review_timeout_policy_effect)
    red = _cases(policy, base_branch_red_policy_effect)
    verdict = _cases(policy, not_needed_review_verdict)
    replan = _cases("replan/operations.py::_transition_to_not_needed", replan_retire)
    revert = _cases("integrator/pr.py::handle_merge_failure", merge_failure)
    retry_op = {"operation": "op"}
    return [
        adapter("done-from-in-progress", (P,), params={"target": D}),
        adapter("done-keeps-force-serial", (P, FS), params={"target": D}),
        adapter("done-repair", (Q, B), params={"target": D}),
        adapter("done-replay", (D,), params={"target": D}),
        adapter("done-escalated-conflict", (H,), params={"target": D}),
        adapter(
            "done-stale-generation",
            (P,),
            params={"target": D, "generation": False},
            model=_ACTIVE_RUN,
            inputs={"execution": STALE},
        ),
        adapter(
            "done-remove-fails-then-retried",
            (P,),
            params={"target": D, "faults": (("remove", "before"), None)},
            inputs=retry_op,
            steps=(Stage.LABEL_ADDED, None),
        ),
        adapter("not-needed-from-in-progress", (P,), params={"target": N}),
        adapter("blocked-from-in-progress", (P,), params={"target": B}),
        timeout("review-timeout-requeue", (P,), inputs={"now": NOW}),
        timeout(
            "review-timeout-exhausted",
            (P,),
            params={"count": 1},
            model={"retries": RetryStates(review_timeout=BackoffState(count=1))},
            inputs={"now": NOW},
        ),
        red("base-red-hold", (P,), params={"attempt": 1}),
        red("base-red-escalate", (B, RED), params={"attempt": 3}, model=_THIRD_RED),
        verdict("review-rejected", (N,), params={"verdict": "failed"}),
        verdict("review-launch-timeout", (N,)),
        replan("queued", (Q,)),
        replan("blocked-with-recompute", (B, RC)),
        replan("in-progress-force-serial", (P, FS)),
        replan("escalated", (H,)),
        replan("replay", (N,)),
        revert("done", (D,)),
        revert("partial-rollback", (D, Q)),
        revert(
            "add-fails-then-retried",
            (D,),
            params={"faults": (("add", "before"), None)},
            inputs=retry_op,
            steps=(FAIL_BEFORE, None),
        ),
        revert(
            "remove-fails-then-retried",
            (D,),
            params={"faults": (("remove", "before"), None)},
            inputs=retry_op,
            steps=(Stage.LABEL_ADDED, None),
        ),
    ]


def _dispatch_direct_cases() -> list[EventCase]:
    repair = _cases(
        "dispatch/status_repair.py::_apply_command", status_repair_add_remove
    )
    lock = _cases(
        "dispatch/phase_rebase.py::_apply_external_lock_sync", external_lock_sync
    )
    reconcile = "dispatch/reconciliation.py::"
    finalize = _cases(
        "dispatch/gc/completion.py::_finalize_not_needed_worktree", finalize_not_needed
    )
    open_task: dict[str, Any] = {}
    done = {"lifecycle": TaskLifecycle.DONE}
    return [
        repair("add-missing-queued", (), params={"desired": open_task}),
        repair("add-missing-blocked", (), params={"desired": {"depends_on": ("x",)}}),
        repair("remove-conflicting-blocked", (Q, B), params={"desired": open_task}),
        repair("interrupted-rollback", (D, Q), params={"desired": open_task}),
        repair("remove-stale-queued-from-done", (D, Q), params={"desired": done}),
        repair("remove-stale-in-progress", (P, Q), params={"desired": open_task}),
        EventCase(
            "dispatch/rebase.py::_apply_forced_serial_event",
            "over-budget",
            (P,),
            forced_serial,
            params={"count": 2},
            model={"counts": BudgetCounts(recompute=2)},
        ),
        lock("lock", (Q,), params={"lock": True}),
        lock("unlock-queued", (Q, EL), params={"lock": False}),
        lock("unlock-restores-queued", (EL,), params={"lock": False, "snapshot": (Q,)}),
        lock("unlock-blocked", (B, EL), params={"lock": False}),
        EventCase(
            reconcile + "_resolve_one_blocked_recompute_issue",
            "pending-dependencies",
            (B, RC),
            blocked_recompute_with_pending_dependencies,
        ),
        EventCase(
            reconcile + "_apply_base_branch_red_unmark",
            "unmark",
            (B, RED),
            base_branch_red_unmark,
        ),
        EventCase(
            reconcile + "_apply_base_branch_red_escalate",
            "third-attempt",
            (B, RED),
            base_branch_red_escalate,
            model=_THIRD_RED,
        ),
        EventCase(
            "dispatch/gc/completion.py::_apply_escalated_base_branch_red",
            "third-attempt",
            (P, RED),
            gc_escalated_base_branch_red,
            model=dict(_THIRD_RED, execution_identity=CURRENT),
        ),
        EventCase(
            "dispatch/prior_parent_merge.py::_normalize_closed_issue_label",
            "terminal-done-cleanup",
            (D, Q),
            prior_parent_normalize,
        ),
        EventCase(
            "dispatch/prior_parent_merge.py::_normalize_closed_issue_label",
            "terminal-not-needed-cleanup",
            (N, B),
            prior_parent_normalize,
        ),
        finalize("labelled", (P, N)),
        finalize("outcome-only", (P,), model=_ACTIVE_RUN),
    ]


def _completion_and_retry_cases() -> list[EventCase]:
    record = _cases(
        "dispatch/cycle_context_state.py::_CycleState.record_completion",
        cycle_record_completion,
    )
    reclaim = _cases("dispatch/gc/zombies.py::_notify_requeued_reclaim", gc_reclaim)
    escalation = "ledger/escalation.py::apply_human_review_escalation"
    requeue = _cases("dispatch/gc/completion.py::_publish_requeue", gc_backoff_retry)
    pending = RetryStates(reclaim=ReclaimState(count=2, pending=True))
    over = RetryStates(reclaim=ReclaimState(count=3))
    spent = RetryStates(early_death=BackoffState(count=2, retry_at=5.0))
    reserved = RetryStates(
        reclaim=ReclaimState(count=1, pending=True),
        early_death=BackoffState(count=1, retry_at=5.0, pending=True),
        review_timeout=BackoffState(count=1, retry_at=5.0, pending=True),
    )
    at_now = {"now": NOW}
    return [
        record("in-progress", (P,), model=_ACTIVE_RUN),
        record(
            "duplicate",
            (P,),
            params={"calls": 2},
            model=_ACTIVE_RUN,
            steps=(None, None),
        ),
        record("already-done", (D,), params={"active": False}),
        EventCase(
            "dispatch/prior_parent_merge.py::reconcile_prior_parent_merges",
            "dry-run",
            (Q,),
            prior_merge_dry_run,
        ),
        EventCase(
            "dispatch/gc/__init__.py::_rule_not_needed",
            "outcome-only",
            (P,),
            rule_not_needed_outcome,
            model=_ACTIVE_RUN,
        ),
        reclaim("first-reclaim", (P,), model=_ACTIVE_RUN),
        reclaim(
            "pending-reclaim-resumed",
            (P,),
            params={"retries": pending},
            model={**_ACTIVE_RUN, "retries": pending},
        ),
        EventCase(
            escalation,
            "reclaim-over-budget",
            (P,),
            gc_reclaim,
            params={"retries": over},
            model={**_ACTIVE_RUN, "retries": over},
        ),
        requeue(
            "early-death-retry",
            (P,),
            params={"kind": "early_death"},
            model=_ACTIVE_RUN,
            inputs=at_now,
        ),
        requeue(
            "review-timeout-retry",
            (P,),
            params={"kind": "review_timeout"},
            model=_ACTIVE_RUN,
            inputs=at_now,
        ),
        EventCase(
            escalation,
            "early-death-exhausted",
            (P,),
            gc_backoff_retry,
            params={"kind": "early_death", "retries": spent},
            model={**_ACTIVE_RUN, "retries": spent},
            inputs=at_now,
        ),
        EventCase(
            "dispatch/launch.py::_record_successful_launch",
            "confirms-reservations",
            (Q,),
            launch_confirms_reservations,
            params={"retries": reserved},
            model={"retries": reserved},
            inputs={"execution": CURRENT},
        ),
    ]


EVENT_CASES: tuple[EventCase, ...] = (
    *_direct_operation_cases(),
    *_dispatch_direct_cases(),
    *_completion_and_retry_cases(),
)
