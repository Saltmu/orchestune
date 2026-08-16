"""#437: 親branchのCAS拒否（`PARENT_BRANCH_ADVANCED`）が連続した回数を親Issue単位で
永続化する。`not_needed_review_state.py`と同じJSONベースの永続化パターンに倣う。

Integratorは`orchestune dispatch`のサイクルごとに新しいプロセスとして起動されるため、
「連続した回数」を跨サイクルで判定するにはプロセス外の永続化が必要になる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from orchestune.dispatch_worktree import file_lock
from orchestune.json_state import read_json_with_recovery, write_json_atomic


@dataclass
class StaleRetryState:
    counts: dict[str, int] = field(default_factory=dict)


def parent_branch_stale_key(parent_issue_number: int | None) -> str:
    """`counts`のキーを、親Issue番号（フラットモードでは固定キー）から導出する。"""
    return f"issue-{parent_issue_number}" if parent_issue_number is not None else "flat"


def load_stale_retry_state(path: str | Path) -> StaleRetryState:
    data = read_json_with_recovery(path, label="integrator_stale_state.json")
    if data is None:
        return StaleRetryState()
    return StaleRetryState(counts=dict(data.get("counts", {})))


def save_stale_retry_state(state: StaleRetryState, path: str | Path) -> None:
    write_json_atomic(path, {"counts": state.counts})


def record_parent_branch_stale_failure(path: str | Path, key: str) -> int:
    """陳腐化（CAS拒否）を1件記録し、更新後のそのキーの連続回数を返す。"""
    lock_path = Path(path).with_suffix(".lock")
    with file_lock(lock_path):
        state = load_stale_retry_state(path)
        state.counts[key] = state.counts.get(key, 0) + 1
        save_stale_retry_state(state, path)
        return state.counts[key]


def reset_parent_branch_stale_count(path: str | Path, key: str) -> None:
    """pushが成功した場合に、そのキーの連続陳腐化カウントをクリアする。

    記録が無い（0のまま）場合は書き込み自体を省略する。
    """
    lock_path = Path(path).with_suffix(".lock")
    with file_lock(lock_path):
        state = load_stale_retry_state(path)
        if key not in state.counts:
            return
        del state.counts[key]
        save_stale_retry_state(state, path)
