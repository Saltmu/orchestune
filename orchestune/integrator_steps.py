"""Concrete steps executed by the integration pipeline.

The orchestration primitives and shared context live in :mod:`orchestune.integrator`.
Keeping the concrete operations here makes the dependency direction explicit: steps
depend on the pipeline contract, while the integrator loads its default steps lazily.
"""

from __future__ import annotations

import subprocess
import sys

from orchestune.git_cli import run_git
from orchestune.integrator_git_ops import IntegrationMerger
from orchestune.integrator_pr import ensure_integration_pr
from orchestune.integrator_tasks import get_sorted_done_tasks
from orchestune.integrator_types import (
    IntegrationComponent,
    IntegrationContext,
    IntegrationReport,
    IntegrationStatus,
)
from orchestune.integrator_worktree import IntegrationWorktree


class PrepareTasksStep(IntegrationComponent):
    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        sorted_done_tasks, ctx.unparsable_done_tasks = get_sorted_done_tasks(
            ctx.config.parent_issue_number, forge=ctx.config.forge
        )
        self._warn_and_flag_unparsable_done_tasks(ctx)

        ctx.active_done_tasks = [
            task
            for task in sorted_done_tasks
            if task.issue_state != "CLOSED" and task.parent_state != "CLOSED"
        ]

        if not ctx.active_done_tasks:
            return {"status": IntegrationStatus.NO_DONE_TASKS}

        return {"status": IntegrationStatus.SUCCESS}

    def _warn_and_flag_unparsable_done_tasks(self, ctx: IntegrationContext) -> None:
        for task in ctx.unparsable_done_tasks:
            print(
                f"Warning: status:done issue #{task.issue_number} has no extractable "
                "subtask_id (Footprint YAML block missing or malformed); excluded "
                "from integration without being marked merged or failed.",
                file=sys.stderr,
            )
            if ctx.config.apply:
                try:
                    ctx.forge.add_comment(
                        task.issue_number,
                        "Integratorは、このIssueのFootprint YAMLブロックから"
                        "`subtask_id`を抽出できなかったため、統合対象から除外しました。\n"
                        "Issue本文のFootprintブロックを確認し、`subtask_id`を修正してください。",
                    )
                except Exception as error:
                    print(
                        "Warning: Failed to comment on unparsable done issue "
                        f"#{task.issue_number}: {error}",
                        file=sys.stderr,
                    )


class RetryChildIssueCloseStep(IntegrationComponent):
    """Retry closing child issues whose integration was already merged."""

    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        if not ctx.config.apply or ctx.config.parent_issue_number is None:
            return {"status": IntegrationStatus.SUCCESS}

        remaining_tasks = []
        retried_closed: list[int] = []
        for task in ctx.active_done_tasks:
            if "integration:included" not in task.status_labels:
                remaining_tasks.append(task)
                continue
            try:
                ctx.forge.close_issue(
                    task.issue_number,
                    "completed",
                    comment=(
                        "Integratorが親ブランチへの自動マージを完了済みですが、"
                        "前回のクローズ処理に失敗していたため再試行しました。"
                    ),
                )
                retried_closed.append(task.issue_number)
            except Exception as error:
                print(
                    "Warning: Failed to retry closing issue "
                    f"#{task.issue_number}: {error}",
                    file=sys.stderr,
                )
                remaining_tasks.append(task)

        ctx.active_done_tasks = remaining_tasks
        if not ctx.active_done_tasks:
            return {
                "status": IntegrationStatus.NO_DONE_TASKS,
                "retried_closed_issues": retried_closed,
            }
        return {
            "status": IntegrationStatus.SUCCESS,
            "retried_closed_issues": retried_closed,
        }


