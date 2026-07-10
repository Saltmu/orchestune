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

import json
from dataclasses import dataclass, field
from pathlib import Path


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
    path = Path(path)
    if not path.exists():
        return NotNeededReviewState()
    data = json.loads(path.read_text(encoding="utf-8"))
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
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
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
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
