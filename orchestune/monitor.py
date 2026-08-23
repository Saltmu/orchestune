"""`orchestune status`: ディスパッチ済みAIセッションの状態を一覧・継続監視するCLI。"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from orchestune.forge import Forge
from orchestune.status_snapshot import build_status_snapshot, format_status_report

_CLEAR_SCREEN = "\x1b[2J\x1b[H"


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


def main(argv: list[str] | None = None, *, forge: Forge | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    label_cache: dict[int, tuple[float, tuple[str, ...]]] = {}

    if not args.watch:
        snapshot = build_status_snapshot(
            args.run_state_path,
            args.log_dir,
            time.time(),
            args.tail_lines,
            label_cache,
            forge=forge,
        )
        print(format_status_report(snapshot, time.time()))
        return 0

    try:
        while True:
            now = time.time()
            snapshot = build_status_snapshot(
                args.run_state_path,
                args.log_dir,
                now,
                args.tail_lines,
                label_cache,
                forge=forge,
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
