"""``orchestune doctor``: argument parsing, rendering and exit codes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from orchestune.dispatch.doctor import (
    DoctorInputError,
    DoctorRequest,
    normalize_workflow_path,
    resolve_repository_root,
    run_doctor,
)
from orchestune.dispatch.doctor_models import (
    OWNERSHIP_NOTICE,
    Diagnostic,
    DoctorReport,
)
from orchestune.infra.cli_output import CliOutput, Level, label_token, make_output

_DIAGNOSTIC_LEVELS = {
    "ok": Level.SUCCESS,
    "warning": Level.WARNING,
    "error": Level.FAILURE,
    "not_checked": Level.SKIPPED,
}
_CONFIGURATION_LEVELS = {
    "valid": Level.SUCCESS,
    "invalid": Level.FAILURE,
    "unverified": Level.UNVERIFIED,
}
_OWNERSHIP_LINE = "ownership_status: unverified"
_TABLE_HEADERS = ("Status", "Code", "Message")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestune doctor",
        description="Diagnose dispatch single-executor setup (offline, read-only).",
    )
    parser.add_argument("--execution-mode", choices=("local", "actions"), required=True)
    parser.add_argument(
        "--workflow",
        action="append",
        default=[],
        metavar="PATH",
        help="Workflow file to diagnose (actions mode; repeatable).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    return parser


def _title(report: DoctorReport) -> str:
    return f"=== Orchestune Dispatch Doctor (mode: {report.mode}) ==="


def _detail_lines(diag: Diagnostic) -> list[str]:
    lines = [f"    - {item}" for item in diag.evidence]
    if diag.remediation:
        lines.append(f"    → {diag.remediation}")
    return lines


def _configuration_line(report: DoctorReport) -> str:
    status = report.configuration_status
    return (
        f"{label_token(_CONFIGURATION_LEVELS[status])} configuration_status: {status}"
    )


def render_text(report: DoctorReport) -> str:
    """Plain-text presentation: one labelled line per diagnostic plus details."""
    lines = [_title(report)]
    for diag in report.diagnostics:
        level = _DIAGNOSTIC_LEVELS[diag.status]
        lines.append(f"{label_token(level)} {diag.code}: {diag.message}")
        if diag.status != "ok":
            lines.extend(_detail_lines(diag))
    lines.append(_configuration_line(report))
    lines.append(f"{label_token(Level.UNVERIFIED)} {_OWNERSHIP_LINE}")
    lines.append(OWNERSHIP_NOTICE)
    return "\n".join(lines)


def render_report(out: CliOutput, report: DoctorReport) -> None:
    """Show the report: plain text, or a table plus details on a decorated TTY."""
    if not out.policy.rich:
        out.emit(out.text_block(render_text(report)))
        return
    out.emit(out.text_block(_title(report)))
    rows = [
        (
            label_token(_DIAGNOSTIC_LEVELS[diag.status]),
            diag.code,
            diag.message,
        )
        for diag in report.diagnostics
    ]
    out.table(_TABLE_HEADERS, rows)
    for diag in report.diagnostics:
        if diag.status != "ok" and (details := _detail_lines(diag)):
            out.emit(out.text_block("\n".join(["", diag.code, *details])))
    status = report.configuration_status
    out.emit(out.text_block(""))
    out.message(_CONFIGURATION_LEVELS[status], f"configuration_status: {status}")
    out.message(Level.UNVERIFIED, _OWNERSHIP_LINE)
    out.emit(out.text_block(OWNERSHIP_NOTICE))


def main(argv: list[str] | None = None, *, output: CliOutput | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.execution_mode == "actions" and not args.workflow:
        parser.error("--workflow is required with --execution-mode actions")
    if args.execution_mode == "local" and args.workflow:
        parser.error("--workflow is only valid with --execution-mode actions")
    try:
        cwd = Path.cwd()
        repo_root = resolve_repository_root()
        workflows = tuple(
            normalize_workflow_path(raw, repo_root, cwd) for raw in args.workflow
        )
    except DoctorInputError as exc:
        print(f"orchestune doctor: {exc}", file=sys.stderr)
        return 2
    report = run_doctor(DoctorRequest(args.execution_mode, repo_root, workflows))
    out = output if output is not None else make_output(machine=args.json)
    if args.json:
        out.payload(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
    else:
        render_report(out, report)
    return 1 if report.has_error else 0
