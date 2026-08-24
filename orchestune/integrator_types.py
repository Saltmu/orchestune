"""Shared contracts for integration orchestration and concrete steps."""

from __future__ import annotations

import os
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TypedDict

from orchestune.dag.similarity import DEFAULT_SIMILARITY_THRESHOLD
from orchestune.forge import Forge, GitHubForge
from orchestune.integration_coordinator import IntegrationCoordinator
from orchestune.models import Task


def _default_integration_run_id() -> str:
    """Git refとして安全なrun idを返す。"""
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", run_id):
        return run_id
    return uuid.uuid4().hex


class IntegrationStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL_SUCCESS = "partial_success"
    NO_DONE_TASKS = "no_done_tasks"
    FAILED_TO_CREATE_TEMP_WORKTREE = "failed_to_create_temp_worktree"
    FAILED_TO_CREATE_TEMP_BRANCH = "failed_to_create_temp_branch"
    FAILED_TO_PUSH_TEMP_BRANCH = "failed_to_push_temp_branch"
    AUTO_MERGE_FAILED = "auto_merge_failed"
    PARENT_BRANCH_ADVANCED = "parent_branch_advanced"
    INTEGRATION_BRANCH_LOCKED = "integration_branch_locked"
    COMPOSITE_SUCCESS = "composite_success"
    COMPOSITE_PARTIAL_SUCCESS = "composite_partial_success"
    COMPOSITE_FAILURE = "composite_failure"


class IntegrationReport(TypedDict, total=False):
    status: IntegrationStatus
    error: str
    merged: list[str]
    failed: list[str]
    failed_reasons: dict[str, str]
    blocked: list[str]
    blocked_reasons: dict[str, str]
    integration_pr_number: int | None
    semantic_review_dispatched: bool
    newly_included: list[str]
    unparsable_done_issues: list[int]
    retried_closed_issues: list[int]
    auto_merged: bool
    closed_issues: list[int]
    details: dict[str, IntegrationReport]


@dataclass
class IntegratorConfig:
    repository_root: Path = Path(".")
    base_branch: str = "origin/main"
    temp_branch: str = "integration/temp-main"
    ci_command: list[str] | None = None
    parent_issue_number: int | None = None
    integration_run_id: str = field(default_factory=_default_integration_run_id)
    apply: bool = False
    enable_semantic_review: bool = True
    coordinator: IntegrationCoordinator | None = None
    forge: Forge | None = None
    # #398/#404/#407: DispatcherConfig.dag_ignore_patternsと同じ設定を
    # get_sorted_done_tasksのDAG構築（get_sorted_done_tasks -> build_dag）にも
    # 一貫して適用し、無視されるべきfootprint衝突が明示的な依存関係と
    # 組み合わさって偽のDagCycleErrorを誘発しないようにする。
    dag_ignore_patterns: tuple[re.Pattern[str], ...] = ()
    # #407/#415: DispatcherConfig.dag_similarity_thresholdと同じ設定を
    # get_sorted_done_tasksのDAG構築にも一貫して適用し、意図的に作った・
    # 消した類似度エッジが既定閾値の再計算で復活・消失して統合順序
    # （topological_order）が検証時と食い違わないようにする。
    dag_similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD

    def __post_init__(self) -> None:
        if self.parent_issue_number is not None:
            self.base_branch = f"origin/parent/issue-{self.parent_issue_number}"
            self.temp_branch = (
                "integration/temp-parent-issue-"
                f"{self.parent_issue_number}-{self.integration_run_id}"
            )
        else:
            self.temp_branch = f"{self.temp_branch}-{self.integration_run_id}"
        if self.forge is None:
            self.forge = GitHubForge()


@dataclass
class IntegrationContext:
    config: IntegratorConfig
    repository_root: Path
    original_root: Path
    base_branch: str
    temp_branch: str
    merged_tasks: list[str] = field(default_factory=list)
    failed_tasks: list[str] = field(default_factory=list)
    blocked_tasks: list[str] = field(default_factory=list)
    failed_reasons: dict[str, str] = field(default_factory=dict)
    blocked_reasons: dict[str, str] = field(default_factory=dict)
    unparsable_done_tasks: list[Task] = field(default_factory=list)
    active_done_tasks: list[Task] = field(default_factory=list)
    integration_pr_number: int | None = None
    semantic_review_dispatched: bool = False
    newly_included: list[str] = field(default_factory=list)
    temp_worktree_path: Path | None = None
    status: IntegrationStatus = IntegrationStatus.SUCCESS
    error: str | None = None
    # #437レビュー対応: このサイクルで実際にnon-fast-forward（CAS）拒否を
    # 検知したかどうか。`IntegrationPipeline`が、CAS拒否以外の理由でサイクルが
    # 終了した場合（CI失敗・worktreeセットアップ失敗等、`AutoMergeChildIntegrationStep`
    # 自体に到達しない場合を含む）に陳腐化マーカーラベルをクリアするかどうかの
    # 判定に使う。
    parent_branch_cas_rejected_this_cycle: bool = False

    @property
    def forge(self) -> Forge:
        assert self.config.forge is not None
        return self.config.forge


class IntegrationComponent(ABC):
    @abstractmethod
    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        pass


__all__ = [
    "IntegrationComponent",
    "IntegrationContext",
    "IntegrationReport",
    "IntegrationStatus",
    "IntegratorConfig",
]
