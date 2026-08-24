from __future__ import annotations

import copy
import subprocess as subprocess  # compatibility patch surface
from pathlib import Path

from orchestune.infra.git_cli import run_git
from orchestune.infra.process_utils import default_ci_command
from orchestune.integrator_steps import (
    AutoMergeChildIntegrationStep,
    EnsureIntegrationPrStep,
    LabelIncludedStep,
    MergeAndTestStep,
    PrepareTasksStep,
    PushTempBranchStep,
    RetryChildIssueCloseStep,
    SemanticReviewStep,
    SetupWorktreeStep,
    _mark_tasks_included,
    clear_parent_branch_stale_marker,
)
from orchestune.integrator_types import (
    IntegrationComponent,
    IntegrationContext,
    IntegrationReport,
    IntegrationStatus,
    IntegratorConfig,
)
from orchestune.integrator_worktree import IntegrationWorktree
from orchestune.models import Task


class IntegrationPipeline(IntegrationComponent):
    def __init__(self, steps: list[IntegrationComponent]):
        self.steps = steps

    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        merged_report: IntegrationReport = {}
        try:
            self._run_steps(ctx, merged_report)
        finally:
            self._cleanup_temp_worktree(ctx)

        return self._build_final_report(ctx, merged_report)

    def _run_steps(self, ctx: IntegrationContext, report: IntegrationReport) -> None:
        for step in self.steps:
            result = step.execute(ctx)
            report.update(result)
            if "status" in result:
                ctx.status = result["status"]
            if "error" in result:
                ctx.error = result["error"]
            if ctx.status != IntegrationStatus.SUCCESS:
                break

    @staticmethod
    def _cleanup_temp_worktree(ctx: IntegrationContext) -> None:
        if ctx.temp_worktree_path:
            try:
                run_git(
                    ["worktree", "remove", "--force", str(ctx.temp_worktree_path)],
                    cwd=ctx.original_root,
                    check=True,
                )
            except Exception:
                pass
        # The stale marker is managed separately on CAS rejection.
        if ctx.config.apply and not ctx.parent_branch_cas_rejected_this_cycle:
            clear_parent_branch_stale_marker(ctx)

    @staticmethod
    def _build_final_report(
        ctx: IntegrationContext, merged_report: IntegrationReport
    ) -> IntegrationReport:
        final_report: IntegrationReport = copy.deepcopy(merged_report)
        final_report["status"] = ctx.status
        final_report["merged"] = ctx.merged_tasks

        if ctx.status == IntegrationStatus.SUCCESS:
            final_report["integration_pr_number"] = ctx.integration_pr_number
            final_report["semantic_review_dispatched"] = ctx.semantic_review_dispatched
            final_report["newly_included"] = ctx.newly_included
        if ctx.failed_tasks:
            final_report["failed"] = ctx.failed_tasks
            final_report["failed_reasons"] = ctx.failed_reasons
        if ctx.blocked_tasks:
            final_report["blocked"] = ctx.blocked_tasks
            final_report["blocked_reasons"] = ctx.blocked_reasons
        if ctx.error:
            final_report["error"] = ctx.error
        if ctx.unparsable_done_tasks:
            final_report["unparsable_done_issues"] = [
                task.issue_number for task in ctx.unparsable_done_tasks
            ]
        return final_report


class MultiIssueIntegrator(IntegrationComponent):
    def __init__(self, integrators: list[IntegrationComponent]):
        self.integrators = integrators

    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        details: dict[str, IntegrationReport] = {}
        success_count = 0
        failure_count = 0

        for integrator in self.integrators:
            sub_ctx = copy.deepcopy(ctx)
            # #313レビュー対応: 注入されたForge（可変な内部状態やlock/clientを
            # 保持しうる）をdeepcopyで複製・破壊しないよう、元の参照を維持する。
            sub_ctx.config.forge = ctx.config.forge
            parent_issue = getattr(integrator, "parent_issue", None)
            key = (
                f"issue_{parent_issue}"
                if parent_issue is not None
                else f"integrator_{id(integrator)}"
            )
            result = integrator.execute(sub_ctx)
            details[key] = result
            if result.get("status") in (
                IntegrationStatus.SUCCESS,
                IntegrationStatus.NO_DONE_TASKS,
            ):
                success_count += 1
            else:
                failure_count += 1

        if success_count > 0 and failure_count == 0:
            overall_status = IntegrationStatus.COMPOSITE_SUCCESS
        elif success_count > 0 and failure_count > 0:
            overall_status = IntegrationStatus.COMPOSITE_PARTIAL_SUCCESS
        else:
            overall_status = IntegrationStatus.COMPOSITE_FAILURE
        if not self.integrators:
            overall_status = IntegrationStatus.COMPOSITE_SUCCESS

        return {"status": overall_status, "details": details}


