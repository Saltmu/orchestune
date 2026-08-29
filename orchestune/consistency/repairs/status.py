"""Deterministic, side-effect-free repair planning for status findings.

Only label operations are planned here.  A status finding whose real remedy is
to reclaim an execution or clear run state is reported by the invariants and
left to the execution policy, so no two planners can act on one Issue for the
same reason.

Every command carries preconditions the executor must re-check immediately
before mutating, because a plan is computed from one snapshot and applied
against live state:

``finding-certainty:known``
    The facts behind the finding were certain when it was raised.
``issue-open``
    The Issue is still open.
``absent-primary-status``
    No primary status label is present, so adding one cannot create a conflict.
``holds-primary-status:<label>``
    The label being replaced or removed is still present.
``retains-primary-status:<label>``
    The label that must survive the removal is present, so an interrupted
    removal can never leave the Issue without a primary status (#381).
``dependencies-declared``
    The task declares at least one dependency, matching the guard in
    ``dispatch.reconciliation._decide_blocked_promotions``.
``no-promotion-hold``
    Neither ``ci:base-branch-red`` nor ``status:blocked-recompute`` is present.
"""

from __future__ import annotations

from collections.abc import Iterable

from orchestune.consistency.invariants.status import (
    BLOCKED_WITH_RESOLVED_DEPENDENCIES,
    FORGE_OBSERVATION_UNKNOWN,
    PRIMARY_STATUS_CONFLICT,
    PRIMARY_STATUS_MISSING,
    QUEUED_WITH_UNRESOLVED_DEPENDENCIES,
    STATUS_OBSERVATION_UNKNOWN,
    TERMINAL_ESCALATION_LABELS,
    primary_status_labels,
)
from orchestune.consistency.models import (
    ConsistencyFinding,
    ConsistencyReport,
    ConsistencyScope,
    FactValue,
    Repairability,
    RepairCommand,
)

COMMAND_ADD_LABEL = "status.add-label"
COMMAND_REMOVE_LABEL = "status.remove-label"
COMMAND_TRANSITION_LABEL = "status.transition-label"

_COMMAND_ORDER = {
    COMMAND_ADD_LABEL: 0,
    COMMAND_TRANSITION_LABEL: 1,
    COMMAND_REMOVE_LABEL: 2,
}
_TRANSITION_TARGETS = {
    BLOCKED_WITH_RESOLVED_DEPENDENCIES: ("status:blocked", "status:queued"),
    QUEUED_WITH_UNRESOLVED_DEPENDENCIES: ("status:queued", "status:blocked"),
}
_PLANNED_CODES = frozenset(
    {PRIMARY_STATUS_CONFLICT, PRIMARY_STATUS_MISSING, *_TRANSITION_TARGETS}
)
_CERTAIN = "finding-certainty:known"
_ISSUE_OPEN = "issue-open"


def _label(value: FactValue) -> str | None:
    return value if isinstance(value, str) and value else None


def _observed_primary(finding: ConsistencyFinding) -> tuple[str, ...]:
    value = finding.observed.value
    if not isinstance(value, tuple):
        return ()
    return primary_status_labels(tuple(item for item in value if isinstance(item, str)))


def _command(
    finding: ConsistencyFinding,
    code: str,
    action: str,
    label: str,
    parameters: tuple[tuple[str, FactValue], ...],
    preconditions: tuple[str, ...],
) -> RepairCommand:
    return RepairCommand(
        code=code,
        scope=ConsistencyScope.TASK,
        subject_id=finding.subject_id,
        idempotency_key=f"status:{finding.subject_id}:{action}:{label}",
        parameters=(*parameters, ("finding_code", finding.code)),
        preconditions=(_CERTAIN, _ISSUE_OPEN, *preconditions),
    )


