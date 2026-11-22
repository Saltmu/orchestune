from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from orchestune.infra.cli_output import Level, make_output
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
from orchestune.installer.state import inspect_bundle_state
from orchestune.installer.targets import resolve_targets
from orchestune.installer.workflow_detection import (
    ProjectWorkflowDetection,
    detect_project_workflow,
    format_merge_guidance_lines,
)
from orchestune.session_dirs import find_project_root

_DOCTOR_LEVELS: dict[str, Level] = {
    "ok": Level.SUCCESS,
    "warning": Level.WARNING,
    "error": Level.FAILURE,
    "not_checked": Level.SKIPPED,
}


def _report_error(text: str) -> None:
    make_output(sys.stderr).message(Level.FAILURE, text)


def _add_common_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "-t",
        "--target",
        action="append",
        required=True,
        choices=["codex", "claude", "antigravity", "antigravity-cli", "grok", "all"],
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
        if cmd == "update":
            sub.add_argument(
                "--with-workflow-skill",
                action="store_true",
                help="Update workflow-template skill (project scope only)",
            )
        if cmd == "install":
            wf_group = sub.add_mutually_exclusive_group()
            wf_group.add_argument(
                "--with-workflow-skill",
                action="store_true",
                help="Include workflow-template skill alongside standard skills (project scope only)",
            )
            wf_group.add_argument(
                "--no-workflow-skill",
                action="store_true",
                help="Skip automatic workflow-template installation (project scope only)",
            )
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


def _resolve_effective_project_dir(
    args: argparse.Namespace, scope: ScopeType
) -> Path | None:
    if scope != ScopeType.PROJECT:
        return None
    if isinstance(args.project_dir, Path):
        return args.project_dir.resolve()
    if args.skills_dir is not None:
        return None
    return find_project_root().resolve()


def _handle_doctor(
    roots: list[PhysicalRoot],
    targets: list[TargetType],
    scope: ScopeType,
    offline: bool,
    as_json: bool,
    project_dir: Path | None = None,
) -> int:
    diagnostics = run_doctor_checks(roots, offline=offline, project_dir=project_dir)
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
        make_output(machine=True).payload(json.dumps(output, indent=2))
    else:
        out = make_output(sys.stdout)
        out.emit(out.text_block("=== Orchestune Skills Doctor ==="))
        for d in diagnostics:
            level = _DOCTOR_LEVELS.get(d.status, Level.UNVERIFIED)
            out.message(level, f"{d.check_name}: {d.message}")
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
        _report_error(f"Conflict error at {root.path}: {e}")
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
        _report_error(f"Installer error at {root.path}: {e}")
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
    except Exception as e:  # noqa: BLE001 - boundary: render unexpected installer operation failures as the existing result
        _report_error(f"Unexpected error at {root.path}: {e}")
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


def _build_workflow_guidance(
    detection: ProjectWorkflowDetection,
    *,
    with_workflow: bool,
    no_workflow: bool,
    dry_run: bool,
) -> dict[str, Any]:
    action, recommended = _classify_guidance_action(
        detection,
        with_workflow=with_workflow,
        no_workflow=no_workflow,
        dry_run=dry_run,
    )
    return {
        "status": detection.status,
        "action": action,
        "detected_discipline_files": list(detection.discipline_files),
        "detected_custom_skills": list(detection.custom_skills),
        "detected_workflow_templates": list(detection.workflow_templates),
        "missing_procedures": list(detection.missing_procedures),
        "template_has_placeholders": detection.template_has_placeholders,
        "recommended_actions": recommended,
        "merge_checklist": list(detection.merge_checklist),
    }


