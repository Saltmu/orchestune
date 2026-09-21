"""#935/#943: worktreeの所有権マーカー（sidecarファイル）の読み書きヘルパー。

`orchestune.dispatch.worktree`（マーカーの発行・確認）と
`orchestune.dispatch.gc.git`（撤去時のマーカー後始末）の双方から参照される
ため、循環import（`worktree`は`gc`パッケージを、`gc.git`は`worktree`を
importする形）を避けて独立モジュールに切り出す。
`tests/test_architecture.py`の循環import検知および関数内隠しimport検知の
両方が、この分離を要求している。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def claim_marker_path(worktree_path: Path) -> Path:
    """#935: worktree本体の外側（sibling）に置く所有権マーカーのパス。

    worktree内部に置くと`git status --porcelain`にuntrackedとして現れ、
    所有権確認用のファイル自体がdirty判定を汚染してしまうため、常に
    worktree_path.parent側に置く。"""
    return worktree_path.parent / f"{worktree_path.name}.claim.json"


def read_claim_marker(worktree_path: Path) -> dict[str, Any] | None:
    try:
        raw = claim_marker_path(worktree_path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        decoded = json.loads(raw)
    except ValueError:
        return None
    # #935レビュー対応(P2, round4): 破損・手動編集されたマーカーが`[]`や
    # `"claim"`のような構文的に妥当な非オブジェクトJSONだった場合、
    # 呼び出し側の`marker.get(...)`がAttributeErrorで落ちる。所有権を
    # 確認できないマーカーとしてfail-closedにNoneを返す。
    if not isinstance(decoded, dict):
        return None
    return decoded


def write_claim_marker(
    worktree_path: Path,
    *,
    claim_id: str,
    branch: str,
    base_sha: str | None,
    branch_created: bool,
) -> None:
    marker_path = claim_marker_path(worktree_path)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps(
            {
                "claim_id": claim_id,
                "branch": branch,
                "base_sha": base_sha,
                "branch_created": branch_created,
            }
        ),
        encoding="utf-8",
    )


def remove_claim_marker(worktree_path: Path) -> None:
    claim_marker_path(worktree_path).unlink(missing_ok=True)


def claim_lock_path(worktree_path: Path) -> Path:
    """#935レビュー対応(P1): 同一branch/worktreeに対する`prepare_task_worktree`と
    `rollback_task_worktree`を相互排他にするロックファイル。forceによる奪取
    （worktree再作成→旧マーカー無効化）の途中状態を、並行するrollbackが
    「旧claim_idがまだ有効」として観測し、奪取直後のworktreeを削除してしまう
    TOCTOUを防ぐ。"""
    return claim_marker_path(worktree_path).with_suffix(".lock")
