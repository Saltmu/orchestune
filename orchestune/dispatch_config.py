"""ディスパッチャー全体の設定（DispatcherConfig）。

act側モジュール（dispatch_gc/dispatch_rebase/dispatch_escalation等）が
dispatch_rules.pyのRule/CycleContext経由でこの設定型を参照する際に、
dispatch_cycle.py経由の循環importを避けるため独立モジュールとして切り出す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from orchestune.dag_similarity import DEFAULT_SIMILARITY_THRESHOLD
from orchestune.dispatch_targets import DispatchTarget, LocalProcessDispatchTarget
from orchestune.forge import Forge, GitHubForge


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
    # #437: 親branch更新（CAS）の連続陳腐化がこの回数に達すると、対象の
    # 子Issueをstatus:blocked-human-reviewへエスカレーションする。
    max_parent_branch_stale_retries: int = 3
    task_timeout_seconds: int = 0
    zombie_gc: bool = True
    # #282: status:not-needed判定の独立検証レビュー（保留分）の永続化先。
    not_needed_review_state_path: Path = Path("not_needed_review_state.json")
    # #394: Integratorが統合ブランチ上で実行するCIコマンド。未指定（None）の場合、
    # IntegratorはOrchestune自身のリポジトリ固有の既定値（./scripts/local-ci.sh）
    # にフォールバックする。導入先リポジトリのCIエントリーポイントに合わせて
    # 明示的に設定することを推奨する（セットアップガイドの「導入要件」参照）。
    ci_command: list[str] | None = None
    # #398/#404: orchestune-dag CLIと同じorchestune.toml/[tool.orchestune]の
    # dag_ignore_patternsから読み込んだ追加の無視パターン。DAG再計算
    # （_decide_footprint_deviation_outcome/_collect_active_conflict_subtask_ids）
    # にも一貫して適用し、初回検証で無視されたファイルが実行中の再計算で
    # 誤って競合として検知されないようにする。
    dag_ignore_patterns: tuple[re.Pattern[str], ...] = ()
    # #407レビュー(#415): orchestune-dag CLIと同じorchestune.toml/[tool.orchestune]の
    # dag_similarity_thresholdから読み込んだ閾値。dag_ignore_patternsと同様、
    # 実行時DAG再計算（_decide_footprint_deviation_outcome/
    # _collect_active_conflict_subtask_ids）とpost-cycle integratorにも
    # 一貫して適用し、orchestune-dagで意図的に消したエッジが既定閾値の
    # 再計算で復活してしまわないようにする。
    dag_similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD

    def __post_init__(self) -> None:
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