def _classify_guidance_action(
    detection: ProjectWorkflowDetection,
    *,
    with_workflow: bool,
    no_workflow: bool,
    dry_run: bool,
) -> tuple[str, list[str]]:
    placeholder_hint = "workflow-template 内の <CI_ENTRYPOINT> / <TEST_COMMAND> 等をプロジェクト固有コマンドへ置換してください。"
    if with_workflow:
        action = "would_install" if dry_run else "explicitly_installed"
        recs = [placeholder_hint]
        if detection.status == "merge_recommended":
            recs.extend(format_merge_guidance_lines(detection))
        return action, recs
    if no_workflow:
        recs = (
            list(format_merge_guidance_lines(detection))
            if detection.status == "merge_recommended"
            else (
                [
                    "開発スキルが未検出のため、`orchestune skills install --target all --scope project --with-workflow-skill` でテンプレートを導入してください。"
                ]
                if detection.status == "missing"
                else []
            )
        )
        return "skipped_by_flag", recs
    if detection.status == "missing":
        return (
            "would_auto_install" if dry_run else "auto_installed",
            [placeholder_hint],
        )
    if detection.status == "workflow_template":
        recs = [placeholder_hint] if detection.template_has_placeholders else []
        return "already_present", recs
    if detection.status == "merge_recommended":
        return "merge_proposed", list(format_merge_guidance_lines(detection))
    return "already_configured", []


def _print_workflow_guidance(out: Any, guidance: dict[str, Any]) -> None:
    action = guidance["action"]
    if action in ("auto_installed", "would_auto_install"):
        verb = (
            "Would auto-install" if action == "would_auto_install" else "Auto-installed"
        )
        out.message(
            Level.SUCCESS,
            f"{verb} 'workflow-template' skill (no existing development skill or discipline doc detected).",
        )
    elif action == "merge_proposed":
        out.message(
            Level.WARNING,
            "Existing development skill / discipline doc detected without 'orchestune complete'. "
            "Preserving existing files; merge recommended:",
        )
    elif action == "already_configured":
        out.message(
            Level.SUCCESS,
            "Existing project development workflow covers 'orchestune complete'.",
        )
    elif action == "already_present":
        out.message(
            Level.INFO,
            "Existing 'workflow-template' skill preserved.",
        )
    elif action == "skipped_by_flag":
        out.message(
            Level.INFO,
            "Skipped automatic 'workflow-template' installation (--no-workflow-skill).",
        )
    for line in guidance.get("recommended_actions", ()):
        out.emit(out.text_block(f"    * {line}"))


def _print_results(
    results: list[OperationResult],
    subcommand: str,
    scope: ScopeType,
    targets: list[TargetType],
    roots: list[PhysicalRoot],
    as_json: bool,
    workflow_guidance: dict[str, Any] | None = None,
) -> None:
    any_failure = any(not r.success for r in results)
    if as_json:
        overall_status = "ok"
        if any_failure:
            overall_status = (
                "failed" if all(not r.success for r in results) else "partial_failure"
            )
        output_data: dict[str, Any] = {
            "schema_version": 1,
            "operation": subcommand,
            "scope": scope.value,
            "logical_targets": [t.value for t in targets],
            "physical_roots": [str(r.path) for r in roots],
            "results": [r.to_dict() for r in results],
            "overall_status": overall_status,
        }
        if workflow_guidance is not None:
            output_data["workflow_guidance"] = workflow_guidance
        make_output(machine=True).payload(json.dumps(output_data, indent=2))
    else:
        out = make_output(sys.stdout)
        out.message(Level.INFO, f"Orchestune skills {subcommand} completed:")
        for r in results:
            level = Level.SUCCESS if r.success else Level.FAILURE
            out.message(level, f"{r.physical_root} ({r.bundle_name}):")
            for act in r.actions:
                out.emit(out.text_block(f"    - {act}"))
            if r.error:
                out.message(Level.FAILURE, f"Error: {r.error}")
        if workflow_guidance is not None:
            _print_workflow_guidance(out, workflow_guidance)


def _resolve_roots_and_payload(
    args: argparse.Namespace,
) -> tuple[list[PhysicalRoot], BundlePayload | None, BundlePayload | None] | int:
    scope = ScopeType(args.scope)
    with_workflow = getattr(args, "with_workflow_skill", False)
    no_workflow = getattr(args, "no_workflow_skill", False)
    if (with_workflow or no_workflow) and scope == ScopeType.USER:
        flag = "--with-workflow-skill" if with_workflow else "--no-workflow-skill"
        _report_error(f"Error: {flag} is only allowed with --scope project.")
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
        _report_error(f"Error: {e}")
        return int(InstallerExitCode.CONTRACT_ERROR)

    if args.subcommand == "doctor":
        return roots, None, None

    source_dir = getattr(args, "source_dir", None)
    try:
        if args.subcommand == "install":
            standard_payload = resolve_payload(
                source_dir=source_dir, with_workflow_skill=False
            )
            workflow_payload = (
                resolve_payload(source_dir=source_dir, with_workflow_skill=True)
                if scope == ScopeType.PROJECT and not no_workflow
                else None
            )
            return roots, standard_payload, workflow_payload
        payload = resolve_payload(
            source_dir=source_dir, with_workflow_skill=with_workflow
        )
        return roots, payload, None
    except PayloadError as e:
        _report_error(f"Error: {e}")
        return int(InstallerExitCode.CONTRACT_ERROR)


