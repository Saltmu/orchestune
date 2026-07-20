from __future__ import annotations

import copy
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from orchestune import github
from orchestune.dispatcher import Task, file_lock
from orchestune.integration_coordinator import IntegrationCoordinator
from orchestune.integrator_git_ops import IntegrationMerger
from orchestune.integrator_pr import ensure_integration_pr
from orchestune.integrator_tasks import get_sorted_done_tasks
from orchestune.integrator_worktree import IntegrationWorktree


@dataclass
class IntegratorConfig:
    repository_root: Path = Path(".")
    base_branch: str = "origin/main"
    temp_branch: str = "integration/temp-main"
    ci_command: list[str] | None = None
    parent_issue_number: int | None = None
    apply: bool = False
    # Integratorの仕事は「統合ブランチ(temp_branch)を作成しCI通過を確認した上で、
    # base_branchへのPRを作成/再利用する」までに限定される。最終マージは常に人間が行う。
    # 意味的レビュー（LLMによる統合diff of バグ検知）は`coordinator`が注入されている場合のみ
    # 実行され、結果は統合PRへのコメントとして残るのみで、自動マージ等は一切行わない。
    enable_semantic_review: bool = True
    coordinator: IntegrationCoordinator | None = None

    def __post_init__(self) -> None:
        if self.parent_issue_number is not None:
            self.base_branch = f"origin/parent/issue-{self.parent_issue_number}"
            self.temp_branch = (
                f"integration/temp-parent-issue-{self.parent_issue_number}"
            )


@dataclass
class IntegrationContext:
    config: IntegratorConfig
    repository_root: Path
    original_root: Path
    base_branch: str
    temp_branch: str

    # 処理中の共有状態
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

    # 全体結果ステータス
    status: str = "success"
    error: str | None = None


class IntegrationComponent(ABC):
    @abstractmethod
    def execute(self, ctx: IntegrationContext) -> dict:
        pass


class IntegrationPipeline(IntegrationComponent):
    def __init__(self, steps: list[IntegrationComponent]):
        self.steps = steps

    def execute(self, ctx: IntegrationContext) -> dict:
        merged_report = {}
        try:
            for step in self.steps:
                res = step.execute(ctx)
                merged_report.update(res)

                if "status" in res:
                    ctx.status = res["status"]
                if "error" in res:
                    ctx.error = res["error"]

                # success以外（failure, partial_success, no_done_tasksなど）の場合は処理を中断
                if ctx.status != "success":
                    break
        finally:
            if ctx.temp_worktree_path:
                try:
                    subprocess.run(
                        [
                            "git",
                            "worktree",
                            "remove",
                            "--force",
                            str(ctx.temp_worktree_path),
                        ],
                        cwd=str(ctx.original_root),
                        capture_output=True,
                        check=True,
                    )
                except Exception:
                    pass

        # 各ステップが返した辞書の内容をベースに、最終結果を集約
        final_report = copy.deepcopy(merged_report)
        final_report["status"] = ctx.status
        final_report["merged"] = ctx.merged_tasks

        if ctx.status == "success":
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
                t.issue_number for t in ctx.unparsable_done_tasks
            ]
        return final_report


class MultiIssueIntegrator(IntegrationComponent):
    def __init__(self, integrators: list[IntegrationComponent]):
        self.integrators = integrators

    def execute(self, ctx: IntegrationContext) -> dict:
        details = {}
        success_count = 0
        failure_count = 0

        for integrator in self.integrators:
            sub_ctx = copy.deepcopy(ctx)
            parent_issue = getattr(integrator, "parent_issue", None)
            key = (
                f"issue_{parent_issue}"
                if parent_issue is not None
                else f"integrator_{id(integrator)}"
            )
            res = integrator.execute(sub_ctx)
            details[key] = res

            status = res.get("status")
            if status in ("success", "no_done_tasks"):
                success_count += 1
            else:
                failure_count += 1

        if success_count > 0 and failure_count == 0:
            overall_status = "composite_success"
        elif success_count > 0 and failure_count > 0:
            overall_status = "composite_partial_success"
        else:
            overall_status = "composite_failure"

        if not self.integrators:
            overall_status = "composite_success"

        return {
            "status": overall_status,
            "details": details,
        }


