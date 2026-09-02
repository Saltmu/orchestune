"""ディスパッチ済みAIセッションのステータス集計ドメインロジック。

`run_state.json`のactive_worktreesとGitHubラベルを突き合わせ、`orchestune
status`（`monitor.py`）が表示する`StatusSnapshot`を構築する。CLI配線
（引数解析・`--watch`ループ・画面出力）とは独立して単体テストできるよう、
`dispatch_state`/`forge`/`process_utils`のみに依存する自己完結したモジュール
として切り出している。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from orchestune.branch_naming import parse_task_branch_name
from orchestune.dispatch.state import load_run_state
from orchestune.forge import Forge, GitHubForge
from orchestune.infra.process_utils import is_process_alive
from orchestune.labels import StatusLabel

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
    (StatusLabel.DONE, MonitorState.DONE),
    (StatusLabel.BLOCKED_HUMAN_REVIEW, MonitorState.BLOCKED_HUMAN_REVIEW),
    (StatusLabel.NOT_NEEDED, MonitorState.NOT_NEEDED),
    (StatusLabel.MANUAL_MERGE_REQUIRED, MonitorState.MANUAL_MERGE_REQUIRED),
    (StatusLabel.BLOCKED, MonitorState.BLOCKED),
    (StatusLabel.EXTERNAL_LOCK, MonitorState.EXTERNAL_LOCK),
    (StatusLabel.QUEUED, MonitorState.QUEUED),
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

    if StatusLabel.IN_PROGRESS in labels:
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
    forge: Forge | None = None,
) -> tuple[str, ...] | None:
    """GitHub APIレート制限を避けるためTTLキャッシュ経由でラベルを取得する。

    `gh`呼び出しが失敗した場合は、statusコマンドをクラッシュさせず、
    stale（期限切れ）でもキャッシュがあればそれを返し、無ければNoneを返して
    呼び出し側にPID生存ベースへのフォールバックを委ねる。
    """
    cached = cache.get(issue_number)
    if cached is not None and now - cached[0] < ttl:
        return cached[1]

    forge = forge or GitHubForge()
    try:
        labels = forge.get_issue_labels(issue_number)
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
    started_at: float | None
    elapsed_seconds: float | None
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
    """ブランチ名 `<prefix>/issue-{issue_number}-{subtask_id}` からsubtask_idを
    抽出する（#777: prefixは特定ツール名に固定しない）。"""
    parsed = parse_task_branch_name(branch)
    if parsed is None or parsed.issue_number != issue_number:
        return None
    return parsed.subtask_id


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
    forge: Forge | None = None,
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

        labels = _fetch_labels_cached(
            active.issue_number, label_cache, now, forge=forge
        )
        state = _derive_monitor_state(labels, alive, active.external_id)

        worktrees.append(
            WorktreeStatus(
                issue_number=active.issue_number,
                subtask_id=_extract_subtask_id(active.branch, active.issue_number),
                branch=active.branch,
                pid=active.pid,
                alive=alive,
                started_at=active.started_at,
                elapsed_seconds=(
                    now - active.started_at if active.started_at is not None else None
                ),
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
        elapsed_label = (
            _format_duration(status.elapsed_seconds)
            if status.elapsed_seconds is not None
            else "不明（自己修復により開始時刻を復元できません）"
        )
        lines.append(f"  {target_label} / 経過時間: {elapsed_label}")
        lines.append(f"  worktree: {status.worktree_path}")
        if status.log_tail:
            lines.append("  ログ末尾:")
            for log_line in status.log_tail:
                lines.append(f"    {log_line}")
        lines.append("")

    return "\n".join(lines)
