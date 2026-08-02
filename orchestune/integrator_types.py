"""Shared contracts for integration orchestration and concrete steps."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TypedDict

from orchestune.forge import Forge, GitHubForge
from orchestune.integration_coordinator import IntegrationCoordinator
from orchestune.models import Task


class IntegrationStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL_SUCCESS = "partial_success"
    NO_DONE_TASKS = "no_done_tasks"
    FAILED_TO_CREATE_TEMP_WORKTREE = "failed_to_create_temp_worktree"
    FAILED_TO_CREATE_TEMP_BRANCH = "failed_to_create_temp_branch"
    FAILED_TO_PUSH_TEMP_BRANCH = "failed_to_push_temp_branch"
    AUTO_MERGE_FAILED = "auto_merge_failed"
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
    apply: bool = False
    enable_semantic_review: bool = True
    coordinator: IntegrationCoordinator | None = None
    forge: Forge | None = None

    def __post_init__(self) -> None:
        if self.parent_issue_number is not None:
            self.base_branch = f"origin/parent/issue-{self.parent_issue_number}"
            self.temp_branch = (
                f"integration/temp-parent-issue-{self.parent_issue_number}"
            )
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
