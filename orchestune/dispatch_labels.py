"""status:*ラベル遷移を安全な順序で適用する共通ヘルパー。"""

from __future__ import annotations

from collections.abc import Iterable

from orchestune.forge import Forge

#: 一次status:*ラベル（他のプライマリラベルと排他的に遷移すべきもの）。
#: 中断した`transition_status_label`呼び出しの取り残しを一括除去する際に使う。
PRIMARY_STATUS_LABELS = ("status:in-progress", "status:queued", "status:blocked")

#: 人間の確認・手動対応を明示的に要求している終端エスカレーション状態。
#: GCなどの自動処理がこれらを検知した場合、status:queuedへの書き換えのような
#: 自動requeueでラベルを上書きしてはならない（人間の確認を経ないまま
#: 自動的に再起動されてしまうため）。
TERMINAL_ESCALATION_LABELS = (
    "status:blocked-human-review",
    "status:manual-merge-required",
)


def transition_status_label(
    forge: Forge,
    issue_number: int | str,
    new_label: str,
    old_labels: Iterable[str],
) -> None:
    """新しい`status:*`ラベルを先に付与してから、指定した旧ラベルを除去する。

    #381: 「旧ラベルをremove→新ラベルをadd」の順で実装すると、この間で
    プロセスがクラッシュした場合にIssueがどの`status:*`ラベルも持たない
    状態のまま、以降のどのサイクルからも発見できなくなる（`integrator_pr.py`の
    `handle_merge_failure`が#254で対処した問題と同種）。常に新ラベルを
    先に確定させることで、途中で例外が起きてもIssueは必ず新旧いずれかの
    ラベルを持ち続ける。`new_label`と同名の`old_label`は、付与直後に
    自分自身を消してしまわないよう除去対象から除く。
    """
    forge.add_label(issue_number, new_label)
    for old_label in old_labels:
        if old_label != new_label:
            forge.remove_label(issue_number, old_label)


__all__ = ["transition_status_label"]
