"""Deterministic, side-effect-free repair planning for execution findings."""

from __future__ import annotations

from collections.abc import Iterable

from orchestune.consistency.invariants.execution import (
    BRANCH_MISSING,
    EXECUTION_OBSERVATION_UNKNOWN,
    FORGE_OBSERVATION_UNKNOWN,
    LOCAL_PROCESS_DEAD,
    ORPHAN_EXECUTION,
    RUN_STATE_MISSING,
    RUN_STATE_STALE,
    WORKTREE_MISSING,
)
from orchestune.consistency.models import (
    ConsistencyFinding,
    ConsistencyReport,
    ConsistencyScope,
    Repairability,
    RepairCommand,
)

COMMAND_RECLAIM = "execution.reclaim"
COMMAND_REQUEUE = "execution.requeue"
COMMAND_BOOKKEEPING = "execution.update-bookkeeping"

_RECLAIM_AND_REQUEUE = (COMMAND_RECLAIM, COMMAND_REQUEUE)
_ACTIONS_BY_FINDING = {
    BRANCH_MISSING: _RECLAIM_AND_REQUEUE,
    LOCAL_PROCESS_DEAD: _RECLAIM_AND_REQUEUE,
    ORPHAN_EXECUTION: (COMMAND_RECLAIM, COMMAND_BOOKKEEPING),
    RUN_STATE_MISSING: (COMMAND_REQUEUE, COMMAND_BOOKKEEPING),
    RUN_STATE_STALE: (COMMAND_BOOKKEEPING,),
    WORKTREE_MISSING: _RECLAIM_AND_REQUEUE,
}
_COMMAND_ORDER = {
    COMMAND_RECLAIM: 0,
    COMMAND_REQUEUE: 1,
    COMMAND_BOOKKEEPING: 2,
}
_PRECONDITIONS = {
    COMMAND_RECLAIM: ("finding-certainty:known", "execution-still-recorded"),
    COMMAND_REQUEUE: ("reclaim-complete", "task-not-active"),
    COMMAND_BOOKKEEPING: ("finding-certainty:known",),
}


def _eligible(findings: Iterable[ConsistencyFinding]) -> tuple[ConsistencyFinding, ...]:
    normalized = tuple(findings)
    if any(finding.code == FORGE_OBSERVATION_UNKNOWN for finding in normalized):
        return ()
    blocked_subjects = {
        finding.subject_id
        for finding in normalized
        if finding.code == EXECUTION_OBSERVATION_UNKNOWN
        and finding.subject_id is not None
    }
    return tuple(
        sorted(
            (
                finding
                for finding in normalized
                if finding.repairability is Repairability.AUTOMATIC
                and finding.subject_id is not None
                and finding.subject_id not in blocked_subjects
                and finding.code in _ACTIONS_BY_FINDING
            ),
            key=lambda finding: (finding.subject_id or "", finding.code),
        )
    )


def _group_actions(
    findings: Iterable[ConsistencyFinding],
) -> dict[tuple[str, str], set[str]]:
    eligible = _eligible(findings)
    no_requeue = {
        finding.subject_id
        for finding in eligible
        if finding.code in {ORPHAN_EXECUTION, RUN_STATE_STALE}
    }
    grouped: dict[tuple[str, str], set[str]] = {}
    for finding in eligible:
        assert finding.subject_id is not None
        for command_code in _ACTIONS_BY_FINDING[finding.code]:
            if command_code == COMMAND_REQUEUE and finding.subject_id in no_requeue:
                continue
            grouped.setdefault((finding.subject_id, command_code), set()).add(
                finding.code
            )
    return grouped


def _command(
    subject_id: str,
    command_code: str,
    reason_codes: set[str],
    *,
    has_reclaim: bool,
) -> RepairCommand:
    action = command_code.removeprefix("execution.")
    preconditions = _PRECONDITIONS[command_code]
    if command_code == COMMAND_REQUEUE and not has_reclaim:
        preconditions = ("execution-absent", "task-not-active")
    return RepairCommand(
        code=command_code,
        scope=ConsistencyScope.TASK,
        subject_id=subject_id,
        idempotency_key=f"execution:{subject_id}:{action}",
        parameters=(("finding_codes", tuple(sorted(reason_codes))),),
        preconditions=preconditions,
    )


def plan_execution_repairs(report: ConsistencyReport) -> tuple[RepairCommand, ...]:
    """Convert known automatic findings into deduplicated typed commands.

    Repeated calls over equivalent reports return equal commands and never apply
    them. A Forge-wide uncertainty blocks the whole plan; an execution-provider
    uncertainty blocks that task. Other stale, ambiguous, manual, and
    unrecognized findings are intentionally ignored.
    """
    grouped = _group_actions(report.findings)
    ordered = sorted(
        grouped,
        key=lambda item: (item[0], _COMMAND_ORDER[item[1]]),
    )
    return tuple(
        _command(
            subject_id,
            command_code,
            grouped[(subject_id, command_code)],
            has_reclaim=(subject_id, COMMAND_RECLAIM) in grouped,
        )
        for subject_id, command_code in ordered
    )


__all__ = [
    "COMMAND_BOOKKEEPING",
    "COMMAND_RECLAIM",
    "COMMAND_REQUEUE",
    "plan_execution_repairs",
]