class SetupWorktreeStep(IntegrationComponent):
    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        if not ctx.config.apply:
            return {"status": IntegrationStatus.SUCCESS}

        try:
            run_git(["worktree", "prune"], cwd=ctx.original_root, check=False)

            worktree_manager = IntegrationWorktree(ctx.original_root, ctx.temp_branch)
            worktree_manager.reclaim(worktree_manager.temp_path())

            run_git(
                [
                    "worktree",
                    "add",
                    str(worktree_manager.temp_path()),
                    ctx.base_branch,
                ],
                cwd=ctx.original_root,
                check=True,
            )
            ctx.repository_root = worktree_manager.temp_path()
            ctx.config.repository_root = worktree_manager.temp_path()
            ctx.temp_worktree_path = worktree_manager.temp_path()
        except (subprocess.CalledProcessError, OSError, RuntimeError) as error:
            return {
                "status": IntegrationStatus.FAILED_TO_CREATE_TEMP_WORKTREE,
                "error": f"Failed to create temp worktree: {error}",
            }
        return {"status": IntegrationStatus.SUCCESS}


class MergeAndTestStep(IntegrationComponent):
    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        merger = IntegrationMerger(
            repository_root=ctx.repository_root,
            original_root=ctx.original_root,
            ci_command=ctx.config.ci_command or ["./scripts/local-ci.sh"],
            forge=ctx.config.forge,
        )

        try:
            if not merger.create_temp_branch(
                ctx.temp_branch, ctx.base_branch, ctx.config.apply
            ):
                return {
                    "status": IntegrationStatus.FAILED_TO_CREATE_TEMP_BRANCH,
                    "error": "Failed to create temp branch",
                }

            (
                merged_tasks,
                failed_tasks,
                blocked_tasks,
                failed_reasons,
                blocked_reasons,
            ) = merger.merge_and_test_tasks(
                ctx.active_done_tasks, ctx.base_branch, ctx.config.apply
            )
            ctx.merged_tasks.extend(merged_tasks)
            ctx.failed_tasks.extend(failed_tasks)
            ctx.blocked_tasks.extend(blocked_tasks)
            ctx.failed_reasons.update(failed_reasons)
            ctx.blocked_reasons.update(blocked_reasons)

            if not failed_tasks and merged_tasks:
                return {"status": IntegrationStatus.SUCCESS}

            status = (
                IntegrationStatus.PARTIAL_SUCCESS
                if merged_tasks
                else IntegrationStatus.FAILURE
            )
            return {
                "status": status,
                "merged": ctx.merged_tasks,
                "failed": ctx.failed_tasks,
                "failed_reasons": ctx.failed_reasons,
                "blocked": ctx.blocked_tasks,
                "blocked_reasons": ctx.blocked_reasons,
            }
        except Exception as error:
            return {
                "status": IntegrationStatus.FAILURE,
                "error": f"Error during merge and test: {error}",
            }


class PushTempBranchStep(IntegrationComponent):
    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        if not ctx.config.apply:
            return {"status": IntegrationStatus.SUCCESS}
        if ctx.failed_tasks or not ctx.merged_tasks:
            return {"status": ctx.status}

        try:
            run_git(
                ["push", "--force", "origin", ctx.temp_branch],
                cwd=ctx.repository_root,
                check=True,
            )
            return {"status": IntegrationStatus.SUCCESS}
        except subprocess.CalledProcessError as error:
            push_error = error.stderr or ""
            print(f"Failed to push temp branch: {push_error}", file=sys.stderr)
            return {
                "status": IntegrationStatus.FAILED_TO_PUSH_TEMP_BRANCH,
                "error": push_error,
            }


class EnsureIntegrationPrStep(IntegrationComponent):
    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        if not ctx.config.apply:
            return {"status": IntegrationStatus.SUCCESS}
        if (
            ctx.status == IntegrationStatus.FAILED_TO_PUSH_TEMP_BRANCH
            or ctx.failed_tasks
            or not ctx.merged_tasks
        ):
            return {"status": ctx.status}

        try:
            pr_number = ensure_integration_pr(
                ctx.temp_branch,
                ctx.base_branch,
                ctx.merged_tasks,
                forge=ctx.config.forge,
            )
            ctx.integration_pr_number = pr_number
            return {
                "status": IntegrationStatus.SUCCESS,
                "integration_pr_number": pr_number,
            }
        except Exception as error:
            print(f"Warning: failed to ensure integration PR: {error}", file=sys.stderr)
            return {"status": IntegrationStatus.SUCCESS, "integration_pr_number": None}


