"""claim.preflight と dispatch.locks が共有する外部ロック契約(DTO・定数)。"""

from __future__ import annotations

from dataclasses import dataclass, field

from orchestune.task_metadata import TaskMetadata

KIND_BRANCH = "branch"
KIND_PR = "pr"


@dataclass(frozen=True)
class CompletedDependencyBranchEvidence:
    """One scan's observed canonical tip contained in the dependent launch base.

    This is neither a completion label nor historical merged-PR evidence. It is
    passed per (dependent issue, dependency issue), never persisted across scans.
    """

    branch_name: str
    base_ref: str
    head_sha: str


@dataclass(frozen=True)
class ExternalLockConflict:
    """#787: 外部ロック1件分の理由。運用者が「なぜ起動しないのか」を追える最小単位。

    `files`は`branch`/`pr`種別でのみ埋まる。差分を取得できなかったブランチや
    changed filesが打ち切られたPRはfail closedでロックするため衝突ファイルを
    特定できず、種別だけで理由を表す。"""

    kind: str
    source: str
    files: tuple[str, ...] = ()


@dataclass
class ExternalLockScanResult:
    to_lock: list[TaskMetadata]
    to_unlock: list[TaskMetadata]
    # #787: 新規ロック(to_lock)だけでなく「前サイクルから継続してロック中の
    # タスク」も収録する。継続ロックはto_lock/to_unlockのどちらにも現れず、
    # 理由を引ける場所が他に無いため(#695の実例)。
    conflicts: dict[int, tuple[ExternalLockConflict, ...]] = field(default_factory=dict)