class SingleIssueIntegrator(IntegrationComponent):
    def __init__(self, parent_issue: int | None, pipeline: IntegrationComponent):
        self.parent_issue = parent_issue
        self.pipeline = pipeline

    def execute(self, ctx: IntegrationContext) -> dict:
        if self.parent_issue is not None:
            ctx.config.parent_issue_number = self.parent_issue
            ctx.base_branch = f"origin/parent/issue-{self.parent_issue}"
            ctx.temp_branch = f"integration/temp-parent-issue-{self.parent_issue}"

        if not ctx.config.apply:
            return self.pipeline.execute(ctx)

        worktree_manager = IntegrationWorktree(ctx.original_root, ctx.temp_branch)
        lock_path = worktree_manager.lock_path()
        try:
            with file_lock(lock_path):
                return self.pipeline.execute(ctx)
        except RuntimeError as e:
            return {
                "status": "integration_branch_locked",
                "error": str(e),
            }


class PrepareTasksStep(IntegrationComponent):
    def execute(self, ctx: IntegrationContext) -> dict:
        sorted_done_tasks, ctx.unparsable_done_tasks = get_sorted_done_tasks(
            ctx.config.parent_issue_number
        )
        self._warn_and_flag_unparsable_done_tasks(ctx)

        ctx.active_done_tasks = [
            t
            for t in sorted_done_tasks
            if t.issue_state != "CLOSED" and t.parent_state != "CLOSED"
        ]

        if not ctx.active_done_tasks:
            return {"status": "no_done_tasks"}

        return {"status": "success"}

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
                    github.add_comment(
                        task.issue_number,
                        "Integratorは、このIssueのFootprint YAMLブロックから"
                        "`subtask_id`を抽出できなかったため、統合対象から除外しました。\n"
                        "Issue本文のFootprintブロックを確認し、`subtask_id`を修正してください。",
                    )
                except Exception as e:
                    print(
                        "Warning: Failed to comment on unparsable done issue "
                        f"#{task.issue_number}: {e}",
                        file=sys.stderr,
                    )


class RetryChildIssueCloseStep(IntegrationComponent):
    """#170レビュー対応: `integration:included`は`AutoMergeChildIntegrationStep`が
    マージ成功後にのみ付与するようになったため、「マージ済みだがクローズには
    失敗した」子Issueを検知する信頼できるシグナルとして再利用できる。

    マージ成功・クローズ失敗のケースでは、対象の子ブランチは既に`base_branch`へ
    取り込まれているため、次サイクルの統合PR作成は差分無しで失敗し、通常の
    マージ経路では二度とクローズが再試行されない。本ステップはその独立した
    回復パスとして、`PrepareTasksStep`の直後に実行する。
    """

    def execute(self, ctx: IntegrationContext) -> dict:
        if not ctx.config.apply or ctx.config.parent_issue_number is None:
            return {"status": "success"}

        remaining_tasks = []
        retried_closed: list[int] = []
        for task in ctx.active_done_tasks:
            if "integration:included" not in task.status_labels:
                remaining_tasks.append(task)
                continue
            try:
                github.close_issue(
                    task.issue_number,
                    "completed",
                    comment=(
                        "Integratorが親ブランチへの自動マージを完了済みですが、"
                        "前回のクローズ処理に失敗していたため再試行しました。"
                    ),
                )
                retried_closed.append(task.issue_number)
            except Exception as e:
                print(
                    "Warning: Failed to retry closing issue "
                    f"#{task.issue_number}: {e}",
                    file=sys.stderr,
                )
                remaining_tasks.append(task)

        ctx.active_done_tasks = remaining_tasks
        if not ctx.active_done_tasks:
            return {"status": "no_done_tasks", "retried_closed_issues": retried_closed}
        return {"status": "success", "retried_closed_issues": retried_closed}


class SetupWorktreeStep(IntegrationComponent):
    def execute(self, ctx: IntegrationContext) -> dict:
        if not ctx.config.apply:
            return {"status": "success"}

        try:
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=str(ctx.original_root),
                capture_output=True,
            )

            worktree_manager = IntegrationWorktree(ctx.original_root, ctx.temp_branch)
            worktree_manager.reclaim(worktree_manager.temp_path())

            subprocess.run(
                [
                    "git",
                    "worktree",
                    "add",
                    str(worktree_manager.temp_path()),
                    ctx.base_branch,
                ],
                cwd=str(ctx.original_root),
                check=True,
                capture_output=True,
            )
            ctx.repository_root = worktree_manager.temp_path()
            ctx.config.repository_root = worktree_manager.temp_path()
            ctx.temp_worktree_path = worktree_manager.temp_path()
        except (subprocess.CalledProcessError, OSError, RuntimeError) as e:
            return {
                "status": "failed_to_create_temp_worktree",
                "error": f"Failed to create temp worktree: {e}",
            }
        return {"status": "success"}


