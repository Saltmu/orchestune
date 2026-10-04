"""status:*ラベル遷移を安全な順序で適用する共通ヘルパー。"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from orchestune.forge import Forge
from orchestune.ledger.status_machine import (
    ACTIVE_LABELS,
    ESCALATION_LABELS,
    plan_transition,
)

#: 一次status:*ラベル（他のプライマリラベルと排他的に遷移すべきもの）。
#: 中断した`transition_status_label`呼び出しの取り残しを一括除去する際に使う。
#: 役割表(`status_machine.LABEL_ROLES`)のACTIVEから、既存の明示順序で導出する。
PRIMARY_STATUS_LABELS = ACTIVE_LABELS

#: 人間の確認・手動対応を明示的に要求している終端エスカレーション状態。
#: GCなどの自動処理がこれらを検知した場合、status:queuedへの書き換えのような
#: 自動requeueでラベルを上書きしてはならない（人間の確認を経ないまま
#: 自動的に再起動されてしまうため）。
#: 役割表のESCALATIONから、既存の明示順序で導出する。
TERMINAL_ESCALATION_LABELS = ESCALATION_LABELS


def transition_status_label(
    forge: Forge,
    issue_number: int | str,
    new_label: str,
    old_labels: Iterable[str],
    on_label_added: Callable[[], None] | None = None,
) -> None:
    """新しい`status:*`ラベルを先に付与してから、指定した旧ラベルを除去する。

    #381: 「旧ラベルをremove→新ラベルをadd」の順で実装すると、この間で
    プロセスがクラッシュした場合にIssueがどの`status:*`ラベルも持たない
    状態のまま、以降のどのサイクルからも発見できなくなる（`integrator_pr.py`の
    `handle_merge_failure`が#254で対処した問題と同種）。常に新ラベルを
    先に確定させることで、途中で例外が起きてもIssueは必ず新旧いずれかの
    ラベルを持ち続ける。`new_label`と同名の`old_label`は、付与直後に
    自分自身を消してしまわないよう除去対象から除く。

    #512/PR#520レビュー16巡目対応(Codex P1): `on_label_added`が渡された場合、
    新ラベルの付与直後——旧ラベルの除去より**前**——に呼び出す。呼び出し元は
    ここでローカルの帳簿を確定させる。旧ラベルの除去だけが失敗したときに
    ローカルを未確定のまま残すと、Issueは新旧両方のラベルを持つため
    `status:in-progress`として発見され続け、次サイクル以降も同じ遷移を
    再試行しながらクオータを占有してしまう。

    #1217: 追加・除去の内容は純粋関数`plan_transition`が決め、ここはそれを
    Forgeへ適用するだけにする。`old_labels`は既存のIterableのまま1要素ずつ
    取得し、要素ごとに計画を立てて除去する——追加前の列挙や削除前の全件
    materializeをすると、ジェネレーターの評価とForge操作の交互実行、および
    例外時の呼び出し履歴が変わってしまうため。実ラベルを読んで除去対象を
    補完したり、不正な遷移を拒否したりはしない。
    """
    forge.add_label(issue_number, plan_transition(new_label, ()).add)
    if on_label_added is not None:
        on_label_added()
    for old_label in old_labels:
        for label in plan_transition(new_label, (old_label,)).remove:
            forge.remove_label(issue_number, label)


__all__ = [
    "PRIMARY_STATUS_LABELS",
    "TERMINAL_ESCALATION_LABELS",
    "transition_status_label",
]
