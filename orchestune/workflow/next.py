"""Derive the next safe workflow action from existing read-only evidence."""

from __future__ import annotations

from dataclasses import asdict, replace

from orchestune.workflow.check import check_gate
from orchestune.workflow.contracts import WorkflowCheck, WorkflowContext, WorkflowResult
from orchestune.workflow.stages import StageDefinition, load_stage_definitions

_UNSAFE_REASONS = frozenset(
    {
        "approval_invalid",
        "base_mismatch",
        "claim_conflict",
        "ci_stale",
        "context_conflict",
        "context_invalid",
        "context_missing",
        "completion_unavailable",
        "evidence_missing",
        "evidence_unavailable",
        "footprint_expansion_required",
        "footprint_invalid",
        "impact_invalid",
        "pr_body_invalid",
        "pr_head_mismatch",
        "pr_stale",
        "review_evidence_invalid",
        "reply_invalid",
        "reply_duplicate",
        "review_head_mismatch",
        "reviewer_invalid",
        "reviewer_missing",
        "terminal_receipt_conflict",
        "unresolved_dependencies",
        "worktree_dirty",
    }
)
_CONDITION_REASONS = {
    "context_recorded": {
        "context_conflict",
        "context_invalid",
        "context_missing",
        "evidence_unavailable",
    },
    "reviewer_selected": {"reviewer_invalid", "reviewer_missing", "waiting_human"},
    "plan_approved": {"approval_invalid", "waiting_human"},
    "footprint_valid": {
        "footprint_expansion_required",
        "footprint_invalid",
        "unresolved_dependencies",
        "evidence_unavailable",
    },
    "claim_current": {
        "claim_missing",
        "claim_conflict",
        "footprint_expansion_required",
        "evidence_unavailable",
    },
    "impact_initial": {"impact_missing", "impact_invalid"},
    "implementation_ready": {"context_invalid", "evidence_unavailable"},
    "ci_current": {"ci_missing", "ci_stale", "evidence_unavailable"},
    "worktree_clean": {"worktree_dirty", "evidence_unavailable"},
    "pr_body_ready": {"pr_body_missing", "pr_body_invalid"},
    "base_correct": {"base_mismatch", "pr_missing", "evidence_unavailable"},
    "pr_current": {
        "pr_missing",
        "pr_head_mismatch",
        "pr_stale",
        "base_mismatch",
        "evidence_unavailable",
    },
    "review_acquired": {
        "review_unavailable",
        "review_evidence_invalid",
        "review_head_mismatch",
        "review_invalid",
        "evidence_unavailable",
    },
    "findings_judged": {
        "review_unavailable",
        "review_evidence_invalid",
        "review_invalid",
        "judgments_missing",
        "evidence_unavailable",
    },
    "review_current": {
        "review_unavailable",
        "review_evidence_invalid",
        "review_head_mismatch",
        "review_invalid",
        "evidence_unavailable",
    },
    "reply_posted": {
        "review_unavailable",
        "reply_missing",
        "reply_invalid",
        "reply_duplicate",
        "evidence_unavailable",
    },
    "completion_ready": {"completion_unavailable", "evidence_unavailable"},
    "terminal_receipt": {
        "terminal_receipt_unavailable",
        "terminal_receipt_conflict",
        "evidence_unavailable",
    },
}
_KNOWN_REASON_CODES = frozenset(_UNSAFE_REASONS.union(*_CONDITION_REASONS.values()))


def _action(action, issue_number: int, stage_id: str) -> dict:
    value = asdict(action)
    value["stage_id"] = stage_id
    value["argv"] = [
        str(issue_number) if argument == "<issue>" else argument
        for argument in action.argv
    ]
    return value


def _stage_actions(
    stage: StageDefinition, issue_number: int, reasons: tuple[str, ...]
) -> tuple[dict, ...]:
    if reasons:
        if stage.id == "review" and set(reasons) & {
            "evidence_missing",
            "review_evidence_invalid",
            "review_head_mismatch",
        }:
            return (
                {
                    "id": "inspect_review",
                    "stage_id": stage.id,
                    "description": (
                        "Inspect the current PR trigger, round, and wait process. "
                        "Reconnect to the same wait if it is running; resume only "
                        "after confirming it terminated. Do not start a concurrent wait."
                    ),
                    "argv": [],
                    "references": stage.references,
                    "reason_codes": reasons,
                },
            )
        active_reasons = set(reasons)
        if "context_invalid" in active_reasons:
            active_reasons.update({"context_missing", "context_conflict"})
        recovery = tuple(
            action
            for action in stage.on_failure
            if not action.reason_codes or set(action.reason_codes) & active_reasons
        )
        if recovery:
            return tuple(_action(action, issue_number, stage.id) for action in recovery)
        return (
            {
                "id": "reconcile",
                "stage_id": stage.id,
                "description": "Reconcile the invalid workflow evidence before proceeding.",
                "argv": [],
                "references": stage.references,
                "reason_codes": reasons,
            },
        )
    return tuple(_action(action, issue_number, stage.id) for action in stage.actions)


