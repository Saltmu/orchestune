"""`orchestune status`: ディスパッチ済みAIセッションの状態を一覧・継続監視するCLI。"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rich.cells import cell_len, chop_cells
from rich.console import Group, RenderableType

from orchestune.forge import Forge, GitHubForge
from orchestune.infra.cli_output import Block, CliOutput, make_output
from orchestune.infra.cli_progress import progress
from orchestune.operator_guidance import EvidenceState
from orchestune.parent_status import (
    ParentObservationCache,
    ParentStatusRequest,
    ParentStatusSnapshot,
    build_parent_status_snapshot,
    compact_parent_attention,
    format_parent_status_report,
)
from orchestune.status_snapshot import (
    StatusSnapshot,
    WorktreeStatus,
    build_status_snapshot,
    compact_status_attention,
    format_parent_integration,
    format_status_report,
    observe_repository_id,
)

_STATUS_HEADERS = ("State", "Issue", "Subtask", "Elapsed", "Branch")
# format_status_report starts with: timestamp, last dispatch cycle, blank line.
_REPORT_HEADING_LINES = 2
_REPORT_BODY_START = 3
_NO_DISPATCH = "現在アクティブなディスパッチはありません。"
_OVERFLOW_NOTICE = (
    "タスク詳細は表示領域に収まらないため省略しています。"
    "`orchestune status` で全体を表示できます。"
)
_PARENT_SECTION_TITLES = (
    "親 Issue サマリー",
    "稼働中セッション",
    "統合・レビュー状態",
    "次の操作案内",
)
_CHILD_MEMBERSHIP_UNKNOWN = "子 Issue の所属は不明です。"
_DISPATCH_REPORT_ERROR = (
    "--dispatch-report は --parent-issue と同時に指定し、--watch とは併用できません"
)


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
        prog="orchestune status",
        description=(
            "Show dispatched AI sessions (active_worktrees in run_state.json)."
        ),
    )
    parser.add_argument("--run-state-path", type=Path, default=Path("run_state.json"))
    parser.add_argument("--log-dir", type=Path, default=Path("logs"))
    parser.add_argument(
        "--tail-lines",
        type=_non_negative_int,
        default=3,
        help="Log lines to show per task (default 3; 0 hides the log tail).",
    )
    parser.add_argument(
        "--watch",
        "-w",
        action="store_true",
        help="Refresh the screen every interval seconds until Ctrl+C.",
    )
    parser.add_argument(
        "--interval",
        type=_positive_int,
        default=3,
        help="Refresh interval in seconds when --watch is set (default 3, integer >= 1).",
    )
    parser.add_argument(
        "-p",
        "--parent-issue",
        type=_positive_int,
        default=None,
        help=(
            "Parent Issue to show. With --dispatch-report, show that saved file. "
            "Without it, show the parent dashboard."
        ),
    )
    parser.add_argument(
        "--dispatch-report",
        type=Path,
        default=None,
        help="Result JSON saved by dispatch. Pass it together with --parent-issue.",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Dispatch report directory for the parent dashboard.",
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
    if not view.rows:
        return _rich_block(plain, [view.heading, "\n" + _NO_DISPATCH], out, footer)
    table = out.table_block(view.headers, view.rows).rich
    details = "\n" + "\n\n".join(view.details)
    full = _rich_block(plain, [view.heading, table, details], out, footer)
    if footer is None or _fits_screen(out, full.rich):
        return full
    # Live crops what does not fit. Keep the rows, attention, and footer on screen.
    return _overflow_block(
        out, plain, view.heading, table, compact_status_attention(snapshot), footer
    )


def _rich_block(
    plain: Block, parts: list[str | RenderableType], out: CliOutput, footer: str | None
) -> Block:
    renderables = [
        out.text_block(part).rich if isinstance(part, str) else part for part in parts
    ]
    if footer is not None:
        renderables.append(out.text_block("\n" + footer).rich)
    return Block(plain.plain, Group(*renderables), soft_wrap=False)


def _overflow_block(
    out: CliOutput,
    plain: Block,
    heading: str,
    table: RenderableType,
    attention: str,
    footer: str | None,
) -> Block:
    """Overflow frame without the blank lines that push the footer off a short TTY."""
    block = _packed_overflow(out, plain, heading, table, attention, footer)
    if _fits_screen(out, block.rich):
        return block
    clipped = _clip_attention(out, attention)
    if clipped == attention:
        return block
    return _packed_overflow(out, plain, heading, table, clipped, footer)


def _packed_overflow(
    out: CliOutput,
    plain: Block,
    heading: str,
    table: RenderableType,
    text: str,
    footer: str | None,
) -> Block:
    parts: list[str | RenderableType] = [heading, table, text, _OVERFLOW_NOTICE]
    if footer is not None:
        parts.append(footer)
    return _rich_block(plain, parts, out, None)


def _clip_attention(out: CliOutput, attention: str) -> str:
    console = out.console
    width = 80 if console is None else console.width
    suffix = " orchestune status"
    if cell_len(attention) <= width or cell_len(suffix) >= width:
        return attention
    lines = chop_cells(attention, width - cell_len(suffix))
    head = lines[0].rstrip() if lines else ""
    return f"{head}{suffix}"


def _fits_screen(out: CliOutput, renderable: RenderableType) -> bool:
    console = out.console
    if console is None:
        return True
    lines = console.render_lines(renderable, console.options, pad=False)
    return len(lines) < console.height


def _reject_dispatch_report_args(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """`--dispatch-report` is only valid with a parent and without `--watch`."""
    if args.dispatch_report is None:
        return
    if args.parent_issue is not None and not args.watch:
        return
    parser.error(_DISPATCH_REPORT_ERROR)


def _parent_request(args: argparse.Namespace, now: float) -> ParentStatusRequest:
    return ParentStatusRequest(
        parent_issue=args.parent_issue,
        repo_root=Path.cwd(),
        run_state_path=args.run_state_path,
        log_dir=args.log_dir,
        now=now,
        report_dir=args.report_dir,
        tail_lines=args.tail_lines,
    )


def _summary_rows(snapshot: ParentStatusSnapshot) -> tuple[tuple[str, str], ...]:
    children = snapshot.children
    if children.evidence == EvidenceState.UNAVAILABLE:
        return (("状態", children.note or _CHILD_MEMBERSHIP_UNKNOWN),)
    rows = [
        ("OPEN", str(children.open_count)),
        ("CLOSED", str(children.closed_count)),
    ]
    rows.extend((name, str(count)) for name, count in children.by_status)
    if children.other:
        rows.append(("other", str(children.other)))
    if children.note:
        rows.append(("注意", children.note))
    return tuple(rows)


def _parent_sections(report: str) -> tuple[str, ...]:
    lines = report.splitlines()
    indexes = [
        index for index, line in enumerate(lines) if line in _PARENT_SECTION_TITLES
    ]
    sections: list[str] = []
    for position, start in enumerate(indexes):
        end = indexes[position + 1] if position + 1 < len(indexes) else len(lines)
        sections.append("\n".join(lines[start:end]).rstrip())
    return tuple(sections)


def parent_status_block(
    out: CliOutput,
    snapshot: ParentStatusSnapshot,
    now: float,
    footer: str | None = None,
) -> Block:
    """Parent dashboard: the plain report, or tables plus the same sections."""
    report = format_parent_status_report(snapshot, now)
    plain = out.text_block(report if footer is None else f"{report}\n{footer}")
    if not out.policy.rich:
        return plain
    heading = report.splitlines()[0]
    summary = out.table_block(
        ("項目", "値"), _summary_rows(snapshot), title="親 Issue サマリー"
    ).rich
    parts: list[str | RenderableType] = [heading, summary]
    if snapshot.sessions.worktrees:
        rows = tuple(_status_row(status) for status in snapshot.sessions.worktrees)
        parts.append(
            out.table_block(_STATUS_HEADERS, rows, title="稼働中セッション").rich
        )
    parts.extend(_parent_sections(report))
    full = _rich_block(plain, parts, out, footer)
    if footer is None or _fits_screen(out, full.rich):
        return full
    return _overflow_block(
        out, plain, heading, summary, compact_parent_attention(snapshot), footer
    )


def _emit_parent_dashboard(
    args: argparse.Namespace,
    out: CliOutput,
    forge: Forge | None,
    clock: Callable[[], float] | None,
    sleep: Callable[[float], None] | None,
) -> int:
    """Show the parent dashboard once, or until Ctrl+C when `--watch` is set."""
    now_fn = clock if clock is not None else time.time
    sleep_fn = sleep if sleep is not None else time.sleep
    resolved = forge if forge is not None else GitHubForge()
    label_cache: dict[int, tuple[float, tuple[str, ...]]] = {}
    cache = ParentObservationCache()

    def take_snapshot(now: float) -> ParentStatusSnapshot:
        return build_parent_status_snapshot(
            _parent_request(args, now),
            resolved,
            cache=cache,
            label_cache=label_cache,
        )

    if not args.watch:
        now = now_fn()
        out.emit(parent_status_block(out, take_snapshot(now), now))
        return 0
    footer = f"{args.interval}秒ごとに自動更新します。Ctrl+Cで終了してください。"
    try:
        with progress(out) as handle:
            while True:
                now = now_fn()
                handle.update(parent_status_block(out, take_snapshot(now), now, footer))
                sleep_fn(args.interval)
    except KeyboardInterrupt:
        out.emit(out.text_block("\n監視を終了しました。"))
    return 0


def _emit_parent_integration(args: argparse.Namespace, out: CliOutput) -> int:
    """Print one saved parent observation. This path does not write or launch work."""
    path = args.dispatch_report
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        payload = None
    text = format_parent_integration(
        payload,
        parent_issue=args.parent_issue,
        repository_id=observe_repository_id(Path.cwd()),
        evidence_path=str(path),
    )
    out.emit(out.text_block(text))
    return 0


def main(
    argv: list[str] | None = None,
    *,
    forge: Forge | None = None,
    output: CliOutput | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    _reject_dispatch_report_args(parser, args)
    out = output if output is not None else make_output()
    if args.dispatch_report is not None:
        return _emit_parent_integration(args, out)
    if args.parent_issue is not None:
        return _emit_parent_dashboard(args, out, forge, clock, sleep)
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
        with progress(out) as handle:
            while True:
                now = now_fn()
                handle.update(status_block(out, take_snapshot(now), now, footer))
                sleep_fn(args.interval)
    except KeyboardInterrupt:
        out.emit(out.text_block("\n監視を終了しました。"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