def _add_commands(finding: ConsistencyFinding) -> tuple[RepairCommand, ...]:
    label = _label(finding.expected.value)
    if label is None:
        return ()
    return (
        _command(
            finding,
            COMMAND_ADD_LABEL,
            "add",
            label,
            (("label", label),),
            ("absent-primary-status",),
        ),
    )


def _remove_commands(finding: ConsistencyFinding) -> tuple[RepairCommand, ...]:
    """Remove the labels a conflict leaves over, never the one that must stay.

    The human gates are re-checked here rather than trusted from the finding:
    stripping `status:blocked-human-review` would restart a task a person
    deliberately stopped, so no report may talk this planner into it.
    """
    keep = _label(finding.expected.value)
    observed = _observed_primary(finding)
    if keep is None or keep not in observed:
        return ()
    removable = tuple(label for label in observed if label != keep)
    if any(label in TERMINAL_ESCALATION_LABELS for label in removable):
        return ()
    return tuple(
        _command(
            finding,
            COMMAND_REMOVE_LABEL,
            "remove",
            label,
            (("label", label),),
            (f"retains-primary-status:{keep}", f"holds-primary-status:{label}"),
        )
        for label in removable
    )


def _transition_commands(finding: ConsistencyFinding) -> tuple[RepairCommand, ...]:
    old_label, new_label = _TRANSITION_TARGETS[finding.code]
    if _observed_primary(finding) != (old_label,):
        return ()
    preconditions: tuple[str, ...] = (f"holds-primary-status:{old_label}",)
    if finding.code == BLOCKED_WITH_RESOLVED_DEPENDENCIES:
        preconditions = (*preconditions, "dependencies-declared", "no-promotion-hold")
    return (
        _command(
            finding,
            COMMAND_TRANSITION_LABEL,
            "transition",
            new_label,
            (("new_label", new_label), ("old_labels", (old_label,))),
            preconditions,
        ),
    )


def _commands_for(finding: ConsistencyFinding) -> tuple[RepairCommand, ...]:
    if finding.code == PRIMARY_STATUS_MISSING:
        return _add_commands(finding)
    if finding.code == PRIMARY_STATUS_CONFLICT:
        return _remove_commands(finding)
    return _transition_commands(finding)


def _eligible(
    findings: Iterable[ConsistencyFinding],
) -> tuple[ConsistencyFinding, ...]:
    """Automatic findings whose subject carries no uncertainty of its own."""
    normalized = tuple(findings)
    if any(finding.code == FORGE_OBSERVATION_UNKNOWN for finding in normalized):
        return ()
    uncertain_subjects = {
        finding.subject_id
        for finding in normalized
        if finding.code == STATUS_OBSERVATION_UNKNOWN
    }
    return tuple(
        sorted(
            (
                finding
                for finding in normalized
                if finding.repairability is Repairability.AUTOMATIC
                and finding.scope is ConsistencyScope.TASK
                and finding.subject_id is not None
                and finding.subject_id not in uncertain_subjects
                and finding.code in _PLANNED_CODES
            ),
            key=lambda finding: (finding.subject_id or "", finding.code),
        )
    )


def plan_status_repairs(report: ConsistencyReport) -> tuple[RepairCommand, ...]:
    """Convert certain, automatic status findings into typed label commands.

    Planning applies nothing.  Equivalent reports produce equal commands, an
    unreachable Forge empties the whole plan, and a task whose own observation
    is uncertain is skipped while its peers are still planned for.  Findings
    that are manual, informational, covered by a live transition intent, or
    owned by another policy are deliberately ignored.
    """
    commands = tuple(
        command
        for finding in _eligible(report.findings)
        for command in _commands_for(finding)
    )
    return tuple(
        sorted(
            commands,
            key=lambda command: (
                command.subject_id or "",
                _COMMAND_ORDER[command.code],
                command.idempotency_key,
            ),
        )
    )


__all__ = [
    "COMMAND_ADD_LABEL",
    "COMMAND_REMOVE_LABEL",
    "COMMAND_TRANSITION_LABEL",
    "plan_status_repairs",
]
