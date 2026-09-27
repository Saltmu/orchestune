"""Shared human-review escalation action used by dispatch and Integrator."""

from __future__ import annotations

from collections.abc import Callable

from orchestune.forge import Forge, GitHubForge
from orchestune.labels import StatusLabel
from orchestune.ledger.status_labels import transition_status_label

# #511: `status:not-needed`（対応不要）検証レビューのタイムアウト時にも
# この共通処理を再利用するため対象へ含める。既存の呼び出し元（GC/actor検証/
# CHANGES_REQUESTED）はいずれも`status:in-progress`/`queued`/`blocked`の
# タスクにしか作用しないため、`status:not-needed`が`current_status_labels`に
# 含まれることはなく、この拡張は既存呼び出し元には影響しない。
_REMOVABLE_STATUS_LABELS = (
    StatusLabel.IN_PROGRESS,
    StatusLabel.QUEUED,
    StatusLabel.BLOCKED,
    StatusLabel.NOT_NEEDED,
)


def apply_human_review_escalation(
    issue_number: int,
    current_status_labels: tuple[str, ...],
    comment: str,
    forge: Forge | None = None,
    on_label_applied: Callable[[], None] | None = None,
) -> None:
    """現在保持しているstatus:*ラベル（in-progress/queued/blocked）を除去した上で
    status:blocked-human-reviewを付与し、理由をコメントする。

    空コミット完了・重複起動検知・CHANGES_REQUESTEDエスカレーションの3箇所で
    重複していたラベル遷移ロジックを集約したもの。`config.apply`によるゲーティング
    は呼び出し側の責務とし、この関数自体は常に無条件で実行する。

    #512/PR#520レビュー12巡目対応(Codex P1): `on_label_applied`が渡された場合、
    `status:blocked-human-review`が付いた瞬間——旧ラベルの除去やコメント投稿より
    **前**——に呼び出す（16巡目対応: 呼び出し位置を`transition_status_label`の
    内部へ移動）。GC回収の呼び出し元はここでローカルの帳簿を確定させる:
    旧ラベルの除去やコメント投稿だけが失敗したときにローカルを未確定のまま残すと、
    GitHub側は既に終端ラベルを持っているのに次サイクルもエスカレーションを
    再試行し続け、帳簿エントリがクオータを占有し続けてしまう。
    """
    forge = forge or GitHubForge()
    transition_status_label(
        forge,
        issue_number,
        StatusLabel.BLOCKED_HUMAN_REVIEW,
        (label for label in _REMOVABLE_STATUS_LABELS if label in current_status_labels),
        # #512/PR#520レビュー16巡目対応(Codex P1): 旧ラベルの除去より前、
        # status:blocked-human-reviewが付いた瞬間に確定させる。
        on_label_added=on_label_applied,
    )
    forge.add_comment(issue_number, comment)
