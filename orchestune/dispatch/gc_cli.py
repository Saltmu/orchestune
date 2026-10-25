"""Command-line interface for local handoff-ready reservation cleanup."""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Sequence
from pathlib import Path

from orchestune.dispatch.gc.handoff import GcRequest
from orchestune.dispatch.gc_service import GcItemResult, run_handoff_gc
from orchestune.infra.cli_output import Block, CliOutput, make_output

_TABLE_HEADERS = ("ISSUE", "RESULT", "ACTION", "REASON", "WORKTREE")


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
        description="Resume verified completion policies and collect interactive worktrees.",
    )
    parser.add_argument(
        "--no-apply",
        action="store_true",
        help="preview decisions without changing state, worktrees, or GitHub",
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


def _write_lines(output: CliOutput, lines: Sequence[str]) -> None:
    """Write literal lines through the shared output layer (plain text is unchanged)."""
    for line in lines:
        output.emit(output.text_block(line))


def _print_items(items: tuple[GcItemResult, ...], preview: bool) -> None:
    output = make_output(sys.stdout)
    # Plain mode keeps the fixed-width layout; the rich form is the shared table,
    # which is boxed when it fits and stacked (never truncated) when it does not.
    plain = ["ISSUE  RESULT      ACTION         REASON                    WORKTREE"]
    rows: list[tuple[str, str, str, str, str]] = []
    for item in items:
        result = item.result or "-"
        worktree = f"{_worktree_action(item, preview)}: {item.worktree_path}"
        plain.append(
            f"#{item.issue_number:<5} {result:<11} "
            f"{item.action:<14} {item.reason:<25} {worktree}"
        )
        rows.append(
            (f"#{item.issue_number}", result, item.action, item.reason, worktree)
        )
    table = output.table_block(_TABLE_HEADERS, rows)
    text = output.text_block("\n".join(plain))
    output.emit(Block(text.plain, table.rich, table.soft_wrap))


def _print_summary(
    items: tuple[GcItemResult, ...], skipped: int, exit_code: int, preview: bool
) -> None:
    lines = []
    if not items:
        if exit_code == 0:
            lines.append("No handoff-ready reservations found.")
        elif exit_code == 22:
            lines.append("GC stopped because it could not acquire a required lock.")
        else:
            lines.append("GC stopped because state could not be read or persisted.")
    action = "would release" if preview else "released"
    count = sum(1 for item in items if item.action in {"would_release", "released"})
    held = sum(1 for item in items if item.action == "held")
    failed = sum(1 for item in items if item.action == "failed")
    lines.append(
        f"Summary: {count} {action}, {held} held, {failed} failed, {skipped} skipped"
    )
    if exit_code:
        lines.append(f"GC exited with status {exit_code}.")
    _write_lines(make_output(sys.stdout), lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    request = GcRequest(
        state_path=args.state,
        apply=not args.no_apply,
        timeout_seconds=args.timeout,
    )
    result = run_handoff_gc(request)
    if args.no_apply:
        _write_lines(
            make_output(sys.stdout),
            ["Preview only; no state, worktree, or GitHub changes were made."],
        )
    if result.items:
        _print_items(result.items, args.no_apply)
    _print_summary(result.items, result.skipped, result.exit_code, args.no_apply)
    return result.exit_code