def _stage_checks(
    result: WorkflowResult, stage: StageDefinition
) -> tuple[WorkflowCheck, ...]:
    selected: dict[str, WorkflowCheck] = {}
    for check in result.checks:
        if check.id in stage.exit_conditions:
            selected.setdefault(check.id, check)
    checks = tuple(selected.values())
    if stage.id == "complete" and not any(
        check.id == "terminal_receipt" for check in checks
    ):
        # Pre-complete checks eligibility; this reports the receipt complete creates.
        return (
            *checks,
            WorkflowCheck(
                id="terminal_receipt",
                status="unmet",
                evidence=(),
                missing=("Run orchestune complete for the current claim generation",),
            ),
        )
    return checks


def _stage_reasons(
    result: WorkflowResult, checks: tuple[WorkflowCheck, ...]
) -> tuple[str, ...]:
    relevant = (
        set().union(*(_CONDITION_REASONS.get(check.id, set()) for check in checks))
        if checks
        else set()
    )
    reasons = tuple(code for code in result.reason_codes if code in relevant)
    if (
        any(check.status == "unknown" for check in checks)
        and "evidence_unavailable" not in reasons
    ):
        return (*reasons, "evidence_unavailable")
    return reasons


def _unsafe_reasons(result: WorkflowResult, stage: StageDefinition) -> tuple[str, ...]:
    unsafe = tuple(
        code
        for code in result.reason_codes
        if code in _UNSAFE_REASONS
        and not (code == "base_mismatch" and stage.id in {"prepare", "claim", "scope"})
        and not (code == "completion_unavailable" and stage.id != "complete")
    )
    unclassified = any(code not in _KNOWN_REASON_CODES for code in result.reason_codes)
    unknown = any(check.status == "unknown" for check in result.checks)
    if (unclassified or unknown) and "evidence_unavailable" not in unsafe:
        return (*unsafe, "evidence_unavailable")
    return unsafe


def _decision_actions(
    stage: StageDefinition, checks: tuple[WorkflowCheck, ...]
) -> tuple[dict, ...]:
    return tuple(
        {
            "stage_id": stage.id,
            "description": f"Resolve the human decision for {check.id}: "
            + "; ".join(check.missing),
            "decision": check.id,
        }
        for check in checks
        if check.status == "waiting_human"
    )


def _stage_status_actions(
    result: WorkflowResult,
    stage: StageDefinition,
    checks: tuple[WorkflowCheck, ...],
    issue_number: int,
    definitions: tuple[StageDefinition, ...],
) -> tuple[str, tuple[str, ...], tuple[dict, ...]]:
    reasons = _stage_reasons(result, checks)
    unsafe = _unsafe_reasons(result, stage)
    reasons = tuple(dict.fromkeys((*reasons, *unsafe)))
    invalid = bool(unsafe)
    decisions = _decision_actions(stage, checks)
    if invalid:
        actions = list(_stage_actions(stage, issue_number, reasons))
        active_unsafe = set(unsafe)
        if "context_invalid" in active_unsafe:
            active_unsafe.update({"context_missing", "context_conflict"})
        for candidate in definitions:
            for action in candidate.on_failure:
                if set(action.reason_codes) & active_unsafe:
                    for recovery in _stage_actions(candidate, issue_number, unsafe):
                        if recovery not in actions:
                            actions.append(recovery)
        actions.extend(action for action in decisions if action not in actions)
        return "unmet", reasons, tuple(actions)
    waiting = tuple(check for check in checks if check.status == "waiting_human")
    if waiting:
        return (
            "waiting_human",
            reasons,
            (*_stage_actions(stage, issue_number, ()), *decisions),
        )
    # Ordinary missing conditions describe work, not invalid evidence.
    return "ready", reasons, _stage_actions(stage, issue_number, ())


def next_step(
    context: WorkflowContext,
    *,
    stages: tuple[StageDefinition, ...] | None = None,
) -> WorkflowResult:
    """Return the earliest derived stage and its current safe guidance."""
    definitions = stages if stages is not None else load_stage_definitions()
    result = check_gate(context, "pre-complete", stages=definitions)
    stage = next((item for item in definitions if item.id == result.stage_id), None)
    if stage is None:
        return replace(
            result,
            gate_id=None,
            status="unmet",
            reason_codes=(*result.reason_codes, "stage_unavailable"),
            actions=(),
        )
    if result.stage_id == "handed-off":
        return replace(result, gate_id=None, actions=())
    if result.status == "terminal_blocked":
        return replace(
            result,
            gate_id=None,
            references=tuple(dict.fromkeys((*result.references, *stage.references))),
        )

    checks = _stage_checks(result, stage)
    status, reasons, actions = _stage_status_actions(
        result, stage, checks, context.issue_number, definitions
    )
    return replace(
        result,
        gate_id=None,
        status=status,
        reason_codes=reasons,
        checks=checks,
        actions=actions,
        references=tuple(dict.fromkeys((*result.references, *stage.references))),
    )
