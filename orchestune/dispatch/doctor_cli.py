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
from orchestune.dispatch.doctor_models import OWNERSHIP_NOTICE, DoctorReport

_ICONS = {"ok": "✓", "warning": "⚠", "error": "✗", "not_checked": "○"}


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


def render_text(report: DoctorReport) -> str:
    lines = [f"=== Orchestune Dispatch Doctor (mode: {report.mode}) ==="]
    for diag in report.diagnostics:
        lines.append(f"[{_ICONS[diag.status]}] {diag.code}: {diag.message}")
        if diag.status == "ok":
            continue
        lines.extend(f"    - {item}" for item in diag.evidence)
        if diag.remediation:
            lines.append(f"    → {diag.remediation}")
    lines.append(f"configuration_status: {report.configuration_status}")
    lines.append("ownership_status: unverified")
    lines.append(OWNERSHIP_NOTICE)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
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
    if args.json:
        print(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 1 if report.has_error else 0
