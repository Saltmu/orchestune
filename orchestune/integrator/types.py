"""Shared contracts for integration orchestration and concrete steps."""

from __future__ import annotations

import os
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, TypedDict

from orchestune.dag.similarity import DEFAULT_SIMILARITY_THRESHOLD
from orchestune.forge import Forge, GitHubForge
from orchestune.infra.managed_process import ProcessRunner
from orchestune.integrator.coordinator import IntegrationCoordinator
from orchestune.integrator.execution import ExecutionState
from orchestune.integrator.proofs import TaskIntegrationProof
from orchestune.integrator.tasks import DependencyIntegrationStates
from orchestune.integrator.timeout_policy import (
    DEFAULT_INTEGRATION_CI_TIMEOUT_SECONDS,
    DEFAULT_INTEGRATION_CLEANUP_TIMEOUT_SECONDS,
    DEFAULT_INTEGRATION_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_INTEGRATION_CYCLE_TIMEOUT_SECONDS,
    DEFAULT_INTEGRATION_DEPENDENCY_TIMEOUT_SECONDS,
    DEFAULT_INTEGRATION_TIMEOUT_BACKOFF_SECONDS,
    DEFAULT_MAX_INTEGRATION_TIMEOUT_RETRIES,
    STATUS_EXECUTION_CLEANUP_FAILED,
    STATUS_EXECUTION_INDETERMINATE,
    STATUS_EXECUTION_RETRY_EXHAUSTED,
    STATUS_EXECUTION_TIMED_OUT,
    IntegrationExecutionPolicy,
)
from orchestune.models import Task
from orchestune.outcome_record import VALID_CHILD_REVIEW_GATE_MODES
from orchestune.task_branch_resolution import TaskBranchResolver, TaskMergeReceipt


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
    REVIEW_GATE_BLOCKED = "review_gate_blocked"
    # #820: bounded execution. A confirmed timeout that may still be retried; an
    # unconfirmed stop/rollback/write held for a human; an exhausted retry budget; an
    # unreadable or unresolved history. None of these re-queues the worker.
    EXECUTION_TIMED_OUT = STATUS_EXECUTION_TIMED_OUT
    EXECUTION_CLEANUP_FAILED = STATUS_EXECUTION_CLEANUP_FAILED
    EXECUTION_RETRY_EXHAUSTED = STATUS_EXECUTION_RETRY_EXHAUSTED
    EXECUTION_INDETERMINATE = STATUS_EXECUTION_INDETERMINATE


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
    # #827: children whose branch deletion is held / escalated this cycle.
    finalization_deferred: list[int]
    finalization_escalated: list[int]
    auto_merged: bool
    closed_issues: list[int]
    # #820: structured causes (stage, limits, attempt, stop/rollback/write state,
    # output tail) instead of a generic "CI verification failed".
    execution_failures: list[dict[str, Any]]
    details: dict[str, IntegrationReport]


@dataclass(kw_only=True)
class IntegratorConfig:
    parent_issue_number: int
    repository_root: Path = Path(".")
    base_branch: str = ""
    temp_branch: str = ""
    ci_command: list[str] | None = None
    integration_run_id: str = field(default_factory=_default_integration_run_id)
    apply: bool = False
    enable_semantic_review: bool = True
    coordinator: IntegrationCoordinator | None = None
    forge: Forge | None = None
    # #398/#404/#407/#659: DispatcherConfigと同じConflict Graph設定を
    # get_sorted_done_tasks -> build_dagにも一貫して渡す。統合順序自体は
    # #659以降、明示的なdepends_onだけで決まる。
    dag_ignore_patterns: tuple[re.Pattern[str], ...] = ()
    # #407/#415/#659: Conflict Graphの再現性とAPI後方互換のために保持する。
    dag_similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD
    # #1031: 子タスクのレビュー合格証跡（verdict=pass, SHA一致）を検証するゲート
    child_review_gate: str = "required"
    # #820: execution bounds. Defaults come from the shared policy constants so the
    # Dispatcher and a directly constructed Integrator agree.
    integration_dependency_timeout_seconds: int = (
        DEFAULT_INTEGRATION_DEPENDENCY_TIMEOUT_SECONDS
    )
    integration_ci_timeout_seconds: int = DEFAULT_INTEGRATION_CI_TIMEOUT_SECONDS
    integration_cycle_timeout_seconds: int = DEFAULT_INTEGRATION_CYCLE_TIMEOUT_SECONDS
    integration_cleanup_timeout_seconds: int = (
        DEFAULT_INTEGRATION_CLEANUP_TIMEOUT_SECONDS
    )
    integration_command_timeout_seconds: int = (
        DEFAULT_INTEGRATION_COMMAND_TIMEOUT_SECONDS
    )
    max_integration_timeout_retries: int = DEFAULT_MAX_INTEGRATION_TIMEOUT_RETRIES
    integration_timeout_backoff_seconds: int = (
        DEFAULT_INTEGRATION_TIMEOUT_BACKOFF_SECONDS
    )
    # Injection seam for the process runner (default: the owning managed runner).
    process_runner: ProcessRunner | None = None

    def __post_init__(self) -> None:
        if self.child_review_gate not in VALID_CHILD_REVIEW_GATE_MODES:
            raise ValueError(
                f"child_review_gate must be 'required' or 'off', got {self.child_review_gate!r}"
            )
        self.build_execution_policy()  # validate the explicit settings
        self.base_branch = f"origin/parent/issue-{self.parent_issue_number}"
        self.temp_branch = (
            "integration/temp-parent-issue-"
            f"{self.parent_issue_number}-{self.integration_run_id}"
        )
        if self.forge is None:
            self.forge = GitHubForge()

    @property
    def execution_policy(self) -> IntegrationExecutionPolicy:
        return self.build_execution_policy()

    def build_execution_policy(self) -> IntegrationExecutionPolicy:
        """Validate and return the execution policy from the explicit fields."""
        return IntegrationExecutionPolicy(
            integration_dependency_timeout_seconds=self.integration_dependency_timeout_seconds,
            integration_ci_timeout_seconds=self.integration_ci_timeout_seconds,
            integration_cycle_timeout_seconds=self.integration_cycle_timeout_seconds,
            integration_cleanup_timeout_seconds=self.integration_cleanup_timeout_seconds,
            integration_command_timeout_seconds=self.integration_command_timeout_seconds,
            max_integration_timeout_retries=self.max_integration_timeout_retries,
            integration_timeout_backoff_seconds=self.integration_timeout_backoff_seconds,
        )


@dataclass
class IntegrationContext:
    config: IntegratorConfig
    repository_root: Path
    original_root: Path
    base_branch: str
    temp_branch: str
    merged_tasks: list[str] = field(default_factory=list)
    merged_task_proofs: dict[int, TaskIntegrationProof] = field(default_factory=dict)
    task_merge_receipts: dict[int, TaskMergeReceipt] = field(default_factory=dict)
    task_branch_resolver: TaskBranchResolver | None = None
    failed_tasks: list[str] = field(default_factory=list)
    blocked_tasks: list[str] = field(default_factory=list)
    failed_reasons: dict[str, str] = field(default_factory=dict)
    blocked_reasons: dict[str, str] = field(default_factory=dict)
    unparsable_done_tasks: list[Task] = field(default_factory=list)
    active_done_tasks: list[Task] = field(default_factory=list)
    dependency_states: DependencyIntegrationStates | None = None
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
    # #820: set by ``SingleIssueIntegrator`` for an applying cycle.
    execution: ExecutionState | None = None
    execution_failures: list[dict[str, Any]] = field(default_factory=list)

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
    "TaskIntegrationProof",
]
