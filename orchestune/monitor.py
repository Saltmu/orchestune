"""`orchestune status`: ディスパッチ済みAIセッションの状態を一覧・継続監視するCLI。"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rich.console import Group

from orchestune.forge import Forge
from orchestune.infra.cli_output import Block, CliOutput, make_output
from orchestune.infra.cli_progress import progress
from orchestune.status_snapshot import (
    StatusSnapshot,
    WorktreeStatus,
    build_status_snapshot,
    format_status_report,
)

_STATUS_HEADERS = ("State", "Issue", "Subtask", "Elapsed", "Branch")
# format_status_report starts with: timestamp, last dispatch cycle, blank line.
_REPORT_HEADING_LINES = 2
_REPORT_BODY_START = 3
_NO_DISPATCH = "現在アクティブなディスパッチはありません。"


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from exc
    if result < 1:
        raise argparse.ArgumentTypeError(f"{value!r} must be a positive integer (>= 1)")
    return result


def _non_negative_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from exc
    if result < 0:
        raise argparse.ArgumentTypeError(
            f"{value!r} must be a non-negative integer (>= 0)"
        )
    return result


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ディスパッチ済みAIセッション（run_state.jsonのactive_worktrees）の状態を表示する"
    )
    parser.add_argument("--run-state-path", type=Path, default=Path("run_state.json"))
    parser.add_argument("--log-dir", type=Path, default=Path("logs"))
    parser.add_argument(
        "--tail-lines",
        type=_non_negative_int,
        default=3,
        help="タスクごとに表示するログ末尾の行数（既定3行、0を指定するとログ末尾を表示しない）",
    )
    parser.add_argument(
        "--watch",
        "-w",
        action="store_true",
        help="指定するとinterval秒おきに画面を自動更新し続ける（Ctrl+Cで終了）",
    )
    parser.add_argument(
        "--interval",
        type=_positive_int,
        default=3,
        help="--watch指定時の自動更新間隔（秒、既定3秒、1以上の整数）",
    )
    return parser


@dataclass(frozen=True)
class StatusView:
    """Strings for the decorated (TTY) status display; no Rich types."""

    heading: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    details: tuple[str, ...]


def _elapsed_label(status: WorktreeStatus) -> str:
    if status.elapsed_seconds is None:
        return "不明"
    hours, remainder = divmod(int(status.elapsed_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    if minutes > 0:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def _status_row(status: WorktreeStatus) -> tuple[str, ...]:
    return (
        status.state.value,
        f"#{status.issue_number}",
        status.subtask_id or "(不明)",
        _elapsed_label(status),
        status.branch,
    )


def present_status(snapshot: StatusSnapshot, now: float) -> StatusView:
    """Turn a snapshot into table rows plus per-task detail text.

    Heading and details are cut from the plain report (one task at a time), so the
    decorated view can never drop a line the plain report shows.
    """

    def report_lines(worktrees: list[WorktreeStatus]) -> list[str]:
        report = StatusSnapshot(worktrees, snapshot.last_reconciled_at)
        return format_status_report(report, now).splitlines()

    heading = "\n".join(report_lines([])[:_REPORT_HEADING_LINES])
    details = tuple(
        "\n".join(report_lines([status])[_REPORT_BODY_START:]).rstrip()
        for status in snapshot.worktrees
    )
    return StatusView(
        heading=heading,
        headers=_STATUS_HEADERS,
        rows=tuple(_status_row(s) for s in snapshot.worktrees),
        details=details,
    )


def status_block(
    out: CliOutput, snapshot: StatusSnapshot, now: float, footer: str | None = None
) -> Block:
    """The status display: the existing report when plain, a table view on a TTY."""
    report = format_status_report(snapshot, now)
    plain = out.text_block(report if footer is None else f"{report}\n{footer}")
    if not out.policy.rich:
        return plain
    view = present_status(snapshot, now)
    parts = [out.text_block(view.heading).rich]
    if view.rows:
        parts.append(out.table_block(view.headers, view.rows).rich)
        parts.append(out.text_block("\n" + "\n\n".join(view.details)).rich)
    else:
        parts.append(out.text_block("\n" + _NO_DISPATCH).rich)
    if footer is not None:
        parts.append(out.text_block("\n" + footer).rich)
    return Block(plain.plain, Group(*parts), soft_wrap=False)


def main(
    argv: list[str] | None = None,
    *,
    forge: Forge | None = None,
    output: CliOutput | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    args = _build_arg_parser().parse_args(argv)
    out = output if output is not None else make_output()
    now_fn = clock if clock is not None else time.time
    sleep_fn = sleep if sleep is not None else time.sleep
    label_cache: dict[int, tuple[float, tuple[str, ...]]] = {}

    def take_snapshot(now: float) -> StatusSnapshot:
        return build_status_snapshot(
            args.run_state_path,
            args.log_dir,
            now,
            args.tail_lines,
            label_cache,
            forge=forge,
        )

    if not args.watch:
        now = now_fn()
        out.emit(status_block(out, take_snapshot(now), now))
        return 0

    footer = f"{args.interval}秒ごとに自動更新します。Ctrl+Cで終了してください。"
    try:
        with progress(out, spinner=False) as handle:
            while True:
                now = now_fn()
                handle.update(status_block(out, take_snapshot(now), now, footer))
                sleep_fn(args.interval)
    except KeyboardInterrupt:
        out.emit(out.text_block("\n監視を終了しました。"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