def _should_install_workflow_for_root(
    root: PhysicalRoot,
    workflow_payload: BundlePayload,
    detection: ProjectWorkflowDetection,
    *,
    with_workflow: bool,
    no_workflow: bool,
) -> bool:
    if no_workflow:
        return False
    if with_workflow or detection.status == "missing":
        return True
    if detection.status == "workflow_template":
        state = inspect_bundle_state(root.path, workflow_payload)
        return state in (BundleState.ABSENT, BundleState.MANAGED_CURRENT)
    return False


def _run_root_operations(
    roots: list[PhysicalRoot],
    args: argparse.Namespace,
    payload: BundlePayload,
    workflow_payload: BundlePayload | None,
    detection: ProjectWorkflowDetection | None,
) -> tuple[list[OperationResult], int]:
    dry_run = args.dry_run
    migrate_legacy = getattr(args, "migrate_legacy", False)
    with_workflow = getattr(args, "with_workflow_skill", False)
    no_workflow = getattr(args, "no_workflow_skill", False)

    results: list[OperationResult] = []
    overall_exit_code: int = int(InstallerExitCode.OK)
    for root in roots:
        res, code = _execute_root_operation(
            root, args.subcommand, payload, dry_run, migrate_legacy
        )
        results.append(res)
        overall_exit_code = max(overall_exit_code, int(code))

        if (
            args.subcommand == "install"
            and workflow_payload is not None
            and detection is not None
            and _should_install_workflow_for_root(
                root,
                workflow_payload,
                detection,
                with_workflow=with_workflow,
                no_workflow=no_workflow,
            )
        ):
            wf_res, wf_code = _execute_root_operation(
                root, "install", workflow_payload, dry_run, migrate_legacy
            )
            results.append(wf_res)
            overall_exit_code = max(overall_exit_code, int(wf_code))
    return results, overall_exit_code


def _resolve_project_workflow(
    args: argparse.Namespace,
    scope: ScopeType,
    roots: list[PhysicalRoot],
    project_dir: Path | None,
) -> tuple[ProjectWorkflowDetection | None, dict[str, Any] | None]:
    if args.subcommand != "install" or scope != ScopeType.PROJECT:
        return None, None
    detection = detect_project_workflow(
        project_dir, extra_skills_roots=[r.path for r in roots]
    )
    guidance = _build_workflow_guidance(
        detection,
        with_workflow=getattr(args, "with_workflow_skill", False),
        no_workflow=getattr(args, "no_workflow_skill", False),
        dry_run=args.dry_run,
    )
    return detection, guidance


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 1

    prep = _resolve_roots_and_payload(args)
    if isinstance(prep, int):
        return prep

    roots, payload, workflow_payload = prep
    targets = [TargetType(t) for t in args.target]
    scope = ScopeType(args.scope)
    project_dir = _resolve_effective_project_dir(args, scope)

    if args.subcommand == "doctor":
        return _handle_doctor(
            roots,
            targets,
            scope,
            getattr(args, "offline", False),
            args.json,
            project_dir=project_dir,
        )

    assert payload is not None
    detection, workflow_guidance = _resolve_project_workflow(
        args, scope, roots, project_dir
    )
    results, overall_exit_code = _run_root_operations(
        roots, args, payload, workflow_payload, detection
    )
    if any(not r.success for r in results) and overall_exit_code == int(
        InstallerExitCode.OK
    ):
        overall_exit_code = int(InstallerExitCode.ERROR)

    _print_results(
        results,
        args.subcommand,
        scope,
        targets,
        roots,
        args.json,
        workflow_guidance=workflow_guidance,
    )
    return overall_exit_code


if __name__ == "__main__":
    sys.exit(main())