class MergeAndTestStep(IntegrationComponent):
    def execute(self, ctx: IntegrationContext) -> dict:
        merger = IntegrationMerger(
            repository_root=ctx.repository_root,
            original_root=ctx.original_root,
            ci_command=ctx.config.ci_command or ["./scripts/local-ci.sh"],
        )

        try:
            if not merger.create_temp_branch(
                ctx.temp_branch, ctx.base_branch, ctx.config.apply
            ):
                return {
                    "status": "failed_to_create_temp_branch",
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
                return {"status": "success"}

            status = "partial_success" if merged_tasks else "failure"
            return {
                "status": status,
                "merged": ctx.merged_tasks,
                "failed": ctx.failed_tasks,
                "failed_reasons": ctx.failed_reasons,
                "blocked": ctx.blocked_tasks,
                "blocked_reasons": ctx.blocked_reasons,
            }
        except Exception as e:
            return {
                "status": "failure",
                "error": f"Error during merge and test: {e}",
            }


class PushTempBranchStep(IntegrationComponent):
    def execute(self, ctx: IntegrationContext) -> dict:
        if not ctx.config.apply:
            return {"status": "success"}
        if ctx.failed_tasks or not ctx.merged_tasks:
            return {"status": ctx.status}

        try:
            subprocess.run(
                [
                    "git",
                    "push",
                    "--force",
                    "origin",
                    ctx.temp_branch,
                ],
                cwd=str(ctx.repository_root),
                check=True,
                capture_output=True,
            )
            return {"status": "success"}
        except subprocess.CalledProcessError as pe:
            push_error = (pe.stderr or b"").decode(errors="replace")
            print(
                f"Failed to push temp branch: {push_error}",
                file=sys.stderr,
            )
            return {
                "status": "failed_to_push_temp_branch",
                "error": push_error,
            }


class EnsureIntegrationPrStep(IntegrationComponent):
    def execute(self, ctx: IntegrationContext) -> dict:
        if not ctx.config.apply:
            return {"status": "success"}
        if (
            ctx.status == "failed_to_push_temp_branch"
            or ctx.failed_tasks
            or not ctx.merged_tasks
        ):
            return {"status": ctx.status}

        try:
            pr_number = ensure_integration_pr(
                ctx.temp_branch, ctx.base_branch, ctx.merged_tasks
            )
            ctx.integration_pr_number = pr_number
            return {"status": "success", "integration_pr_number": pr_number}
        except Exception as e:
            print(f"Warning: failed to ensure integration PR: {e}", file=sys.stderr)
            return {"status": "success", "integration_pr_number": None}


class SemanticReviewStep(IntegrationComponent):
    def execute(self, ctx: IntegrationContext) -> dict:
        if not ctx.config.apply:
            return {"status": "success"}
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
                return {"status": "success", "semantic_review_dispatched": True}
            except Exception as e:
                print(
                    f"Warning: Failed to dispatch semantic review: {e}",
                    file=sys.stderr,
                )
        return {"status": "success", "semantic_review_dispatched": False}


class LabelIncludedStep(IntegrationComponent):
    def execute(self, ctx: IntegrationContext) -> dict:
        if not ctx.config.apply:
            return {"status": "success"}
        if ctx.config.parent_issue_number is not None:
            # #209: parent_issue_number指定時（自動マージ経路）は、
            # AutoMergeChildIntegrationStepがマージ成功直後・クローズ試行前に
            # 既に付与済みのため、ここでは何もしない（二重付与の回避）。
            return {"status": "success", "newly_included": ctx.newly_included}
        if (
            ctx.failed_tasks
            or not ctx.merged_tasks
            or ctx.integration_pr_number is None
        ):
            return {"status": ctx.status}

        newly_included = _mark_tasks_included(ctx)
        ctx.newly_included = newly_included
        return {"status": "success", "newly_included": newly_included}


def _mark_tasks_included(ctx: IntegrationContext) -> list[str]:
    """`ctx.merged_tasks`のうち、まだ`integration:included`ラベルを持たない
    タスクへラベルを付与する。`AutoMergeChildIntegrationStep`と
    `LabelIncludedStep`の両方から呼び出される共通ロジック。"""
    newly_included: list[str] = []
    task_by_subtask_id = {
        t.subtask_id: t for t in ctx.active_done_tasks if t.subtask_id
    }
    for subtask_id in ctx.merged_tasks:
        task = task_by_subtask_id.get(subtask_id)
        if task is None or "integration:included" in task.status_labels:
            continue
        try:
            github.add_label(task.issue_number, "integration:included")
            newly_included.append(subtask_id)
        except Exception as e:
            print(
                "Warning: Failed to add integration:included label to "
                f"issue #{task.issue_number}: {e}",
                file=sys.stderr,
            )
    return newly_included


class AutoMergeChildIntegrationStep(IntegrationComponent):
    """#170: `parent_issue_number`指定時（子ブランチ→親ブランチの統合）のみ、
    CI通過済みの統合PRを人間の確認を待たずに自動マージし、対象の子Issueを
    自動クローズする。`parent_issue_number`が未指定（base_branch=main、＝
    親ブランチ→mainの最終マージに相当するケース）では何もしない。最終マージは
    引き続き人間が行う。
    """

    def execute(self, ctx: IntegrationContext) -> dict:
        if not ctx.config.apply or ctx.config.parent_issue_number is None:
            return {"status": "success"}
        if (
            ctx.failed_tasks
            or not ctx.merged_tasks
            or ctx.integration_pr_number is None
        ):
            return {"status": ctx.status}

        try:
            github.merge_pull_request(ctx.integration_pr_number)
        except Exception as e:
            print(
                "Warning: Failed to auto-merge integration PR "
                f"#{ctx.integration_pr_number}: {e}",
                file=sys.stderr,
            )
            self._comment_on_merge_failure(ctx, e)
            return {
                "status": "auto_merge_failed",
                "error": str(e),
                "auto_merged": False,
            }

        # #209: マージ成功が確定した時点でクローズ試行より前に
        # `integration:included`を記帳する。以後クローズ処理やプロセスが
        # 失敗しても、次サイクルのRetryChildIssueCloseStepがこのラベルを
        # 信頼できる回復シグナルとしてクローズを再試行できる。
        newly_included = _mark_tasks_included(ctx)
        ctx.newly_included = newly_included

        closed_issues = self._close_merged_child_issues(ctx)
        return {
            "status": "success",
            "auto_merged": True,
            "closed_issues": closed_issues,
            "newly_included": newly_included,
        }

    def _comment_on_merge_failure(
        self, ctx: IntegrationContext, error: Exception
    ) -> None:
        task_by_subtask_id = {
            t.subtask_id: t for t in ctx.active_done_tasks if t.subtask_id
        }
        for subtask_id in ctx.merged_tasks:
            task = task_by_subtask_id.get(subtask_id)
            if task is None:
                continue
            try:
                github.add_comment(
                    task.issue_number,
                    (
                        "Integratorによる統合PR "
                        f"#{ctx.integration_pr_number} の自動マージに失敗しました"
                        f"（{error}）。次回のディスパッチサイクルで自動的に"
                        "再試行されます。ブランチ保護や権限設定に起因する場合は、"
                        "人間による確認が必要です。"
                    ),
                )
            except Exception as e:
                print(
                    "Warning: Failed to comment on merge failure for issue "
                    f"#{task.issue_number}: {e}",
                    file=sys.stderr,
                )

    def _close_merged_child_issues(self, ctx: IntegrationContext) -> list[int]:
        closed_issues: list[int] = []
        task_by_subtask_id = {
            t.subtask_id: t for t in ctx.active_done_tasks if t.subtask_id
        }
        for subtask_id in ctx.merged_tasks:
            task = task_by_subtask_id.get(subtask_id)
            if task is None:
                continue
            try:
                github.close_issue(
                    task.issue_number,
                    "completed",
                    comment=(
                        "Integratorが親ブランチへの自動マージを完了したため、"
                        "このIssueを自動的にクローズしました。"
                    ),
                )
                closed_issues.append(task.issue_number)
            except Exception as e:
                print(
                    f"Warning: Failed to close issue #{task.issue_number} "
                    f"after auto-merge: {e}",
                    file=sys.stderr,
                )
        return closed_issues


class Integrator:
    def __init__(self, config: IntegratorConfig):
        self.config = config
        if self.config.ci_command is None:
            self.config.ci_command = ["./scripts/local-ci.sh"]
        self.original_root = Path(self.config.repository_root).resolve()
        self.config.repository_root = self.original_root
        self._worktree = IntegrationWorktree(
            self.original_root, self.config.temp_branch
        )

        # 既存コードが直接参照する可能性のある属性
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

    def run(self) -> dict:
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

        res = runner.execute(ctx)

        # 属性の同期
        self.failed_reasons = ctx.failed_reasons
        self.blocked_reasons = ctx.blocked_reasons
        self.unparsable_done_tasks = ctx.unparsable_done_tasks

        return res
