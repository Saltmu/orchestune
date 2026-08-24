"""#282: `status:not-needed`判定の独立検証レビューを、次回以降のディスパッチ
サイクルで検知してIssueをクローズできるよう永続化する。`integration_review_state.py`
と同じJSONベースの永続化パターンに倣う。

検証レビューはClaude Codeクラウドルーチンを非同期fireするため、Python側で結果を
同期的に受け取れない。そのため「どのIssueを、いつ検証依頼したか」をここに記録し、
後続サイクルで対象Issueのラベル
（`not-needed-review:passed`/`not-needed-review:failed`）をポーリングして
Python側が決定論的にクローズを実行する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .json_state import read_json_with_recovery, write_json_atomic


@dataclass
class PendingNotNeededReview:
    issue_number: int
    subtask_id: str
    dispatched_at: float
    session_external_id: str | None = None
    session_external_url: str | None = None


@dataclass
class NotNeededReviewState:
    pending: list[PendingNotNeededReview] = field(default_factory=list)


def load_not_needed_review_state(path: str | Path) -> NotNeededReviewState:
    data = read_json_with_recovery(path, label="not_needed_review_state.json")
    if data is None:
        return NotNeededReviewState()
    pending = [
        PendingNotNeededReview(
            issue_number=entry["issue_number"],
            subtask_id=entry["subtask_id"],
            dispatched_at=entry["dispatched_at"],
            session_external_id=entry.get("session_external_id"),
            session_external_url=entry.get("session_external_url"),
        )
        for entry in data.get("pending", [])
    ]
    return NotNeededReviewState(pending=pending)


def save_not_needed_review_state(state: NotNeededReviewState, path: str | Path) -> None:
    data = {
        "pending": [
            {
                "issue_number": entry.issue_number,
                "subtask_id": entry.subtask_id,
                "dispatched_at": entry.dispatched_at,
                "session_external_id": entry.session_external_id,
                "session_external_url": entry.session_external_url,
            }
            for entry in state.pending
        ]
    }
    write_json_atomic(path, data)
