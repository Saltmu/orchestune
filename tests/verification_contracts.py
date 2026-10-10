"""Verification contracts of Phases 1-3 and the controls that prove detection (#1275).

One row is one guarantee.  ``tests`` verify it on the production code; ``controls``
map a *fault id* to the test node ids that inject that single fault (in the test
only, with ``monkeypatch``) and require a ``ContractViolation`` of exactly this
contract id.  ``verified`` needs both; a row without a control stays
``unverified`` with a reason.  ``tests/test_verification_contracts.py`` checks the
table against real pytest collection and against ``docs/{ja,en}/verification-contracts.md``.

Node ids name a test function (every collected parameter set must exist and none
may be skipped or xfailed) or one explicit parametrized case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Status = Literal["verified", "unverified", "known_defect", "out_of_scope"]

P1 = "tests/test_status_machine_stateful.py"
P2 = "tests/test_status_reconciliation_stateful.py"
P2P = "tests/test_consistency_status_repairs.py"
P3A = "tests/test_status_events.py"
P3B = "tests/test_status_events_stateful.py"
P3BR = "tests/test_status_event_retry_resume.py"
P3C = "tests/test_dependency_liveness_stateful.py"


@dataclass(frozen=True)
class Contract:
    id: str
    phase: str
    guarantee: str
    premise: str
    boundary: str
    expectation: str
    status: Status
    tests: tuple[str, ...] = ()
    controls: dict[str, tuple[str, ...]] = field(default_factory=dict)
    reason: str = ""
    issues: tuple[int, ...] = ()


CONTRACTS: tuple[Contract, ...] = (
    # ---- Phase 1: shared adapter -------------------------------------------------
    Contract(
        id="P1-LIFECYCLE-NONEMPTY",
        phase="1",
        guarantee="at least one lifecycle label after every Forge add/remove",
        premise="no external change; a single unrecovered operation",
        boundary="each Forge operation (not only the adapter's return)",
        expectation="always, including right after an injected failure",
        status="verified",
        tests=(
            f"{P1}::TestStatusLabelMachine::runTest",
            f"{P1}::test_one_complete_retry_converges_after_each_failure_position",
        ),
        controls={
            "remove-before-add": (
                f"{P1}::test_control_remove_before_add_is_detected_mid_operation",
            )
        },
    ),
    Contract(
        id="P1-SUCCESS-TARGET-ONLY",
        phase="1",
        guarantee="a normal finish with a complete removal list leaves the target alone",
        premise="complete old-label list; no failure",
        boundary="adapter return",
        expectation="lifecycle == {target}; auxiliary labels untouched",
        status="verified",
        tests=(
            f"{P1}::TestStatusLabelMachine::runTest",
            f"{P1}::test_complete_removal_list_leaves_only_the_target",
        ),
        controls={
            "remove-missing": (f"{P1}::test_control_remove_missing_is_detected",)
        },
    ),
    Contract(
        id="P1-RETRY-CONVERGES",
        phase="1",
        guarantee="one complete retry after a partial failure converges to the target",
        premise="add, callback and every remove of the retry succeed",
        boundary="retry return; replay leaves labels unchanged",
        expectation="exactly one complete retry",
        status="verified",
        tests=(
            f"{P1}::TestStatusLabelMachine::runTest",
            f"{P1}::test_one_complete_retry_converges_after_each_failure_position",
        ),
        controls={"retry-noop": (f"{P1}::test_control_retry_noop_is_detected",)},
    ),
    Contract(
        id="P1-ESCALATION-NOT-ACTIVE",
        phase="1",
        guarantee="normal rules never choose an ESCALATION -> ACTIVE transition",
        premise="rule choice in the test; the adapter does not refuse it",
        boundary="rule selection",
        expectation="never",
        status="unverified",
        tests=(f"{P1}::TestStatusLabelMachine::runTest",),
        reason="a property of the test's rule choice, not of production code; no fault can be injected",
    ),
    Contract(
        id="P1-EXTERNAL-CHANGE",
        phase="1",
        guarantee="after an arbitrary external relabel",
        premise="external_relabel may delete or add any lifecycle label",
        boundary="-",
        expectation="no unconditional guarantee; the model starts a new epoch",
        status="out_of_scope",
        reason="documented non-guarantee (status-labels.md); recovery is Phase 2",
    ),
    # ---- Phase 2: reconciliation -------------------------------------------------
    Contract(
        id="P2-FINAL-ESCALATION-PROTECTED",
        phase="2",
        guarantee="a stale plan never re-activates a FINAL or ESCALATION task",
        premise="the label changes between planning and the executor's fresh read",
        boundary="executor fresh guard",
        expectation="labels stay exactly the protected one",
        status="verified",
        tests=(f"{P2}::test_protected_status_survives_a_stale_plan",),
        controls={
            "fresh-guard-bypass": (
                f"{P2}::test_control_fresh_guard_bypass_is_detected",
            )
        },
    ),
    Contract(
        id="P2-PLAN-HUMAN-GATE",
        phase="2",
        guarantee="the planner never plans the removal of a human gate",
        premise="a report may claim anything",
        boundary="planner output",
        expectation="empty plan",
        status="verified",
        tests=(f"{P2P}::test_plan_refuses_to_strip_a_human_gate_even_when_told_to",),
        controls={
            "planner-gate-bypass": (
                f"{P2P}::test_control_planner_gate_bypass_is_detected",
            )
        },
    ),
    Contract(
        id="P2-PROMOTION-HOLD",
        phase="2",
        guarantee="a promotion hold that appears before execution blocks the promotion",
        premise="ci:base-branch-red / blocked-recompute added after planning",
        boundary="executor fresh guard",
        expectation="status:queued is never added while a hold is present",
        status="verified",
        tests=(f"{P2}::test_promotion_hold_survives_a_stale_plan",),
        controls={
            "hold-guard-bypass": (f"{P2}::test_control_hold_guard_bypass_is_detected",)
        },
    ),
    Contract(
        id="P2-RECOVERY-ONE-CYCLE",
        phase="2",
        guarantee="a repairable case converges in exactly one recovery cycle",
        premise="complete KNOWN facts, stable evidence, apply, all commands allowed, no holds",
        boundary="end of the first cycle (expectation 1; upper bound k=3 is separate)",
        expectation="labels == target and no finding after cycle 1",
        status="verified",
        tests=(
            f"{P2}::test_recovery_takes_exactly_one_cycle",
            f"{P2}::TestReconciliationMachine::runTest",
        ),
        controls={
            "repair-disabled": (f"{P2}::test_control_repair_disabled_is_detected",),
            "repair-delayed": (
                f"{P2}::test_control_repair_delayed_by_one_cycle_is_detected",
            ),
        },
    ),
    Contract(
        id="P2-RECOVERY-STABLE",
        phase="2",
        guarantee="two further cycles keep labels, plans, history and the Intent set",
        premise="after P2-RECOVERY-ONE-CYCLE",
        boundary="cycles 2 and 3",
        expectation="no change",
        status="unverified",
        tests=(f"{P2}::TestReconciliationMachine::runTest",),
        reason="no control: a repair that oscillates after convergence has not been injected",
    ),
    Contract(
        id="P2-LIVE-VERIFICATION",
        phase="2",
        guarantee="an applied repair is backed by the live labels",
        premise="the Forge accepted the command",
        boundary="repair result status",
        expectation="APPLIED only when the live primary status is the target",
        status="verified",
        tests=(f"{P2}::test_a_command_that_changes_nothing_is_not_reported_applied",),
        controls={"event-only": (f"{P2}::test_control_event_only_repair_is_detected",)},
    ),
    Contract(
        id="P2-CONVERGENCE-UPPER-BOUND",
        phase="2",
        guarantee="convergence within the upper bound k=3",
        premise="as P2-RECOVERY-ONE-CYCLE",
        boundary="cycle k",
        expectation="k=3",
        status="unverified",
        reason="no test drives more than one pass; only the expectation (1 cycle) is checked",
    ),
    # ---- Phase 3a: event model -----------------------------------------------------
    Contract(
        id="P3A-ROUTE-COVERAGE",
        phase="3a",
        guarantee="every production source has its routes and every route condition is executed",
        premise="static table of call sites, out-of-scope paths and invariant paths",
        boundary="route table",
        expectation="complete",
        status="verified",
        tests=(
            f"{P3A}::TestEventRegistry::test_route_table_covers_every_source_and_executes_every_condition",
        ),
        controls={
            "unrouted-source": (
                f"{P3A}::TestConformanceControls::test_control_unrouted_source_is_detected",
            ),
            "unexecuted-route-condition": (
                f"{P3A}::TestConformanceControls::test_control_unexecuted_route_condition_is_detected",
            ),
        },
    ),
    Contract(
        id="P3A-DYNAMIC-CONFORMANCE",
        phase="3a",
        guarantee="the production driver reaches the labels and state the Event model predicts",
        premise="executed case table; static coverage is a separate contract",
        boundary="each step: labels, result, completion, execution, retries, counts",
        expectation="equal to apply_event",
        status="verified",
        tests=(
            f"{P3A}::TestEventModelConformance::test_production_driver_matches_apply_event",
        ),
        controls={
            "misrouted-event": (
                f"{P3A}::TestConformanceControls::test_control_misrouted_event_is_detected",
            ),
            "wrong-model-target": (
                f"{P3A}::TestConformanceControls::test_control_wrong_model_target_is_detected",
            ),
        },
    ),
    Contract(
        id="P3A-DOCUMENT-TABLE",
        phase="3a",
        guarantee="status-labels.md lists every route with its sources and targets",
        premise="the document is the specification readers use",
        boundary="document table",
        expectation="equal to the route table",
        status="unverified",
        tests=(f"{P3A}::TestStatusLabelsDocumentMatchesEventTable",),
        reason="no control: a document drift has not been injected",
    ),
    # ---- Phase 3b: budgeted loops ----------------------------------------------------
    Contract(
        id="P3B-BUDGET-CONSUMED",
        phase="3b",
        guarantee="each new logical retry consumes one budget slot; a resume consumes none",
        premise="budgeted events with distinct operations",
        boundary="each delivery",
        expectation="count + 1 (resume or exhausted: unchanged)",
        status="verified",
        tests=(
            f"{P3B}::TestBudgetTerminationMachine::runTest",
            f"{P3B}::test_each_retry_consumes_one_slot_and_persistent_budgets_survive_ledger_loss",
        ),
        controls={
            "budget-not-consumed": (
                f"{P3B}::test_control_budget_not_consumed_is_detected",
            )
        },
    ),
    Contract(
        id="P3B-RETRY-BOUND",
        phase="3b",
        guarantee="new retries per budget never exceed the specification table within a ledger epoch",
        premise="default limits; local budgets reset only on ledger loss",
        boundary="each counted retry",
        expectation="RETRY_BOUNDS",
        status="verified",
        tests=(
            f"{P3B}::TestBudgetTerminationMachine::runTest",
            f"{P3B}::TestBudgetBounds::test_loop_stops_at_the_table_bound",
        ),
        controls={
            "budget-bound-exceeded": (
                f"{P3B}::test_control_budget_bound_exceeded_is_detected",
            )
        },
    ),
    Contract(
        id="P3B-LEDGER-LOSS-KEEPS-PERSISTENT",
        phase="3b",
        guarantee="ledger loss resets only the local budgets",
        premise="recompute and base-branch-red live on the Forge",
        boundary="restart(ledger_loss=True)",
        expectation="persistent counts unchanged",
        status="verified",
        tests=(
            f"{P3B}::TestBudgetTerminationMachine::runTest",
            f"{P3B}::TestBudgetBounds::test_ledger_loss_starts_a_new_epoch_for_local_budgets_only",
        ),
        controls={
            "persistent-budget-reset": (
                f"{P3B}::test_control_persistent_budget_reset_is_detected",
            )
        },
    ),
    Contract(
        id="P3B-LAUNCH-AND-ESCALATION",
        phase="3b",
        guarantee="no duplicate active execution, no relaunch after completion, escalation is irreversible",
        premise="Event model driven by random sequences",
        boundary="each delivery",
        expectation="invariants 3-5",
        status="unverified",
        tests=(f"{P3B}::TestBudgetTerminationMachine::runTest",),
        reason="plain assertions without contract ids; no control was injected",
    ),
    Contract(
        id="P3B-PERSISTENT-BUDGET-PRESERVED",
        phase="3b",
        guarantee="a GC reclaim keeps the backoff budgets; a relaunch keeps the recompute budget",
        premise="production persistence of TaskReclaimRecord and Issue-body counters",
        boundary="persisted records",
        expectation="budgets survive",
        status="known_defect",
        tests=(
            f"{P3BR}::TestRecomputeBudget::test_relaunch_keeps_the_persisted_recompute_budget",
            f"{P3BR}::test_reclaim_keeps_backoff_budgets",
        ),
        reason="strict xfail pins the counterexamples; the fix removes the marks",
        issues=(1279, 1280),
    ),
    # ---- Phase 3c: dependency resolution ----------------------------------------------
    Contract(
        id="P3C-CASE-DELAY",
        phase="3c",
        guarantee="per completion path, T is promoted exactly at its case-table cycle (d = 0)",
        premise="evidence usable for the promotion decision in cycle 0",
        boundary="cycle of the evidence; never earlier, never later",
        expectation="d from the case table",
        status="verified",
        tests=(f"{P3C}::test_each_completion_path_promotes_at_the_case_table_cycle",),
        controls={
            "promotion-suppressed": (
                f"{P3C}::test_control_case_table_detects_missing_or_late_promotion",
            ),
            "promotion-delayed": (
                f"{P3C}::test_control_case_table_detects_missing_or_late_promotion",
            ),
            "empty-completion-set": (f"{P3C}::test_injected_miswiring_is_detected",),
            "throwaway-context": (f"{P3C}::test_injected_miswiring_is_detected",),
        },
    ),
    Contract(
        id="P3C-LIVENESS-BOUND",
        phase="3c",
        guarantee="T is queued (apply) or previewed (dry run) after N = 1 fair cycle",
        premise="fair cycle: eligible at cycle start and at the promotion point, no error or injected failure",
        boundary="end of the first fair cycle; apply checks the real label, dry run this cycle's preview",
        expectation="N = 1",
        status="verified",
        tests=(f"{P3C}::TestDependencyLivenessMachine::runTest",),
        controls={
            "promotion-suppressed": (
                f"{P3C}::test_control_machine_detects_missing_or_late_promotion",
            ),
            "promotion-delayed": (
                f"{P3C}::test_control_machine_detects_missing_or_late_promotion",
            ),
            "event-only": (f"{P3C}::test_control_event_only_promotion_is_detected",),
        },
    ),
    Contract(
        id="P3C-INTERMEDIATE-LIVENESS",
        phase="3c",
        guarantee="an intermediate node is promoted (apply) or previewed (dry run) after N = 1 fair cycle",
        premise="its own dependencies are valid and unreserved at both boundaries",
        boundary="end of the first fair cycle for that node",
        expectation="N = 1",
        status="verified",
        tests=(f"{P3C}::TestDependencyLivenessMachine::runTest",),
        controls={
            "intermediate-ignored": (
                f"{P3C}::test_control_intermediate_ignored_is_detected",
            ),
            "promotion-suppressed": (
                f"{P3C}::test_noop_stale_callback_counts_toward_liveness",
            ),
        },
    ),
    Contract(
        id="P3C-SAFETY",
        phase="3c",
        guarantee="no promotion (or dry-run preview) without valid evidence, or under a hold or reservation",
        premise="apply is judged at the promotion point, a dry run at cycle start",
        boundary="every cycle",
        expectation="never",
        status="verified",
        tests=(
            f"{P3C}::TestDependencyLivenessMachine::runTest",
            f"{P3C}::test_base_branch_red_hold_prevents_promotion",
            f"{P3C}::test_unreleased_completion_reservation_prevents_promotion",
        ),
        controls={
            "hold-guard-bypass": (
                f"{P3C}::test_control_hold_guard_bypass_is_detected",
            ),
            "reservation-guard-bypass": (
                f"{P3C}::test_control_reservation_guard_bypass_is_detected",
            ),
            "stale-evidence": (f"{P3C}::test_control_stale_evidence_is_detected",),
        },
    ),
    Contract(
        id="P3C-INTERMEDIATE-SAFETY",
        phase="3c",
        guarantee="an intermediate node is not promoted before its own dependencies are valid and unreserved",
        premise="apply is judged at the promotion point",
        boundary="every cycle",
        expectation="never",
        status="verified",
        tests=(f"{P3C}::TestDependencyLivenessMachine::runTest",),
        controls={
            "reservation-guard-bypass": (
                f"{P3C}::test_control_intermediate_reservation_guard_bypass_is_detected",
            )
        },
    ),
    Contract(
        id="P3C-DRYRUN-RESERVATION",
        phase="3c",
        guarantee="a dry-run preview respects unreleased completion reservations",
        premise="the preview is defined over the context snapshot",
        boundary="dry-run cycle",
        expectation="no preview under a reservation of D, T or an intermediate node",
        status="known_defect",
        reason="assert_safe and the intermediate checks excuse these previews until the production fix lands",
        issues=(1281, 1283),
    ),
)
