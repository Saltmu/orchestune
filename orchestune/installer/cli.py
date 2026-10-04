from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from orchestune.installer.contracts import (
    BundlePayload,
    BundleState,
    ConflictError,
    InstallerError,
    InstallerExitCode,
    PayloadError,
    PhysicalRoot,
    ScopeType,
    TargetResolutionError,
    TargetType,
)
from orchestune.installer.doctor import run_doctor_checks
from orchestune.installer.engine import (
    OperationResult,
    inspect_status,
    install_skills,
    uninstall_skills,
    update_skills,
)
from orchestune.installer.payload import resolve_payload
from orchestune.installer.targets import resolve_targets


def _add_common_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "-t",
        "--target",
        action="append",
        required=True,
        choices=["codex", "claude", "antigravity", "antigravity-cli", "all"],
        help="Target assistant (repeatable or 'all')",
    )
    sub.add_argument(
        "-s",
        "--scope",
        required=True,
        choices=["project", "user"],
        help="Target installation scope (project or user)",
    )
    sub.add_argument(
        "--project-dir", type=Path, help="Explicit target project directory"
    )
    sub.add_argument("--home", type=Path, help="Explicit target user home directory")
    sub.add_argument(
        "--skills-dir",
        type=Path,
        help="Override target skills directory (single target only)",
    )
    sub.add_argument(
        "--dry-run", action="store_true", help="Preview actions without writing"
    )
    sub.add_argument(
        "--json", action="store_true", help="Output results in JSON format"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestune skills",
        description="Install and manage Orchestune skills for AI assistants",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    for cmd in ["install", "update", "uninstall", "status", "doctor"]:
        sub = subparsers.add_parser(cmd)
        _add_common_arguments(sub)
        if cmd in ("install", "update"):
            sub.add_argument(
                "--source-dir", type=Path, help="Explicit source checkout root"
            )
            sub.add_argument(
                "--with-workflow-skill",
                action="store_true",
                help="Include workflow-template skill (project scope only)",
            )
        if cmd == "install":
            sub.add_argument(
                "--migrate-legacy",
                action="store_true",
                help="Migrate legacy skill symlinks or copies safely",
            )
        if cmd == "doctor":
            sub.add_argument(
                "--offline",
                action="store_true",
                help="Skip network checks (such as GitHub authentication status)",
            )

    return parser


def _handle_doctor(
    roots: list[PhysicalRoot],
    targets: list[TargetType],
    scope: ScopeType,
    offline: bool,
    as_json: bool,
) -> int:
    diagnostics = run_doctor_checks(roots, offline=offline)
    has_error = any(d.status == "error" for d in diagnostics)
    if as_json:
        output = {
            "schema_version": 1,
            "operation": "doctor",
            "scope": scope.value,
            "logical_targets": [t.value for t in targets],
            "physical_roots": [str(r.path) for r in roots],
            "diagnostics": [
                {
                    "name": d.check_name,
                    "status": d.status,
                    "message": d.message,
                    "details": d.details,
                }
                for d in diagnostics
            ],
            "overall_status": "failed" if has_error else "ok",
        }
        print(json.dumps(output, indent=2))
    else:
        print("=== Orchestune Skills Doctor ===")
        for d in diagnostics:
            prefix = {
                "ok": "✓",
                "warning": "⚠",
                "error": "✗",
                "not_checked": "○",
            }.get(d.status, "?")
            print(f"[{prefix}] {d.check_name}: {d.message}")
    return InstallerExitCode.ERROR if has_error else InstallerExitCode.OK


def _dispatch_action(
    root: PhysicalRoot,
    subcommand: str,
    payload: BundlePayload,
    dry_run: bool,
    migrate_legacy: bool,
) -> OperationResult:
    if subcommand == "install":
        return install_skills(
            root, payload, dry_run=dry_run, migrate_legacy=migrate_legacy
        )
    if subcommand == "update":
        return update_skills(root, payload, dry_run=dry_run)
    if subcommand == "uninstall":
        return uninstall_skills(root, payload, dry_run=dry_run)
    if subcommand == "status":
        return inspect_status(root, payload)
    raise ValueError(f"Unknown subcommand {subcommand}")


def _execute_root_operation(
    root: PhysicalRoot,
    subcommand: str,
    payload: BundlePayload,
    dry_run: bool,
    migrate_legacy: bool,
) -> tuple[OperationResult, int]:
    try:
        res = _dispatch_action(root, subcommand, payload, dry_run, migrate_legacy)
        return res, InstallerExitCode.OK
    except ConflictError as e:
        print(f"Conflict error at {root.path}: {e}", file=sys.stderr)
        res = OperationResult(
            physical_root=root.path,
            bundle_name=payload.bundle_name,
            operation=subcommand,
            state_before=BundleState.MODIFIED,
            state_after=BundleState.MODIFIED,
            success=False,
            error=str(e),
        )
        return res, InstallerExitCode.CONFLICT
    except InstallerError as e:
        print(f"Installer error at {root.path}: {e}", file=sys.stderr)
        res = OperationResult(
            physical_root=root.path,
            bundle_name=payload.bundle_name,
            operation=subcommand,
            state_before=BundleState.STATE_INVALID,
            state_after=BundleState.STATE_INVALID,
            success=False,
            error=str(e),
        )
        return res, e.exit_code
    except Exception as e:
        print(f"Unexpected error at {root.path}: {e}", file=sys.stderr)
        res = OperationResult(
            physical_root=root.path,
            bundle_name=payload.bundle_name,
            operation=subcommand,
            state_before=BundleState.STATE_INVALID,
            state_after=BundleState.STATE_INVALID,
            success=False,
            error=str(e),
        )
        return res, InstallerExitCode.ERROR


def _print_results(
    results: list[OperationResult],
    subcommand: str,
    scope: ScopeType,
    targets: list[TargetType],
    roots: list[PhysicalRoot],
    as_json: bool,
) -> None:
    any_failure = any(not r.success for r in results)
    if as_json:
        overall_status = "ok"
        if any_failure:
            overall_status = (
                "failed" if all(not r.success for r in results) else "partial_failure"
            )
        output_data = {
            "schema_version": 1,
            "operation": subcommand,
            "scope": scope.value,
            "logical_targets": [t.value for t in targets],
            "physical_roots": [str(r.path) for r in roots],
            "results": [r.to_dict() for r in results],
            "overall_status": overall_status,
        }
        print(json.dumps(output_data, indent=2))
    else:
        print(f"Orchestune skills {subcommand} completed:")
        for r in results:
            status_str = "SUCCESS" if r.success else "FAILED"
            print(f"  [{status_str}] {r.physical_root} ({r.bundle_name}):")
            for act in r.actions:
                print(f"    - {act}")
            if r.error:
                print(f"    Error: {r.error}")


def _resolve_roots_and_payload(
    args: argparse.Namespace,
) -> tuple[list[PhysicalRoot], BundlePayload | None] | int:
    scope = ScopeType(args.scope)
    with_workflow = getattr(args, "with_workflow_skill", False)
    if with_workflow and scope == ScopeType.USER:
        print(
            "Error: --with-workflow-skill is only allowed with --scope project.",
            file=sys.stderr,
        )
        return int(InstallerExitCode.CONTRACT_ERROR)

    targets = [TargetType(t) for t in args.target]
    try:
        roots = resolve_targets(
            targets=targets,
            scope=scope,
            project_dir=args.project_dir,
            home=args.home,
            skills_dir=args.skills_dir,
        )
    except TargetResolutionError as e:
        print(f"Error: {e}", file=sys.stderr)
        return int(InstallerExitCode.CONTRACT_ERROR)

    if args.subcommand == "doctor":
        return roots, None

    source_dir = getattr(args, "source_dir", None)
    try:
        payload = resolve_payload(
            source_dir=source_dir, with_workflow_skill=with_workflow
        )
    except PayloadError as e:
        print(f"Error: {e}", file=sys.stderr)
        return int(InstallerExitCode.CONTRACT_ERROR)

    return roots, payload


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 1

    prep = _resolve_roots_and_payload(args)
    if isinstance(prep, int):
        return prep

    roots, payload = prep
    targets = [TargetType(t) for t in args.target]
    scope = ScopeType(args.scope)

    if args.subcommand == "doctor":
        return _handle_doctor(
            roots, targets, scope, getattr(args, "offline", False), args.json
        )

    assert payload is not None
    dry_run = args.dry_run
    migrate_legacy = getattr(args, "migrate_legacy", False)
    results: list[OperationResult] = []
    overall_exit_code: int = int(InstallerExitCode.OK)

    for root in roots:
        res, code = _execute_root_operation(
            root, args.subcommand, payload, dry_run, migrate_legacy
        )
        results.append(res)
        overall_exit_code = max(overall_exit_code, int(code))

    if any(not r.success for r in results) and overall_exit_code == int(
        InstallerExitCode.OK
    ):
        overall_exit_code = int(InstallerExitCode.ERROR)

    _print_results(results, args.subcommand, scope, targets, roots, args.json)
    return overall_exit_code


if __name__ == "__main__":
    sys.exit(main())