class SemanticReviewStep(IntegrationComponent):
    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        if not ctx.config.apply:
            return {"status": IntegrationStatus.SUCCESS}
        if (
            ctx.failed_tasks
            or not ctx.merged_tasks
            or ctx.integration_pr_number is None
        ):
            return {"status": ctx.status}

        if ctx.config.enable_semantic_review and ctx.config.coordinator is not None:
            try:
                ctx.config.coordinator.dispatch_review(
                    temp_branch=ctx.temp_branch,
                    base_branch=ctx.base_branch,
                    pr_number=ctx.integration_pr_number,
                    parent_issue_number=ctx.config.parent_issue_number,
                    merged_subtask_ids=ctx.merged_tasks,
                )
                ctx.semantic_review_dispatched = True
                return {
                    "status": IntegrationStatus.SUCCESS,
                    "semantic_review_dispatched": True,
                }
            except Exception as error:
                print(
                    f"Warning: Failed to dispatch semantic review: {error}",
                    file=sys.stderr,
                )
        return {
            "status": IntegrationStatus.SUCCESS,
            "semantic_review_dispatched": False,
        }


class LabelIncludedStep(IntegrationComponent):
    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        if not ctx.config.apply:
            return {"status": IntegrationStatus.SUCCESS}
        if ctx.config.parent_issue_number is not None:
            return {
                "status": IntegrationStatus.SUCCESS,
                "newly_included": ctx.newly_included,
            }
        if (
            ctx.failed_tasks
            or not ctx.merged_tasks
            or ctx.integration_pr_number is None
        ):
            return {"status": ctx.status}

        newly_included = _mark_tasks_included(ctx)
        ctx.newly_included = newly_included
        return {
            "status": IntegrationStatus.SUCCESS,
            "newly_included": newly_included,
        }


def _mark_tasks_included(ctx: IntegrationContext) -> list[str]:
    """Label merged tasks that have not already been marked as included."""
    newly_included: list[str] = []
    task_by_subtask_id = {
        task.subtask_id: task for task in ctx.active_done_tasks if task.subtask_id
    }
    for subtask_id in ctx.merged_tasks:
        task = task_by_subtask_id.get(subtask_id)
        if task is None or "integration:included" in task.status_labels:
            continue
        try:
            ctx.forge.add_label(task.issue_number, "integration:included")
            newly_included.append(subtask_id)
        except Exception as error:
            print(
                "Warning: Failed to add integration:included label to "
                f"issue #{task.issue_number}: {error}",
                file=sys.stderr,
            )
    return newly_included


