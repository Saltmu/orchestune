"""Command-line interface for local handoff-ready reservation cleanup."""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence
from pathlib import Path

from orchestune.dispatch.gc.handoff import GcRequest
from orchestune.dispatch.gc_service import GcItemResult, run_handoff_gc


def _timeout_value(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if not math.isfinite(timeout) or timeout < 0:
        raise argparse.ArgumentTypeError(
            "timeout must be a finite number greater than or equal to zero"
        )
    return timeout


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestune gc",
        description="Release locally handoff-ready task reservations.",
    )
    parser.add_argument(
        "--no-apply",
        action="store_true",
        help="preview decisions without changing local state or worktrees",
    )
    parser.add_argument(
        "--state",
        type=Path,
        help="run_state.json path (relative paths use the primary checkout)",
    )
    parser.add_argument(
        "--timeout",
        type=_timeout_value,
        default=0.0,
        help="lock wait in seconds (default: 0)",
    )
    return parser


def _worktree_action(item: GcItemResult, preview: bool) -> str:
    if not preview:
        return item.worktree_action
    return {
        "remove": "would_remove",
        "retain": "would_retain",
        "absent": "absent",
    }.get(item.worktree_action, item.worktree_action)


def _print_items(items: tuple[GcItemResult, ...], preview: bool) -> None:
    print("ISSUE  RESULT      ACTION         REASON                    WORKTREE")
    for item in items:
        print(
            f"#{item.issue_number:<5} {item.result or '-':<11} "
            f"{item.action:<14} {item.reason:<25} "
            f"{_worktree_action(item, preview)}: {item.worktree_path}"
        )


def _print_summary(
    items: tuple[GcItemResult, ...], skipped: int, exit_code: int, preview: bool
) -> None:
    if not items:
        if exit_code == 0:
            print("No handoff-ready reservations found.")
        elif exit_code == 22:
            print("GC stopped because it could not acquire a required lock.")
        else:
            print("GC stopped because state could not be read or persisted.")
    action = "would release" if preview else "released"
    count = sum(1 for item in items if item.action in {"would_release", "released"})
    held = sum(1 for item in items if item.action == "held")
    failed = sum(1 for item in items if item.action == "failed")
    print(f"Summary: {count} {action}, {held} held, {failed} failed, {skipped} skipped")
    if exit_code:
        print(f"GC exited with status {exit_code}.")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    request = GcRequest(
        state_path=args.state,
        apply=not args.no_apply,
        timeout_seconds=args.timeout,
    )
    result = run_handoff_gc(request)
    if args.no_apply:
        print("Preview only; no local state or worktree changes were made.")
    if result.items:
        _print_items(result.items, args.no_apply)
    _print_summary(result.items, result.skipped, result.exit_code, args.no_apply)
    return result.exit_code
