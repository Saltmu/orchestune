"""status:blocked-human-reviewへの共通エスカレーション処理（act）。"""

from __future__ import annotations

from orchestune import github

_REMOVABLE_STATUS_LABELS = ("status:in-progress", "status:queued", "status:blocked")


def apply_human_review_escalation(
    issue_number: int,
    current_status_labels: tuple[str, ...],
    comment: str,
) -> None:
    """現在保持しているstatus:*ラベル（in-progress/queued/blocked）を除去した上で
    status:blocked-human-reviewを付与し、理由をコメントする。

    空コミット完了・重複起動検知・CHANGES_REQUESTEDエスカレーションの3箇所で
    重複していたラベル遷移ロジックを集約したもの。`config.apply`によるゲーティング
    は呼び出し側の責務とし、この関数自体は常に無条件で実行する。
    """
    for label in _REMOVABLE_STATUS_LABELS:
        if label in current_status_labels:
            github.remove_label(issue_number, label)
    github.add_label(issue_number, "status:blocked-human-review")
    github.add_comment(issue_number, comment)