class AutoMergeChildIntegrationStep(IntegrationComponent):
    """Auto-merge child integrations, leaving final parent merges to humans."""

    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        if not ctx.config.apply or ctx.config.parent_issue_number is None:
            return {"status": IntegrationStatus.SUCCESS}
        if ctx.failed_tasks or not ctx.merged_tasks:
            return {"status": ctx.status}

        if ctx.integration_pr_number is None:
            # #373: 前サイクルで`merge_pull_request`は成功していたが、その
            # 直後（ラベル付与前）にプロセスがクラッシュしたケースの回復経路。
            # 今サイクルの一時ブランチは既にbase_branchへ統合済みのため差分
            # 無しでPR作成に失敗する。全対象ブランチの先端が実際にbase_branch
            # へ含まれていることを確認できた場合に限り、マージ済みとして
            # ラベル付与・クローズだけを再試行する。1件でも未検証ならfail
            # closedで何もしない（PR作成が一時的なAPI障害等で失敗しただけの
            # ケースを誤って完了扱いしないため）。
            if not self._verify_already_integrated(ctx):
                return {"status": ctx.status}
        else:
            try:
                ctx.forge.merge_pull_request(ctx.integration_pr_number)
            except Exception as error:
                print(
                    "Warning: Failed to auto-merge integration PR "
                    f"#{ctx.integration_pr_number}: {error}",
                    file=sys.stderr,
                )
                self._comment_on_merge_failure(ctx, error)
                return {
                    "status": IntegrationStatus.AUTO_MERGE_FAILED,
                    "error": str(error),
                    "auto_merged": False,
                }

        newly_included = _mark_tasks_included(ctx)
        ctx.newly_included = newly_included

        closed_issues = self._close_merged_child_issues(ctx)
        return {
            "status": IntegrationStatus.SUCCESS,
            "auto_merged": ctx.integration_pr_number is not None,
            "closed_issues": closed_issues,
            "newly_included": newly_included,
        }

    def _verify_already_integrated(self, ctx: IntegrationContext) -> bool:
        """`ctx.merged_tasks`の全ブランチが実際に`base_branch`へ含まれている
        ことを確認できた場合のみ`True`を返す（1件でも未検証ならfail closed）。"""
        base_branch_name = ctx.base_branch.removeprefix("origin/")
        task_by_subtask_id = {
            task.subtask_id: task for task in ctx.active_done_tasks if task.subtask_id
        }
        for subtask_id in ctx.merged_tasks:
            task = task_by_subtask_id.get(subtask_id)
            if task is None:
                return False
            branch_name = f"claude/issue-{task.issue_number}-{task.subtask_id}"
            try:
                if not ctx.forge.is_current_branch_tip_merged_into(
                    branch_name, base_branch_name
                ):
                    return False
            except Exception as error:
                print(
                    "Warning: Failed to verify whether "
                    f"{branch_name} was already integrated into "
                    f"{base_branch_name}: {error}",
                    file=sys.stderr,
                )
                return False
        return True

    def _comment_on_merge_failure(
        self, ctx: IntegrationContext, error: Exception
    ) -> None:
        task_by_subtask_id = {
            task.subtask_id: task for task in ctx.active_done_tasks if task.subtask_id
        }
        for subtask_id in ctx.merged_tasks:
            task = task_by_subtask_id.get(subtask_id)
            if task is None:
                continue
            try:
                ctx.forge.add_comment(
                    task.issue_number,
                    (
                        "Integratorによる統合PR "
                        f"#{ctx.integration_pr_number} の自動マージに失敗しました"
                        f"（{error}）。次回のディスパッチサイクルで自動的に"
                        "再試行されます。ブランチ保護や権限設定に起因する場合は、"
                        "人間による確認が必要です。"
                    ),
                )
            except Exception as comment_error:
                print(
                    "Warning: Failed to comment on merge failure for issue "
                    f"#{task.issue_number}: {comment_error}",
                    file=sys.stderr,
                )

    def _close_merged_child_issues(self, ctx: IntegrationContext) -> list[int]:
        closed_issues: list[int] = []
        task_by_subtask_id = {
            task.subtask_id: task for task in ctx.active_done_tasks if task.subtask_id
        }
        for subtask_id in ctx.merged_tasks:
            task = task_by_subtask_id.get(subtask_id)
            if task is None:
                continue
            try:
                ctx.forge.close_issue(
                    task.issue_number,
                    "completed",
                    comment=(
                        "Integratorが親ブランチへの自動マージを完了したため、"
                        "このIssueを自動的にクローズしました。"
                    ),
                )
                closed_issues.append(task.issue_number)
            except Exception as error:
                print(
                    f"Warning: Failed to close issue #{task.issue_number} "
                    f"after auto-merge: {error}",
                    file=sys.stderr,
                )
        return closed_issues


__all__ = [
    "AutoMergeChildIntegrationStep",
    "EnsureIntegrationPrStep",
    "LabelIncludedStep",
    "MergeAndTestStep",
    "PrepareTasksStep",
    "PushTempBranchStep",
    "RetryChildIssueCloseStep",
    "SemanticReviewStep",
    "SetupWorktreeStep",
]
