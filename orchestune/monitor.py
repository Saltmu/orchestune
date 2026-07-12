"""`orchestune status`: ディスパッチ済みAIセッションの状態を一覧・継続監視するCLI。"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from pathlib import Path

from orchestune.dispatch_gc import is_process_alive
from orchestune.dispatch_state import load_run_state

_CLEAR_SCREEN = "\x1b[2J\x1b[H"
_TAIL_CHUNK_SIZE = 8192


@dataclass
class WorktreeStatus:
    issue_number: int
    subtask_id: str | None
    branch: str
    pid: int | None
    alive: bool | None
    started_at: float
    elapsed_seconds: float
    worktree_path: str
    external_id: str | None
    external_url: str | None
    log_tail: list[str]


def _extract_subtask_id(branch: str, issue_number: int) -> str | None:
    """ブランチ名 `claude/issue-{issue_number}-{subtask_id}` からsubtask_idを抽出する。"""
    prefix = f"claude/issue-{issue_number}-"
    if not branch.startswith(prefix):
        return None
    return branch[len(prefix) :]


def _read_log_tail(log_path: Path, n_lines: int) -> list[str]:
    """ログ末尾n_lines行を返す。ファイル全体を読み込まず、末尾からチャンク単位で
    改行がn_lines個見つかるまで逆向きに読み進める（長時間セッションの巨大ログでも
    更新のたびにI/O・メモリ使用量がファイルサイズに比例して増えないようにするため）。"""
    if n_lines <= 0:
        return []
    if not log_path.exists():
        return []

    with open(log_path, "rb") as f:
        f.seek(0, 2)
        remaining = f.tell()
        chunks: list[bytes] = []
        newline_count = 0

        while remaining > 0 and newline_count <= n_lines:
            read_size = min(_TAIL_CHUNK_SIZE, remaining)
            remaining -= read_size
            f.seek(remaining)
            chunk = f.read(read_size)
            newline_count += chunk.count(b"\n")
            chunks.append(chunk)

    data = b"".join(reversed(chunks))
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-n_lines:] if lines else []


def _format_duration(seconds: float) -> str:
    total_seconds = int(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes > 0:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def build_status_snapshot(
    run_state_path: str | Path,
    log_dir: str | Path,
    now: float,
    tail_lines: int = 3,
) -> list[WorktreeStatus]:
    log_dir = Path(log_dir)
    run_state = load_run_state(run_state_path)

    snapshot = []
    for active in run_state.active_worktrees.values():
        alive: bool | None
        if active.pid is None and active.external_id is not None:
            alive = None
        else:
            alive = is_process_alive(active.pid)

        slug = active.branch.replace("/", "-")
        log_tail = _read_log_tail(log_dir / f"{slug}.log", tail_lines)

        snapshot.append(
            WorktreeStatus(
                issue_number=active.issue_number,
                subtask_id=_extract_subtask_id(active.branch, active.issue_number),
                branch=active.branch,
                pid=active.pid,
                alive=alive,
                started_at=active.started_at,
                elapsed_seconds=now - active.started_at,
                worktree_path=active.worktree_path,
                external_id=active.external_id,
                external_url=active.external_url,
                log_tail=log_tail,
            )
        )

    snapshot.sort(key=lambda s: s.issue_number)
    return snapshot


def format_status_report(snapshot: list[WorktreeStatus], now: float) -> str:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    lines = [f"Orchestune status ({timestamp})", ""]

    if not snapshot:
        lines.append("現在アクティブなディスパッチはありません。")
        return "\n".join(lines)

    for status in snapshot:
        if status.alive is None:
            state_label = "EXTERNAL"
            target_label = (
                f"外部セッション: {status.external_id} ({status.external_url})"
            )
        elif status.alive:
            state_label = "RUNNING"
            target_label = f"PID: {status.pid}"
        else:
            state_label = "STOPPED"
            target_label = f"PID: {status.pid}"

        subtask_label = status.subtask_id or "(不明)"
        lines.append(
            f"[{state_label}] Issue #{status.issue_number} ({subtask_label}) "
            f"- {status.branch}"
        )
        lines.append(
            f"  {target_label} / 経過時間: {_format_duration(status.elapsed_seconds)}"
        )
        lines.append(f"  worktree: {status.worktree_path}")
        if status.log_tail:
            lines.append("  ログ末尾:")
            for log_line in status.log_tail:
                lines.append(f"    {log_line}")
        lines.append("")

    return "\n".join(lines)


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


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    if not args.watch:
        snapshot = build_status_snapshot(
            args.run_state_path, args.log_dir, time.time(), args.tail_lines
        )
        print(format_status_report(snapshot, time.time()))
        return 0

    try:
        while True:
            now = time.time()
            snapshot = build_status_snapshot(
                args.run_state_path, args.log_dir, now, args.tail_lines
            )
            print(_CLEAR_SCREEN, end="")
            print(format_status_report(snapshot, now))
            print(f"{args.interval}秒ごとに自動更新します。Ctrl+Cで終了してください。")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n監視を終了しました。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
