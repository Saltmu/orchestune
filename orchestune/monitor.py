"""`orchestune status`: ディスパッチ済みAIセッションの状態を一覧・継続監視するCLI。"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from orchestune import github
from orchestune.dispatch_gc import is_process_alive
from orchestune.dispatch_state import load_run_state

_CLEAR_SCREEN = "\x1b[2J\x1b[H"
_TAIL_CHUNK_SIZE = 8192
_LABEL_CACHE_TTL_SECONDS = 15.0


class MonitorState(str, Enum):
    """#137: dispatch cycleの状態遷移（docs/ja/status-labels.md）に追従した表示状態。"""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    EXTERNAL = "EXTERNAL"
    PROCESS_EXITED = "PROCESS_EXITED"
    DONE = "DONE"
    BLOCKED_HUMAN_REVIEW = "BLOCKED_HUMAN_REVIEW"
    NOT_NEEDED = "NOT_NEEDED"
    EXTERNAL_LOCK = "EXTERNAL_LOCK"
    BLOCKED = "BLOCKED"
    MANUAL_MERGE_REQUIRED = "MANUAL_MERGE_REQUIRED"
    UNKNOWN = "UNKNOWN"


_STATE_DESCRIPTIONS: dict[MonitorState, str] = {
    MonitorState.QUEUED: "GCにより再キュー済み。次回dispatch cycleでの起動待ち",
    MonitorState.RUNNING: "workerは実行中",
    MonitorState.EXTERNAL: "外部workerは実行中または結果待ち",
    MonitorState.PROCESS_EXITED: "worker終了を検知。次回dispatch cycleで状態反映予定",
    MonitorState.DONE: "dispatchにより完了済み",
    MonitorState.BLOCKED_HUMAN_REVIEW: "dispatchによりhuman reviewへ遷移済み",
    MonitorState.NOT_NEEDED: "対応不要と判定済み",
    MonitorState.EXTERNAL_LOCK: "外部ブランチとのfootprint競合によりロック中",
    MonitorState.BLOCKED: "依存関係未解決によりブロック中",
    MonitorState.MANUAL_MERGE_REQUIRED: "自動リベース失敗により手動マージが必要",
    MonitorState.UNKNOWN: "ラベル構成が未知のため状態を判定できません",
}

# docs/ja/status-labels.md の遷移表に基づく優先順位。上位ほど「run_state側の
# 帳簿がstaleである（GitHubラベルは既に次の状態へ進んでいる）」ことを示す。
_LABEL_PRIORITY: tuple[tuple[str, MonitorState], ...] = (
    ("status:done", MonitorState.DONE),
    ("status:blocked-human-review", MonitorState.BLOCKED_HUMAN_REVIEW),
    ("status:not-needed", MonitorState.NOT_NEEDED),
    ("status:manual-merge-required", MonitorState.MANUAL_MERGE_REQUIRED),
    ("status:blocked", MonitorState.BLOCKED),
    ("status:external-lock", MonitorState.EXTERNAL_LOCK),
    ("status:queued", MonitorState.QUEUED),
)


def _derive_monitor_state(
    labels: tuple[str, ...] | None,
    alive: bool | None,
    external_id: str | None,
) -> MonitorState:
    """GitHubラベルを正として表示状態を導出する（decide、副作用なし）。

    labelsがNone（ラベル取得失敗）の場合は、従来通りPID生存ベースの分類に
    フォールバックする。
    """
    if labels is None:
        if alive is None:
            return MonitorState.EXTERNAL
        return MonitorState.RUNNING if alive else MonitorState.PROCESS_EXITED

    for label, state in _LABEL_PRIORITY:
        if label in labels:
            return state

    if "status:in-progress" in labels:
        if alive:
            return MonitorState.RUNNING
        if external_id is not None:
            return MonitorState.EXTERNAL
        return MonitorState.PROCESS_EXITED

    return MonitorState.UNKNOWN


def _fetch_labels_cached(
    issue_number: int,
    cache: dict[int, tuple[float, tuple[str, ...]]],
    now: float,
    ttl: float = _LABEL_CACHE_TTL_SECONDS,
) -> tuple[str, ...] | None:
    """GitHub APIレート制限を避けるためTTLキャッシュ経由でラベルを取得する。

    `gh`呼び出しが失敗した場合は、statusコマンドをクラッシュさせず、
    stale（期限切れ）でもキャッシュがあればそれを返し、無ければNoneを返して
    呼び出し側にPID生存ベースへのフォールバックを委ねる。
    """
    cached = cache.get(issue_number)
    if cached is not None and now - cached[0] < ttl:
        return cached[1]

    try:
        labels = github.get_issue_labels(issue_number)
    except Exception:
        return cached[1] if cached is not None else None

    cache[issue_number] = (now, labels)
    return labels


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
    state: MonitorState
    labels_fetch_failed: bool = False


@dataclass
class StatusSnapshot:
    worktrees: list[WorktreeStatus]
    last_reconciled_at: float | None


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
    label_cache: dict[int, tuple[float, tuple[str, ...]]] | None = None,
) -> StatusSnapshot:
    log_dir = Path(log_dir)
    run_state = load_run_state(run_state_path)
    if label_cache is None:
        label_cache = {}

    worktrees = []
    for active in run_state.active_worktrees.values():
        alive: bool | None
        if active.pid is None and active.external_id is not None:
            alive = None
        else:
            alive = is_process_alive(active.pid)

        slug = active.branch.replace("/", "-")
        log_tail = _read_log_tail(log_dir / f"{slug}.log", tail_lines)

        labels = _fetch_labels_cached(active.issue_number, label_cache, now)
        state = _derive_monitor_state(labels, alive, active.external_id)

        worktrees.append(
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
                state=state,
                labels_fetch_failed=labels is None,
            )
        )

    worktrees.sort(key=lambda s: s.issue_number)
    return StatusSnapshot(
        worktrees=worktrees, last_reconciled_at=run_state.last_reconciled_at
    )


def _format_last_reconciled(last_reconciled_at: float | None, now: float) -> str:
    if last_reconciled_at is None:
        return "最終dispatchサイクル: (未記録)"
    return f"最終dispatchサイクル: {_format_duration(now - last_reconciled_at)}前"


def format_status_report(snapshot: StatusSnapshot, now: float) -> str:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    lines = [
        f"Orchestune status ({timestamp})",
        _format_last_reconciled(snapshot.last_reconciled_at, now),
        "",
    ]

    if not snapshot.worktrees:
        lines.append("現在アクティブなディスパッチはありません。")
        return "\n".join(lines)

    for status in snapshot.worktrees:
        if status.external_id is not None:
            target_label = (
                f"外部セッション: {status.external_id} ({status.external_url})"
            )
        else:
            target_label = f"PID: {status.pid}"

        subtask_label = status.subtask_id or "(不明)"
        lines.append(
            f"[{status.state.value}] Issue #{status.issue_number} ({subtask_label}) "
            f"- {status.branch}"
        )
        lines.append(f"  {_STATE_DESCRIPTIONS[status.state]}")
        if status.labels_fetch_failed:
            lines.append("  (ラベル取得失敗。PID生存状態のみで判定しています)")
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
    label_cache: dict[int, tuple[float, tuple[str, ...]]] = {}

    if not args.watch:
        snapshot = build_status_snapshot(
            args.run_state_path, args.log_dir, time.time(), args.tail_lines, label_cache
        )
        print(format_status_report(snapshot, time.time()))
        return 0

    try:
        while True:
            now = time.time()
            snapshot = build_status_snapshot(
                args.run_state_path, args.log_dir, now, args.tail_lines, label_cache
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
