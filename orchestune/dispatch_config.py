"""ディスパッチャー全体の設定（DispatcherConfig）。

act側モジュール（dispatch_gc/dispatch_rebase/dispatch_escalation等）が
dispatch_rules.pyのRule/CycleContext経由でこの設定型を参照する際に、
dispatch_cycle.py経由の循環importを避けるため独立モジュールとして切り出す。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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
    task_timeout_seconds: int = 0
    zombie_gc: bool = True
    # #282: status:not-needed判定の独立検証レビュー（保留分）の永続化先。
    not_needed_review_state_path: Path = Path("not_needed_review_state.json")
    # #394: Integratorが統合ブランチ上で実行するCIコマンド。未指定（None）の場合、
    # IntegratorはOrchestune自身のリポジトリ固有の既定値（./scripts/local-ci.sh）
    # にフォールバックする。導入先リポジトリのCIエントリーポイントに合わせて
    # 明示的に設定することを推奨する（セットアップガイドの「導入要件」参照）。
    ci_command: list[str] | None = None

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
