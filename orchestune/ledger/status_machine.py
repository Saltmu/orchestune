"""`status:*`ラベルの役割表と遷移計画を表す純粋モジュール。

Forgeに依存せず、ラベルの読み取り・書き込みやコールバックを行わない。
ここで定義するのは「どのラベルがどの役割を持つか」「通常の遷移の方針」
「スナップショットから導く追加・除去の計画」だけで、計画の適用
(`orchestune.ledger.status_labels.transition_status_label`)とは分離している。

`ALLOWED_TRANSITIONS`は通常のlifecycle遷移の方針を表す表であり、本番では不正
遷移を拒否しない。人間確認ラベルの保護は既存の呼び出し側の判断に残る。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from orchestune.labels import StatusLabel


class LabelRole(StrEnum):
    """`status:*`ラベルが担う役割。"""

    ACTIVE = "active"
    ESCALATION = "escalation"
    FINAL = "final"
    AUXILIARY = "auxiliary"


#: 全`StatusLabel`の役割。補助ラベルはlifecycleと共存できる。
LABEL_ROLES: Mapping[StatusLabel, LabelRole] = MappingProxyType(
    {
        StatusLabel.QUEUED: LabelRole.ACTIVE,
        StatusLabel.BLOCKED: LabelRole.ACTIVE,
        StatusLabel.IN_PROGRESS: LabelRole.ACTIVE,
        StatusLabel.BLOCKED_HUMAN_REVIEW: LabelRole.ESCALATION,
        StatusLabel.MANUAL_MERGE_REQUIRED: LabelRole.ESCALATION,
        StatusLabel.DONE: LabelRole.FINAL,
        StatusLabel.NOT_NEEDED: LabelRole.FINAL,
        StatusLabel.BLOCKED_RECOMPUTE: LabelRole.AUXILIARY,
        StatusLabel.FORCE_SERIAL: LabelRole.AUXILIARY,
        StatusLabel.EXTERNAL_LOCK: LabelRole.AUXILIARY,
    }
)

#: 単一性の検証対象となる役割（done / not-neededを含む7種類）。
LIFECYCLE_ROLES = frozenset({LabelRole.ACTIVE, LabelRole.ESCALATION, LabelRole.FINAL})


def _ordered_labels(
    role: LabelRole, order: tuple[StatusLabel, ...]
) -> tuple[StatusLabel, ...]:
    """役割表から導出した集合を、既存の明示順序で並べて返す。

    列挙型やMappingの走査順に依存しない。`order`が役割表と食い違う場合は
    import時に失敗させ、役割表と公開定数のずれを黙って許さない。
    """
    declared = {
        label for label, label_role in LABEL_ROLES.items() if label_role is role
    }
    if set(order) != declared or len(order) != len(declared):
        raise ValueError(
            f"explicit order for {role.value} labels drifted from LABEL_ROLES"
        )
    return order


#: ACTIVEラベル（既存の`PRIMARY_STATUS_LABELS`の順序: in-progress / queued / blocked）。
ACTIVE_LABELS = _ordered_labels(
    LabelRole.ACTIVE,
    (StatusLabel.IN_PROGRESS, StatusLabel.QUEUED, StatusLabel.BLOCKED),
)

#: ESCALATIONラベル（既存の`TERMINAL_ESCALATION_LABELS`の順序）。
ESCALATION_LABELS = _ordered_labels(
    LabelRole.ESCALATION,
    (StatusLabel.BLOCKED_HUMAN_REVIEW, StatusLabel.MANUAL_MERGE_REQUIRED),
)

_LIFECYCLE_LABELS = frozenset(
    label for label, role in LABEL_ROLES.items() if role in LIFECYCLE_ROLES
)


def lifecycle_labels(labels: Iterable[str]) -> frozenset[StatusLabel]:
    """渡されたラベルのうち、既知のlifecycleラベルだけを集合で返す。

    未知のラベルや補助ラベルはlifecycleに含めない。
    """
    present = set(labels)
    return frozenset(label for label in _LIFECYCLE_LABELS if label in present)


def status_repair_preserves_protection(
    labels: Iterable[str], target: str | None
) -> bool:
    """Permit safe cardinality repair, including the interrupted merge rollback.

    Only the exact done+queued pair may remove a protected lifecycle. Auxiliary
    labels do not participate. This predicate governs repair, not normal policy.
    """
    primary = lifecycle_labels(labels)
    protected = {
        label
        for label in primary
        if LABEL_ROLES[label] in {LabelRole.FINAL, LabelRole.ESCALATION}
    }
    if target == StatusLabel.QUEUED and primary == {
        StatusLabel.DONE,
        StatusLabel.QUEUED,
    }:
        return True
    return not protected or protected == {target}


def _build_allowed_transitions() -> frozenset[tuple[StatusLabel, StatusLabel]]:
    active = (StatusLabel.QUEUED, StatusLabel.BLOCKED, StatusLabel.IN_PROGRESS)
    pairs: set[tuple[StatusLabel, StatusLabel]] = {
        # claim / 起動成功。
        (StatusLabel.QUEUED, StatusLabel.IN_PROGRESS),
        (StatusLabel.BLOCKED, StatusLabel.IN_PROGRESS),
        # 依存の解決・未解決、起動失敗、GC回収による再投入。
        (StatusLabel.BLOCKED, StatusLabel.QUEUED),
        (StatusLabel.QUEUED, StatusLabel.BLOCKED),
        (StatusLabel.IN_PROGRESS, StatusLabel.QUEUED),
        (StatusLabel.IN_PROGRESS, StatusLabel.BLOCKED),
        # 人間確認へのエスカレーション。not-neededは検証タイムアウトが由来。
        (StatusLabel.NOT_NEEDED, StatusLabel.BLOCKED_HUMAN_REVIEW),
        # 自動rebase失敗。
        (StatusLabel.IN_PROGRESS, StatusLabel.MANUAL_MERGE_REQUIRED),
    }
    for source in active:
        pairs.add((source, StatusLabel.BLOCKED_HUMAN_REVIEW))
        pairs.add((source, StatusLabel.DONE))
        pairs.add((source, StatusLabel.NOT_NEEDED))
    # 同一操作の再実行は明示的な自己遷移として許可する。
    pairs.update((label, label) for label in _LIFECYCLE_LABELS)
    return frozenset(pairs)


#: 通常のlifecycle遷移の方針（source, target）。初期化・修復・補助ラベルの操作は含まない。
ALLOWED_TRANSITIONS: frozenset[tuple[StatusLabel, StatusLabel]] = (
    _build_allowed_transitions()
)


def is_allowed(source: str, target: str) -> bool:
    """`source`から`target`への通常のlifecycle遷移が方針上許可されているか。"""
    return (source, target) in ALLOWED_TRANSITIONS


@dataclass(frozen=True, slots=True)
class TransitionPlan:
    """スナップショットから導いた、ラベルの追加と除去の計画。"""

    add: str
    remove: tuple[str, ...]


def plan_transition(new_label: str, old_labels: tuple[str, ...]) -> TransitionPlan:
    """`new_label`の付与と`old_labels`の除去を計画する。

    `remove`は入力の順序と重複を保ち、`new_label`と等しい要素だけを除く。
    空の旧ラベル列や未知の文字列も受け付け、ソート・重複排除・実ラベルの取得・
    許可遷移の強制は行わない。
    """
    return TransitionPlan(
        add=new_label,
        remove=tuple(label for label in old_labels if label != new_label),
    )


__all__ = [
    "ACTIVE_LABELS",
    "ALLOWED_TRANSITIONS",
    "ESCALATION_LABELS",
    "LABEL_ROLES",
    "LIFECYCLE_ROLES",
    "LabelRole",
    "TransitionPlan",
    "is_allowed",
    "lifecycle_labels",
    "plan_transition",
    "status_repair_preserves_protection",
]