class SingleIssueIntegrator(IntegrationComponent):
    def __init__(self, parent_issue: int | None, pipeline: IntegrationComponent):
        self.parent_issue = parent_issue
        self.pipeline = pipeline

    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        if self.parent_issue is not None:
            ctx.config.parent_issue_number = self.parent_issue
            ctx.base_branch = f"origin/parent/issue-{self.parent_issue}"
            ctx.temp_branch = (
                f"integration/temp-parent-issue-{self.parent_issue}-"
                f"{ctx.config.integration_run_id}"
            )
            ctx.config.base_branch = ctx.base_branch
            ctx.config.temp_branch = ctx.temp_branch

        # #435: runごとに一意なworktreeを使うため、CIを含むサイクル全体は
        # ロックしない。worktree操作だけを各Stepで短く保護する。
        return self.pipeline.execute(ctx)


class Integrator:
    def __init__(self, config: IntegratorConfig):
        self.config = config
        if self.config.ci_command is None:
            self.config.ci_command = default_ci_command()
        self.original_root = Path(self.config.repository_root).resolve()
        self.config.repository_root = self.original_root
        self._worktree = IntegrationWorktree(
            self.original_root, self.config.temp_branch
        )
        self.failed_reasons: dict[str, str] = {}
        self.blocked_reasons: dict[str, str] = {}
        self.unparsable_done_tasks: list[Task] = []

    def _worktree_key(self) -> str:
        return self._worktree.key()

    def _temp_worktree_path(self) -> Path:
        return self._worktree.temp_path()

    def _worktree_lock_path(self) -> Path:
        return self._worktree.lock_path()

    def _reclaim_worktree_path(self, path: Path) -> None:
        self._worktree.reclaim(path)

    def run(self) -> IntegrationReport:
        ctx = IntegrationContext(
            config=self.config,
            repository_root=self.original_root,
            original_root=self.original_root,
            base_branch=self.config.base_branch,
            temp_branch=self.config.temp_branch,
        )
        pipeline = IntegrationPipeline(
            [
                PrepareTasksStep(),
                RetryChildIssueCloseStep(),
                SetupWorktreeStep(),
                MergeAndTestStep(),
                PushTempBranchStep(),
                EnsureIntegrationPrStep(),
                SemanticReviewStep(),
                AutoMergeChildIntegrationStep(),
                LabelIncludedStep(),
            ]
        )
        runner = SingleIssueIntegrator(
            parent_issue=self.config.parent_issue_number,
            pipeline=pipeline,
        )
        result = runner.execute(ctx)
        self.failed_reasons = ctx.failed_reasons
        self.blocked_reasons = ctx.blocked_reasons
        self.unparsable_done_tasks = ctx.unparsable_done_tasks
        return result


__all__ = [
    "AutoMergeChildIntegrationStep",
    "EnsureIntegrationPrStep",
    "IntegrationComponent",
    "IntegrationContext",
    "IntegrationPipeline",
    "IntegrationReport",
    "IntegrationStatus",
    "Integrator",
    "IntegratorConfig",
    "LabelIncludedStep",
    "MergeAndTestStep",
    "MultiIssueIntegrator",
    "PrepareTasksStep",
    "PushTempBranchStep",
    "RetryChildIssueCloseStep",
    "SemanticReviewStep",
    "SetupWorktreeStep",
    "SingleIssueIntegrator",
    "_mark_tasks_included",
]
