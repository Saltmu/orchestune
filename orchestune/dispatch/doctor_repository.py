"""Offline repository-wide diagnostics for ``orchestune doctor``.

Looks at workflows other than the one under diagnosis (and at gc/recover
entrypoints in every workflow); never runs workflow shell, touches the network
or writes files.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestune.dispatch.doctor_actions import (
    MIGRATION_DOC_REF,
    STANDARD_GROUP,
    is_standard_group,
)
from orchestune.dispatch.doctor_entrypoints import (
    DispatchEntrypoint,
    StepLocation,
    WorkflowScan,
    scan_workflow,
)
from orchestune.dispatch.doctor_models import (
    CODE_OTHER_CONTROL_ENTRYPOINTS,
    CODE_OTHER_ENTRYPOINTS,
    Diagnostic,
    DiagnosticStatus,
    DoctorContext,
    worst_status,
)


@dataclass(frozen=True)
class _Scanned:
    path: str
    standard: bool
    scan: WorkflowScan


def _where(path: str, location: StepLocation, command: str) -> str:
    return (
        f"{path} / {location.job} / step[{location.step_index}] "
        f"line {location.line_no}: {command}"
    )


def _entry_where(path: str, entry: DispatchEntrypoint) -> str:
    return _where(path, entry.location, entry.command)


def _classify_other(
    context: DoctorContext, wf: _Scanned, entry: DispatchEntrypoint
) -> tuple[DiagnosticStatus, str]:
    where = _entry_where(wf.path, entry)
    if context.mode == "local":
        return "warning", f"dispatch entrypoint: {where}"
    if wf.standard:
        return "warning", f"standard group: {where}"
    return "error", f"group missing or non-standard: {where}"


def _other_entrypoints(
    context: DoctorContext, others: list[_Scanned], unreadable: list[str]
) -> Diagnostic:
    statuses: list[DiagnosticStatus] = []
    evidence: list[str] = list(unreadable)
    for wf in others:
        for entry in wf.scan.dispatch:
            if entry.apply:
                status, line = _classify_other(context, wf, entry)
                statuses.append(status)
                evidence.append(line)
    status = worst_status(statuses)
    if status == "ok":
        return Diagnostic(
            CODE_OTHER_ENTRYPOINTS,
            "ok",
            "No other workflow dispatches in apply mode.",
            tuple(evidence),
        )
    if context.mode == "local":
        return Diagnostic(
            CODE_OTHER_ENTRYPOINTS,
            "warning",
            "Workflows also dispatch in apply mode; Actions and local runs may overlap.",
            tuple(evidence),
            "Pick a single owner for dispatch: remove the workflow dispatch "
            "or stop local dispatching.",
        )
    if status == "warning":
        return Diagnostic(
            CODE_OTHER_ENTRYPOINTS,
            "warning",
            "Other workflows dispatch in apply mode with the standard group.",
            tuple(evidence),
            "They serialize with the control workflow but are not diagnosed; "
            "pass them with --workflow as well.",
        )
    return Diagnostic(
        CODE_OTHER_ENTRYPOINTS,
        "error",
        "Other workflows dispatch in apply mode outside the standard group.",
        tuple(evidence),
        "Whether a workflow is disabled cannot be determined offline. Delete the "
        f"old workflow file, or give it the standard group `{STANDARD_GROUP}`. "
        f"See {MIGRATION_DOC_REF}.",
    )


def _other_control_entrypoints(
    scanned: list[_Scanned], unreadable: list[str]
) -> Diagnostic:
    statuses: list[DiagnosticStatus] = []
    evidence: list[str] = list(unreadable)
    for wf in scanned:
        for entry in wf.scan.control:
            statuses.append("ok" if wf.standard else "warning")
            note = "standard group" if wf.standard else "outside the standard group"
            evidence.append(
                f"{entry.kind} ({note}): {_where(wf.path, entry.location, entry.command)}"
            )
    status = worst_status(statuses)
    blind = [
        f"undetectable {u.kind}: {_where(wf.path, u.location, u.detail)}"
        for wf in scanned
        for u in wf.scan.undetectable
    ]
    if status == "ok" and blind:
        return Diagnostic(
            CODE_OTHER_CONTROL_ENTRYPOINTS,
            "not_checked",
            "Some workflow calls could not be inspected for gc/recover.",
            tuple(evidence + blind),
            "Call `orchestune gc` / `orchestune recover --apply` directly in a run step.",
        )
    if status == "ok":
        message = (
            "gc/recover entrypoints are in the standard group."
            if statuses
            else "No applied gc/recover entrypoint found."
        )
        return Diagnostic(
            CODE_OTHER_CONTROL_ENTRYPOINTS, "ok", message, tuple(evidence)
        )
    return Diagnostic(
        CODE_OTHER_CONTROL_ENTRYPOINTS,
        "warning",
        "gc/recover run outside the standard concurrency group.",
        tuple(evidence + blind),
        "Put periodic gc in the standard group. Run one-off recovery only after "
        "confirming the control runner is stopped.",
    )


def run_repository_checks(context: DoctorContext) -> tuple[Diagnostic, ...]:
    """Return ``other_entrypoints`` then ``other_control_entrypoints``."""
    specified = {f.path for f in context.specified}
    scanned: list[_Scanned] = []
    unreadable: list[str] = []
    others: list[_Scanned] = []
    for f in context.discovered:
        if f.document is None:
            unreadable.append(f"{f.path}: unreadable ({f.error})")
            continue
        item = _Scanned(
            f.path, is_standard_group(f.document), scan_workflow(f.path, f.document)
        )
        scanned.append(item)
        if f.path not in specified:
            others.append(item)
    return (
        _other_entrypoints(context, others, unreadable),
        _other_control_entrypoints(scanned, unreadable),
    )
