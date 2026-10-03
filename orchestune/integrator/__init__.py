from __future__ import annotations

import copy
import subprocess as subprocess  # compatibility patch surface
import sys
from contextlib import ExitStack, nullcontext
from pathlib import Path

from orchestune.infra.execution_deadline import (
    ExecutionCommandTimeout,
    ExecutionDeadlineExceeded,
    ExecutionInterrupt,
    activate_scope,
)
from orchestune.infra.git_cli import run_git
from orchestune.infra.process_utils import default_ci_command
from orchestune.integrator.execution import (
    ExecutionState,
    IntegrationExecutionAbort,
    provisional_status,
    require_bounded_forge,
)
from orchestune.integrator.review_gate import (
    ChildReviewGateDecision,
    decide_child_review_gate,
)
from orchestune.integrator.steps import (
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
from orchestune.integrator.types import (
    IntegrationComponent,
    IntegrationContext,
    IntegrationReport,
    IntegrationStatus,
    IntegratorConfig,
)
from orchestune.integrator.worktree import IntegrationWorktree
from orchestune.models import Task

# Steps that write to git or GitHub. A timeout *inside* one leaves the write's
# outcome unknown (#820); a timeout before any of them only ends the cycle.
_WRITE_STEPS = frozenset(
    {
        "RetryChildIssueCloseStep",
        "PushTempBranchStep",
        "EnsureIntegrationPrStep",
        "SemanticReviewStep",
        "AutoMergeChildIntegrationStep",
        "LabelIncludedStep",
    }
)
_EXECUTION_TIMEOUTS = (ExecutionDeadlineExceeded, ExecutionCommandTimeout)
# The only write step with a single write: a refused start there wrote nothing.
_SINGLE_WRITE_STEPS = frozenset({"PushTempBranchStep"})


def _remove_temp_worktree(ctx: IntegrationContext) -> None:
    """Remove the temporary worktree, on the cleanup budget once time has run out."""
    if not ctx.temp_worktree_path:
        return
    execution = ctx.execution
    scope = execution.scope if execution is not None else None
    aborted = execution is not None and execution.abort is not None
    phase = (
        scope.cleanup_phase()
        if scope is not None and (scope.expired() or aborted)
        else nullcontext()
    )
    with phase:
        try:
            run_git(
                ["worktree", "remove", "--force", str(ctx.temp_worktree_path)],
                cwd=ctx.original_root,
                check=True,
            )
        except (Exception, ExecutionInterrupt):
            pass


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
            try:
                self._guard_deadline(ctx, step)
                result = step.execute(ctx)
            except IntegrationExecutionAbort as abort:
                self._record_abort(ctx, abort)
                break
            except _EXECUTION_TIMEOUTS as error:
                if ctx.execution is None:
                    raise
                self._record_abort(
                    ctx,
                    ctx.execution.timeout_abort(
                        error,
                        type(step).__name__,
                        write_step=type(step).__name__ in _WRITE_STEPS,
                        before_start=isinstance(error, ExecutionDeadlineExceeded)
                        and (
                            error.before_step
                            or type(step).__name__ in _SINGLE_WRITE_STEPS
                        ),
                    ),
                )
                break
            report.update(result)
            if "status" in result:
                ctx.status = result["status"]
            if "error" in result:
                ctx.error = result["error"]
            if ctx.status != IntegrationStatus.SUCCESS:
                break

    @staticmethod
    def _guard_deadline(ctx: IntegrationContext, step: IntegrationComponent) -> None:
        """Start no new step once the parent's cycle deadline has passed."""
        execution = ctx.execution
        if execution is not None and execution.scope.expired():
            raise ExecutionDeadlineExceeded(type(step).__name__, before_step=True)

    @staticmethod
    def _record_abort(
        ctx: IntegrationContext, abort: IntegrationExecutionAbort
    ) -> None:
        abort.status = provisional_status(abort)
        ctx.status = IntegrationStatus(abort.status)
        ctx.error = str(abort)
        ctx.execution_failures.append(abort.failure.to_dict())
        execution = ctx.execution
        if execution is None:
            return
        execution.abort = abort
        if abort.hold:
            execution.hold(abort.failure.detail or abort.failure.cause.value)

    @staticmethod
    def _cleanup_temp_worktree(ctx: IntegrationContext) -> None:
        execution = ctx.execution
        if execution is not None and execution.holding:
            print(
                "[Integrator] Holding the integration worktree for a human "
                f"({ctx.temp_worktree_path}): {execution.hold_reason}",
                file=sys.stderr,
            )
            return
        scope = execution.scope if execution is not None else None
        aborted = execution is not None and execution.abort is not None
        if execution is not None:
            # #820: keep the worktree until the attempt's result is saved; if that
            # cannot be confirmed the cycle becomes indeterminate and must keep it.
            execution.worktree_removal_pending = True
        else:
            _remove_temp_worktree(ctx)
        # The stale marker is managed separately on CAS rejection.
        if (
            ctx.config.apply
            and not ctx.parent_branch_cas_rejected_this_cycle
            and not aborted
            and (scope is None or not scope.expired())
        ):
            try:
                clear_parent_branch_stale_marker(ctx)
            except ExecutionInterrupt as error:
                print(
                    f"Warning: stale-marker cleanup was cut short: {error}",
                    file=sys.stderr,
                )

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
        if ctx.execution_failures:
            final_report["execution_failures"] = list(ctx.execution_failures)
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
    def __init__(self, parent_issue: int, pipeline: IntegrationComponent):
        self.parent_issue = parent_issue
        self.pipeline = pipeline

    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        ctx.config.parent_issue_number = self.parent_issue
        ctx.base_branch = f"origin/parent/issue-{self.parent_issue}"
        ctx.temp_branch = (
            f"integration/temp-parent-issue-{self.parent_issue}-"
            f"{ctx.config.integration_run_id}"
        )
        ctx.config.base_branch = ctx.base_branch
        ctx.config.temp_branch = ctx.temp_branch

        if not ctx.config.apply:
            # Dry run: validate the execution policy only. No process, reservation
            # comment or label change happens.
            ctx.config.build_execution_policy()
            return self.pipeline.execute(ctx)
        return self._execute_bounded(ctx)

    def _execute_bounded(self, ctx: IntegrationContext) -> IntegrationReport:
        """#820: run one parent's cycle under a monotonic deadline and attempt budget."""
        forge_error = require_bounded_forge(ctx.config.forge)
        if forge_error is not None:
            return {"status": IntegrationStatus.FAILURE, "error": forge_error}
        state = ExecutionState.create(
            parent_issue_number=self.parent_issue,
            policy=ctx.config.execution_policy,
            forge=ctx.config.forge,
            original_root=ctx.original_root,
            temp_branch=ctx.temp_branch,
        )
        ctx.execution = state
        with ExitStack() as stack:
            stack.enter_context(activate_scope(state.scope))
            lock_error = state.acquire_parent_lock(stack)
            if lock_error is not None:
                return {
                    "status": IntegrationStatus.INTEGRATION_BRANCH_LOCKED,
                    "error": lock_error,
                }
            # #435: runごとに一意なworktreeを使うため、worktree操作だけを各Stepで
            # 短く保護する。実行全体は上の親単位ロックで直列化する（#820）。
            try:
                report = self.pipeline.execute(ctx)
                return self._finalize(ctx, state, report)
            finally:
                if state.worktree_removal_pending and not state.holding:
                    _remove_temp_worktree(ctx)

    @staticmethod
    def _finalize(
        ctx: IntegrationContext, state: ExecutionState, report: IntegrationReport
    ) -> IntegrationReport:
        normal_success = (
            report.get("status") == IntegrationStatus.SUCCESS
            and not ctx.failed_tasks
            and bool(ctx.merged_tasks)
        )
        outcome = state.finalize(normal_success=normal_success)
        if outcome.failures:
            report["execution_failures"] = [
                failure.to_dict() for failure in outcome.failures
            ]
        if outcome.status is not None:
            report["status"] = IntegrationStatus(outcome.status)
            report["error"] = outcome.failures[0].detail if outcome.failures else ""
        return report


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
    "ChildReviewGateDecision",
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
    "decide_child_review_gate",
]
