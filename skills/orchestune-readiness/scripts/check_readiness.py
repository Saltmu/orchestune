"""CLI helper for orchestune-readiness skill.

Checks skip_readiness_check configuration and invokes orchestune doctor when applicable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from orchestune.config_contracts import ConfigError
from orchestune.config_schema import validate_toml_config
from orchestune.dispatch.doctor import check as doctor_check
from orchestune.infra.repository_config import find_and_load_config_file


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_readiness",
        description="Check readiness for orchestune decomposition and parallel tasks.",
    )
    parser.add_argument(
        "--repo-root",
        required=True,
        type=Path,
        help="Path to repository root directory.",
    )
    parser.add_argument(
        "--execution-mode",
        required=True,
        choices=["local", "actions"],
        help="Execution mode (local or actions).",
    )
    parser.add_argument(
        "--workflow",
        action="append",
        default=[],
        help="Target GitHub Actions workflow path (repeatable; required with actions).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    repo_root = args.repo_root
    if not repo_root.exists() or not repo_root.is_dir():
        print(
            f"check_readiness: --repo-root does not exist or is not a directory: {repo_root}",
            file=sys.stderr,
        )
        return 2

    resolved_root = repo_root.resolve()

    if args.execution_mode == "actions" and not args.workflow:
        print(
            "check_readiness: --workflow is required with --execution-mode actions",
            file=sys.stderr,
        )
        return 2

    if args.execution_mode == "local" and args.workflow:
        print(
            "check_readiness: --workflow is only valid with --execution-mode actions",
            file=sys.stderr,
        )
        return 2

    current_dir = Path.cwd().resolve()
    effective_cwd = (
        current_dir
        if current_dir == resolved_root or current_dir.is_relative_to(resolved_root)
        else resolved_root
    )

    try:
        workflows = tuple(
            doctor_check.normalize_workflow_path(raw, resolved_root, effective_cwd)
            for raw in args.workflow
        )
    except doctor_check.DoctorInputError as exc:
        print(f"check_readiness: {exc}", file=sys.stderr)
        return 2

    try:
        raw_config = find_and_load_config_file(resolved_root)
        config = validate_toml_config(raw_config)
    except ConfigError as exc:
        print(f"check_readiness: configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - boundary: report configuration loading errors
        print(f"check_readiness: failed to load configuration: {exc}", file=sys.stderr)
        return 2

    skip = config.get("skip_readiness_check", False)
    if skip is True:
        payload = {
            "schema_version": 1,
            "skipped": True,
            "doctor": None,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    try:
        report = doctor_check.run_doctor(
            doctor_check.DoctorRequest(
                args.execution_mode,
                resolved_root,
                workflows,
            )
        )
    except Exception as exc:  # noqa: BLE001 - boundary: report doctor failures
        print(f"check_readiness: doctor check failed: {exc}", file=sys.stderr)
        return 2

    payload = {
        "schema_version": 1,
        "skipped": False,
        "doctor": report.to_json(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if report.has_error else 0


if __name__ == "__main__":
    sys.exit(main())
