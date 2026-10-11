"""Execution identity inventory for every production event source/condition."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from orchestune.dispatch.cycle_context_state import LaunchFact
from orchestune.ledger.run_state import ActiveWorktree
from orchestune.ledger.status_events import Event, ExecutionIdentity
from tests.status_event_test_support import EVENT_BY_SOURCE

_SUCCESSFUL_LAUNCH = "dispatch/launch.py::_record_successful_launch"
_RECOVERED_LAUNCH = "dispatch/launch_attempts.py::reconcile_attempt"
_MANUAL_CLAIM = "claim/service.py::_apply_status_label"

# Updating EVENT_BY_SOURCE requires reviewing the execution-protection contract
# for the new source as well.  Conditions are expanded from each registered
# route below and remain checked by P3A-ROUTE-COVERAGE.
EXPECTED_SOURCES = frozenset(
    {
        "claim/service.py::_apply_status_label",
        "dispatch/launch.py::_apply_yaml_error_blocking",
        "dispatch/launch.py::_apply_invalid_footprint_blocking",
        "dispatch/launch.py::_handle_launch_failure",
        _SUCCESSFUL_LAUNCH,
        _RECOVERED_LAUNCH,
        "dispatch/reconciliation.py::_resolve_one_blocked_recompute_issue",
        "dispatch/reconciliation.py::_release_recompute_for_promotion",
        "dispatch/reconciliation.py::_apply_base_branch_red_requeue",
        "dispatch/reconciliation.py::_apply_base_branch_red_unmark",
        "dispatch/reconciliation.py::_apply_base_branch_red_escalate",
        "dispatch/rebase.py::notify_recompute",
        "dispatch/rebase.py::_apply_forced_serial_event",
        "dispatch/rebase.py::_apply_recomputed_event",
        "dispatch/rebase.py::_prepare_wip_backup_for_rebase",
        "dispatch/rebase.py::_handle_rebase_failure",
        "dispatch/phase_rebase.py::_apply_external_lock_sync",
        "dispatch/gc/zombies.py::_notify_requeued_reclaim",
        "dispatch/prior_parent_merge.py::_apply_verified_repair",
        "dispatch/prior_parent_merge.py::_normalize_closed_issue_label",
        "dispatch/prior_parent_merge.py::reconcile_prior_parent_merges",
        "dispatch/gc/completion.py::_apply_blocked_hold",
        "dispatch/gc/completion.py::_publish_requeue",
        "dispatch/gc/usage_limit.py::_requeue",
        "dispatch/gc/completion.py::_apply_done_worktree_cleanup",
        "dispatch/gc/completion.py::_apply_escalated_base_branch_red",
        "dispatch/gc/completion.py::_finalize_not_needed_worktree",
        "dispatch/gc/cloud_completion.py::_handle_abandoned_cloud_reclaim",
        "dispatch/gc/policy_effects.py::reconcile_labels",
        "dispatch/gc/policy_review.py::reconcile_review",
        "dispatch/gc/__init__.py::_rule_not_needed",
        "dispatch/status_repair.py::_apply_command",
        "dispatch/recovery.py::execute_recovery_requeue_command",
        "dispatch/cycle_context_state.py::_CycleState.record_completion",
        "complete/status_labels.py::_completion_mutate",
        "replan/operations.py::_transition_to_not_needed",
        "integrator/pr.py::handle_merge_failure",
        "ledger/escalation.py::apply_human_review_escalation",
        "integrator/steps.py::AutoMergeChildIntegrationStep._restore_blocked_label",
    }
)


@dataclass(frozen=True)
class ProtectionRow:
    source: str
    condition: str
    event: Event
    kind: str
    input_identity: str
    guard: str
    scope: str
    old_input: str
    effect: str
    test_node: str
    excluded_reason: str


def expected_launch_fact(active: ActiveWorktree) -> LaunchFact:
    """Normalize each field independently, without calling production helpers."""
    pid = active.launch.pid
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        pid = None
    started_at = active.launch.started_at
    if isinstance(started_at, bool) or not isinstance(started_at, int | float):
        started_at = None
    else:
        try:
            if not math.isfinite(started_at):
                started_at = None
        except OverflowError:
            started_at = None
    external_id = active.launch.external_id
    if not isinstance(external_id, str) or not external_id:
        external_id = None
    launch_attempt_id = active.launch.launch_attempt_id
    if not isinstance(launch_attempt_id, str) or not launch_attempt_id:
        launch_attempt_id = None
    claim_id = active.claim.claim_id
    if not isinstance(claim_id, str) or not claim_id:
        claim_id = None
    return LaunchFact(
        issue_number=active.core.issue_number,
        branch=active.core.branch,
        worktree_path=active.core.worktree_path,
        pid=pid,
        started_at=started_at,
        external_id=external_id,
        launch_attempt_id=launch_attempt_id,
        owner_kind=active.claim.owner_kind,
        claim_id=claim_id,
        reservation_kind=active.claim.reservation_kind,
    )


def execution_identity(active: ActiveWorktree) -> ExecutionIdentity:
    """Encode every normalized LaunchFact field in a stable test-only token."""
    fact = expected_launch_fact(active)
    payload = [
        fact.issue_number,
        fact.branch,
        fact.worktree_path,
        fact.pid,
        fact.started_at,
        fact.external_id,
        fact.launch_attempt_id,
        fact.owner_kind,
        fact.claim_id,
        fact.reservation_kind,
    ]
    return ExecutionIdentity(json.dumps(payload, separators=(",", ":")))


def _row(source: str, event: Event, kind: str, condition: str) -> ProtectionRow:
    if source == _SUCCESSFUL_LAUNCH:
        return ProtectionRow(
            source,
            condition,
            event,
            kind,
            "yes: normalized LaunchFact projected to ExecutionIdentity",
            "CycleActionAdapter.record_launch -> _CycleState.record_launch",
            "full active fact within the current cycle; same fact is idempotent",
            "same active fact: NOOP; other active fact: CONFLICT / launch-mismatch",
            "NOOP preserves state; CONFLICT leaves launch fact and labels unchanged",
            "tests/test_status_event_identity_conformance.py::test_record_launch_identity_matrix; tests/test_status_events.py::TestEventModelConformance::test_production_driver_matches_apply_event",
            "none for launch identity",
        )
    if source == _RECOVERED_LAUNCH:
        return ProtectionRow(
            source,
            condition,
            event,
            kind,
            "yes: recovered LaunchFact plus attempt and claim generation",
            "reconcile_attempt preconditions and _CycleState.record_launch",
            "attempt/claim generation before recording; full active fact in current cycle",
            "wrong attempt or released claim is skipped; active fact mismatch is CONFLICT / launch-mismatch",
            "skipped or conflicted launch does not replace current active fact",
            "tests/test_status_event_identity_conformance.py::test_record_launch_identity_matrix; tests/test_status_events.py::TestEventModelConformance::test_production_driver_matches_apply_event",
            "none for accepted recovered launches; release/stale-attempt exits are listed in the source driver",
        )
    if event is Event.LAUNCH and source == _MANUAL_CLAIM:
        return ProtectionRow(
            source,
            condition,
            event,
            kind,
            "no: this is a user-driven label claim, not a LaunchFact",
            "no execution-identity guard at this source",
            "none; status mutation is governed by the claim operation",
            "old launch identity is unavailable to this call; the label effect may occur",
            "claim label mutation can take effect without CycleContext.record_launch",
            "tests/test_status_event_identity_conformance.py::test_source_protection_inventory_is_complete; tests/test_status_events.py::TestEventModelConformance::test_production_driver_matches_apply_event",
            "candidate: decide separately whether manual claim needs a generation guard",
        )
    return ProtectionRow(
        source,
        condition,
        event,
        kind,
        "no execution identity is passed as a launch fact at this event source",
        "source-local status, operation, or freshness guards; no record_launch guard",
        "not an execution launch; any operation/generation protection is source-specific",
        "N/A: no launch identity enters this event source",
        "the source-specific event effect is covered by its EVENT_BY_SOURCE case",
        "tests/test_status_event_identity_conformance.py::test_source_protection_inventory_is_complete; tests/test_status_events.py::TestEventModelConformance::test_production_driver_matches_apply_event",
        "launch identity does not apply to this Event/Kind route",
    )


def protection_rows() -> tuple[ProtectionRow, ...]:
    """Return a source/condition row for every registered production route."""
    return tuple(
        _row(source, route.event, route.kind.value, condition)
        for source, routes in EVENT_BY_SOURCE.items()
        for route in routes
        for condition in route.cases
    )


def all_route_keys() -> set[tuple[str, str, Event, str]]:
    return {
        (source, condition, route.event, route.kind.value)
        for source, routes in EVENT_BY_SOURCE.items()
        for route in routes
        for condition in route.cases
    }


def render_protection_table() -> str:
    """Render the reviewed inventory identically in both status-label docs."""
    header = (
        "| Source | Condition | Event / Kind | Identity input | Guard | Scope | "
        "Old input: expected result / effect | Test node | Excluded path reason |\n"
        "|---|---|---|---|---|---|---|---|---|"
    )
    body = "\n".join(
        f"| {row.source} | `{row.condition}` | `{row.event.value}` / `{row.kind}` | {row.input_identity} | "
        f"{row.guard} | {row.scope} | {row.old_input}; {row.effect} | `{row.test_node}` | "
        f"{row.excluded_reason} |"
        for row in protection_rows()
    )
    return f"{header}\n{body}"
