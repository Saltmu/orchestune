"""Project a workflow next result into operator guidance. No new decision."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orchestune.operator_guidance import (
    EvidenceState,
    GuidanceAction,
    NextOwner,
    OperatorGuidance,
    ProgressOutlook,
)
from orchestune.workflow.contracts import WorkflowResult

_UNAVAILABLE = frozenset(
    {
        "evidence_unavailable",
        "context_missing",
        "context_invalid",
        "context_conflict",
        "stage_unavailable",
    }
)
_STALE = frozenset({"ci_stale", "pr_stale"})
_EXPLANATIONS = {
    "ready": "次の操作は自動では実行されません",
    "unmet": "安全な判断を妨げる証拠の不足または不正です",
    "waiting_human": "人の判断が必要です",
    "terminal_blocked": "確定した blocked Outcome です。自動では再開しません",
}


def guidance_from_workflow(result: WorkflowResult) -> OperatorGuidance:
    """Copy status, reasons, and actions. Do not invent a command."""
    evidence, note = _evidence(result)
    owner, progress, condition = _outlook(result)
    return OperatorGuidance(
        target=_target(result),
        stage=result.stage_id,
        reason_codes=result.reason_codes,
        explanation=_EXPLANATIONS.get(result.status, "不明"),
        next_owner=owner,
        progress=progress,
        progress_condition=condition,
        actions=tuple(_project(action) for action in result.actions),
        references=result.references,
        evidence_state=evidence,
        evidence_note=note,
    )


def _target(result: WorkflowResult) -> str:
    target = f"issue #{result.issue_number}"
    if result.claim_id:
        return f"{target} / claim {result.claim_id}"
    return target


def _outlook(
    result: WorkflowResult,
) -> tuple[NextOwner, ProgressOutlook, str | None]:
    if result.stage_id == "handed-off":
        return NextOwner.NONE, ProgressOutlook.TERMINAL, None
    if result.status == "terminal_blocked":
        return NextOwner.USER, ProgressOutlook.TERMINAL, None
    if result.status == "waiting_human":
        return NextOwner.USER, ProgressOutlook.MANUAL, None
    if result.status == "unmet":
        return NextOwner.UNKNOWN, ProgressOutlook.UNKNOWN, None
    if result.status == "ready":
        return NextOwner.AGENT, ProgressOutlook.MANUAL, None
    return NextOwner.UNKNOWN, ProgressOutlook.UNKNOWN, None


def _evidence(result: WorkflowResult) -> tuple[EvidenceState, str]:
    codes = set(result.reason_codes)
    unknown = any(check.status == "unknown" for check in result.checks)
    if codes & _UNAVAILABLE or unknown:
        return (
            EvidenceState.UNAVAILABLE,
            "不足または不明な証拠からは完了や再開を断定しません",
        )
    if codes & _STALE:
        return EvidenceState.STALE, "古い証拠です。完了や再開を断定しません"
    return EvidenceState.CURRENT, "この表示回の workflow 結果です"


def _project(action: Mapping[str, Any]) -> GuidanceAction:
    raw = action.get("argv")
    argv = tuple(str(part) for part in raw) if isinstance(raw, list) and raw else None
    references = tuple(str(item) for item in action.get("references") or ())
    preconditions = tuple(str(item) for item in action.get("preconditions") or ())
    description = str(action.get("description") or "不明")
    return GuidanceAction(
        description=description,
        argv=argv,
        preconditions=preconditions,
        references=references,
    )
