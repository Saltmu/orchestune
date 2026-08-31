"""ディスパッチャー全体の設定（DispatcherConfig）。

act側モジュール（dispatch_gc/dispatch_rebase/dispatch_escalation等）が
dispatch_rules.pyのRule/CycleContext経由でこの設定型を参照する際に、
dispatch_cycle.py経由の循環importを避けるため独立モジュールとして切り出す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from orchestune.consistency.invariants.status import (
    BLOCKED_WITH_RESOLVED_DEPENDENCIES,
    PRIMARY_STATUS_CONFLICT,
)
from orchestune.consistency.repairs.execution import (
    COMMAND_BOOKKEEPING,
    COMMAND_RECLAIM,
    COMMAND_REQUEUE,
)
from orchestune.consistency.supervisor import MAX_REPAIR_PASSES, ConsistencyMode
from orchestune.dag.similarity import DEFAULT_SIMILARITY_THRESHOLD
from orchestune.dispatch.execution_profiles import ExecutionProfileConfig
from orchestune.dispatch.targets import DispatchTarget, LocalProcessDispatchTarget
from orchestune.forge import Forge, GitHubForge

DEFAULT_SELF_HEALING_REPAIR_ALLOWLIST = frozenset(
    {
        BLOCKED_WITH_RESOLVED_DEPENDENCIES,
        PRIMARY_STATUS_CONFLICT,
        COMMAND_BOOKKEEPING,
        COMMAND_RECLAIM,
        COMMAND_REQUEUE,
    }
)


@dataclass
class DispatcherConfig:
    max_concurrent: int = 2
    max_launches_per_window: int = 1
    window_seconds: int = 3600
    run_state_path: Path = Path("run_state.json")
    worktree_root: Path = Path("worktrees")
    log_dir: Path = Path("logs")
    events_log_path: Path = Path("events.jsonl")
    parent_issue_number: int | None = None
    apply: bool = False
    dispatch_target: DispatchTarget | None = None
    forge: Forge | None = None
    deviation_buffer_lines: int = 5
    max_recompute_retries: int = 2
    task_timeout_seconds: int = 0
    zombie_gc: bool = True
    # #512: ゾンビ/タイムアウトGCが同一タスクを`status:queued`へ差し戻せる回数の上限。
    # 超過したタスクは`status:blocked-human-review`へ遷移し、再投入されなくなる。
    # 「無制限」を表す値は用意しない（終端のない経路を作らないため）。`0`は
    # 「1回目の回収で即エスカレーション」を意味する。
    max_task_reclaims: int = 3
    # #675: 起動直後（コミット作成前）にプロセスが終了した場合だけ、一時的な
    # API/ストリーム障害として再投入する。回数はRunStateに永続化し、無限再試行を
    # 防ぐため有限の既定値を持つ。
    early_death_window_seconds: int = 120
    max_early_death_retries: int = 2
    early_death_backoff_seconds: int = 60
    # #438: ウィンドウ内の総トークン消費上限およびサブタスクごとの消費上限
    max_tokens_per_window: int | None = None
    max_tokens_per_task: int | None = None
    # #282: status:not-needed判定の独立検証レビュー（保留分）の永続化先。
    not_needed_review_state_path: Path = Path("not_needed_review_state.json")
    # #511: 上記の保留エントリが、どちらの結果ラベル
    # （not-needed-review:passed/failed）も付かないまま保持され続ける秒数の上限。
    # レビューセッションのクラッシュ等で結果が返らなかった場合に永久pending化
    # しないための終端。#512の`max_task_reclaims`と同じ理由で「無制限」を表す
    # 値は用意しない（終端のない経路を作らないため）。
    not_needed_review_timeout_seconds: int = 86400
    # #394: Integratorが統合ブランチ上で実行するCIコマンド。未指定（None）の場合、
    # IntegratorはOrchestune自身のリポジトリ固有の既定値（./scripts/local-ci.sh）
    # にフォールバックする。導入先リポジトリのCIエントリーポイントに合わせて
    # 明示的に設定することを推奨する（セットアップガイドの「導入要件」参照）。
    ci_command: list[str] | None = None
    # #398/#404: orchestune-dag CLIと同じorchestune.toml/[tool.orchestune]の
    # dag_ignore_patternsから読み込んだ追加の無視パターン。Conflict Graph再計算
    # （_decide_footprint_deviation_outcome/_collect_active_conflict_subtask_ids）
    # にも一貫して適用し、初回検証で無視されたファイルが実行中の再計算で
    # 誤って競合として検知されないようにする。
    dag_ignore_patterns: tuple[re.Pattern[str], ...] = ()
    # #407レビュー(#415): orchestune-dag CLIと同じorchestune.toml/[tool.orchestune]の
    # dag_similarity_thresholdから読み込んだ閾値。dag_ignore_patternsと同様、
    # 実行時Conflict Graph再計算（_decide_footprint_deviation_outcome/
    # _collect_active_conflict_subtask_ids）とpost-cycle integratorにも
    # 一貫して適用し、orchestune-dagで意図的に消したエッジが既定閾値の
    # 再計算で復活してしまわないようにする。
    dag_similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD
    # #668: リポジトリ定義の実行プロファイル設定（モデル・推論強度解決用）
    execution_profile_config: ExecutionProfileConfig | None = None
    # #706/#709/#746: modeは追加のrepository-wide loopを段階化する。
    # Supervisor配下へ移行済みの安全なstatus/recovery/GC自己修復は、後方互換の
    # default動作としてこのmodeおよび追加allowlistとは独立して維持する。
    consistency_mode: ConsistencyMode = ConsistencyMode.OFF
    consistency_repair_allowlist: frozenset[str] = frozenset()
    consistency_max_repair_passes: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.consistency_mode, ConsistencyMode):
            self.consistency_mode = ConsistencyMode(self.consistency_mode)
        self.consistency_repair_allowlist = frozenset(self.consistency_repair_allowlist)
        if not 1 <= self.consistency_max_repair_passes <= MAX_REPAIR_PASSES:
            raise ValueError(
                "consistency_max_repair_passes must be between "
                f"1 and {MAX_REPAIR_PASSES}"
            )
        if self.execution_profile_config is None:
            self.execution_profile_config = ExecutionProfileConfig()
        if self.dispatch_target is None:
            self.dispatch_target = LocalProcessDispatchTarget(log_dir=self.log_dir)
        if self.forge is None:
            self.forge = GitHubForge()

    @property
    def resolved_forge(self) -> Forge:
        """`__post_init__`が常に既定値を設定するため、`self.forge`自体は
        `Forge | None`型だがここでは非Noneであることが保証される。
        呼び出し側での重複した`is not None`チェックを避ける。"""
        assert self.forge is not None
        return self.forge
