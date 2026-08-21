#!/usr/bin/env python3
"""Persist and compare agent-local Local CI baselines.

This wrapper intentionally does not change ``local-ci.sh``.  CI runners and
the Integrator keep invoking that script directly as an absolute gate; workers
may invoke this utility when they need to distinguish a newly introduced CI
failure from a failure already present on the base branch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = PROJECT_ROOT / ".orchestune" / "baseline.json"
DEFAULT_CI_COMMAND = "./scripts/local-ci.sh"
EXIT_BASE_BRANCH_RED = 2
EXIT_STATE_ERROR = 70
STATE_VERSION = 1


@dataclass(frozen=True)
class CiResult:
    """The exit status and stable failure identifiers from one Local CI run."""

    exit_code: int
    failures: list[str]
    output: str


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record and compare an agent-local Local CI baseline."
    )
    parser.add_argument("command", choices=("record", "check"))
    parser.add_argument(
        "--base-branch",
        default="main",
        help="branch used to calculate git merge-base (default: main)",
    )
    parser.add_argument(
        "--ci-command",
        default=DEFAULT_CI_COMMAND,
        help=f"Local CI command to execute (default: {DEFAULT_CI_COMMAND})",
    )
    return parser.parse_args(argv)


def _merge_base(base_branch: str) -> str:
    result = subprocess.run(
        ["git", "merge-base", "HEAD", base_branch],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"could not calculate merge-base with {base_branch}: {detail}")

    base_sha = result.stdout.strip()
    if not base_sha:
        raise RuntimeError(f"git merge-base returned no SHA for {base_branch}")
    return base_sha


def _normalise_output(output: str) -> str:
    """Remove machine-specific noise before using a CI output fallback key."""

    normalised = output.replace(str(PROJECT_ROOT), "<project-root>")
    normalised = re.sub(r"\b[0-9a-f]{40}\b", "<sha>", normalised)
    normalised = re.sub(r"\b\d+(?:\.\d+)?s\b", "<duration>", normalised)
    return normalised


def _failure_identifiers(output: str, exit_code: int) -> list[str]:
    if exit_code == 0:
        return []

    # pytest's node IDs are the most useful persistent identity when available.
    identifiers = re.findall(r"^FAILED\s+([^\s]+)", output, flags=re.MULTILINE)
    if identifiers:
        return sorted(set(identifiers))

    # Other Local CI steps often report a stable error line.  Preserve every
    # distinct line rather than treating unrelated formatter/type failures as
    # one baseline failure.
    error_lines = re.findall(r"^(?:error:|ERROR:|Error: )\s*(.+)$", output, re.MULTILINE)
    if error_lines:
        return sorted({f"error:{line.strip()}" for line in error_lines if line.strip()})

    digest = hashlib.sha256(_normalise_output(output).encode()).hexdigest()[:16]
    return [f"ci-output:{digest}"]


def _run_ci(command: str) -> CiResult:
    command_parts = shlex.split(command)
    if not command_parts:
        raise RuntimeError("--ci-command must not be empty")

    result = subprocess.run(
        command_parts,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    output = f"{result.stdout}{result.stderr}"
    if output:
        print(output, end="" if output.endswith("\n") else "\n")
    return CiResult(
        exit_code=result.returncode,
        failures=_failure_identifiers(output, result.returncode),
        output=output,
    )


def _read_state(path: Path | None = None) -> dict[str, object] | None:
    path = BASELINE_PATH if path is None else path
    try:
        with path.open(encoding="utf-8") as state_file:
            state = json.load(state_file)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[ci-baseline] Cannot read baseline state: {exc}", file=sys.stderr)
        return None

    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        print("[ci-baseline] Baseline state is invalid; using absolute mode.")
        return None
    return state


def _write_state(state: dict[str, object], path: Path | None = None) -> None:
    path = BASELINE_PATH if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".json.tmp")
    with temporary_path.open("w", encoding="utf-8") as state_file:
        json.dump(state, state_file, ensure_ascii=False, indent=2, sort_keys=True)
        state_file.write("\n")
    temporary_path.replace(path)


def _baseline_payload(base_sha: str, ci_command: str, result: CiResult) -> dict[str, object]:
    return {
        "version": STATE_VERSION,
        "base_sha": base_sha,
        "ci_command": ci_command,
        "baseline": {
            "exit_code": result.exit_code,
            "failures": result.failures,
        },
        # Kept independently from the baseline so a resumed agent can continue
        # Failure Analyst's consecutive-failure count.
        "failure_counts": {},
    }


def _update_failure_counts(state: dict[str, object], failures: list[str]) -> None:
    previous = state.get("failure_counts")
    previous_counts = previous if isinstance(previous, dict) else {}
    state["failure_counts"] = {
        failure: int(previous_counts.get(failure, 0)) + 1 for failure in failures
    }


def _record(base_sha: str, ci_command: str) -> int:
    result = _run_ci(ci_command)
    _write_state(_baseline_payload(base_sha, ci_command, result))
    print(f"[ci-baseline] Recorded baseline for {base_sha[:12]}.")
    return result.exit_code


def _check(base_sha: str, ci_command: str) -> int:
    state = _read_state()
    if state is None:
        print("[ci-baseline] No baseline: using Local CI as an absolute gate.")
        return _run_ci(ci_command).exit_code

    if state.get("base_sha") != base_sha:
        print("[ci-baseline] Baseline is stale after base branch advancement; re-recording.")
        return _record(base_sha, ci_command)

    result = _run_ci(ci_command)
    _update_failure_counts(state, result.failures)
    _write_state(state)

    if result.exit_code == 0:
        print("[ci-baseline] Local CI passed.")
        return 0

    baseline = state.get("baseline")
    baseline_failures = (
        set(baseline.get("failures", [])) if isinstance(baseline, dict) else set()
    )
    current_failures = set(result.failures)
    new_failures = sorted(current_failures - baseline_failures)
    existing_failures = sorted(current_failures & baseline_failures)

    if new_failures:
        print("[ci-baseline] New CI failures: " + ", ".join(new_failures), file=sys.stderr)
        return result.exit_code

    if existing_failures:
        print("[ci-baseline] base-branch-red: only baseline CI failures remain.")
        for failure in existing_failures:
            count = state["failure_counts"].get(failure, 0)  # type: ignore[index]
            print(f"[ci-baseline] Failure Analyst count {count}: {failure}")
        return EXIT_BASE_BRANCH_RED

    # A failed command with no identifiers is defensive-only; do not turn an
    # unclassified failure into a pass or a base-branch attribution.
    print("[ci-baseline] Unclassified CI failure.", file=sys.stderr)
    return result.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        base_sha = _merge_base(args.base_branch)
        if args.command == "record":
            return _record(base_sha, args.ci_command)
        return _check(base_sha, args.ci_command)
    except (OSError, RuntimeError) as exc:
        print(f"[ci-baseline] {exc}", file=sys.stderr)
        return EXIT_STATE_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
