"""Concrete steps executed by the integration pipeline.

The orchestration primitives and shared context live in :mod:`orchestune.integrator`.
Keeping the concrete operations here makes the dependency direction explicit: steps
depend on the pipeline contract, while the integrator loads its default steps lazily.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

from orchestune.dispatch_escalation import apply_human_review_escalation
from orchestune.dispatch_gc_git import prune_stale_integration_temp_branches
from orchestune.dispatch_worktree import file_lock
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
from orchestune.process_utils import default_ci_command


@contextmanager
def _retry_file_lock(lock_path, attempts: int = 3) -> Iterator[None]:
    """短時間の共有git操作に限り、競合ロックを少数回リトライする。"""
    lock = None
    last_error: RuntimeError | None = None
    for attempt in range(attempts):
        candidate = file_lock(lock_path)
        try:
            candidate.__enter__()
        except RuntimeError as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(0.05 * (attempt + 1))
            continue
        lock = candidate
        break
    if lock is None:
        assert last_error is not None
        raise last_error

    try:
        yield
    except BaseException as error:
        lock.__exit__(type(error), error, error.__traceback__)
        raise
    else:
        lock.__exit__(None, None, None)


class PrepareTasksStep(IntegrationComponent):
    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        sorted_done_tasks, ctx.unparsable_done_tasks = get_sorted_done_tasks(
            ctx.config.parent_issue_number,
            forge=ctx.config.forge,
            ignore_patterns=ctx.config.dag_ignore_patterns,
            threshold=ctx.config.dag_similarity_threshold,
        )
        self._warn_and_flag_unparsable_done_tasks(ctx)

        ctx.active_done_tasks = [
            task
            for task in sorted_done_tasks
            if task.issue_state != "CLOSED"
            and task.parent_state != "CLOSED"
            # #437: 親branch更新の連続陳腐化でstatus:blocked-human-reviewへ
            # エスカレーション済みのタスクは、人間の確認が入るまで統合対象から
            # 除外する（そうしないと同じ陳腐化を毎サイクル無限に繰り返す）。
            and "status:blocked-human-review" not in task.status_labels
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
            worktree_manager = IntegrationWorktree(ctx.original_root, ctx.temp_branch)
            # GCだけを全integration runで短時間直列化する。CIやタスクmergeは
            # run固有worktree上で行うため、このロックの外で並行実行できる。
            try:
                with _retry_file_lock(worktree_manager.gc_lock_path()):
                    run_git(["worktree", "prune"], cwd=ctx.original_root, check=False)
                    prune_stale_integration_temp_branches(
                        ctx.original_root, forge=ctx.config.forge
                    )
            except RuntimeError as error:
                # GCは孤児の掃除であり、現在のrunの正しさに必須ではない。
                # 競合時は統合全体を失敗させず、次回に回収を委ねる。
                print(
                    f"Warning: Skipping integration GC due to lock contention: {error}",
                    file=sys.stderr,
                )

            with file_lock(worktree_manager.lock_path()):
                if ctx.config.parent_issue_number is not None:
                    base_name = ctx.base_branch.removeprefix("origin/")
                    with _retry_file_lock(
                        worktree_manager.base_ref_lock_path(ctx.base_branch)
                    ):
                        run_git(
                            [
                                "fetch",
                                "origin",
                                f"{base_name}:refs/remotes/origin/{base_name}",
                            ],
                            cwd=ctx.original_root,
                            check=True,
                        )
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
            ci_command=ctx.config.ci_command or default_ci_command(),
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


#: pushが拒否されたことを示すgitの標準的な文言。認証エラーやネットワーク障害等の
#: 他のpush失敗と区別し、実際のnon-fast-forward（CAS拒否）だけを陳腐化として
#: 検知するために使う。"fetch first"は、fetchしていない状態で先に別のpushが
#: 入った場合にgitが出すバリエーション。
_CAS_REJECTION_MARKERS = ("non-fast-forward", "[rejected]", "fetch first")

#: 親Issueに付与し、直前サイクルでも親branch pushがnon-fast-forward拒否されて
#: いたことを示すマーカーラベル。#437レビュー対応: このカウントをローカルの
#: state fileへ永続化すると、GitHub Actionsのスケジュール実行ではジョブごとに
#: 新しいランナー（`actions/checkout`）を使うため、ファイルがサイクルをまたいで
#: 残らずリトライ判定が機能しない。GitHub上のラベルという、ランナーをまたいでも
#: 失われない場所に状態を置くことで、この問題自体を発生させない。
_PARENT_BRANCH_STALE_LABEL = "integration:parent-branch-stale"


def _is_parent_branch_cas_rejection(error: Exception) -> bool:
    """#437レビュー対応: `error`が実際のnon-fast-forward（CAS）拒否によるpush
    失敗かどうかを判定する。認証エラー・ネットワーク障害・ブランチ保護拒否等の
    他の失敗要因まで陳腐化としてカウント・エスカレーションしてしまうと、一時
    障害が解消してもエスカレーション済みの子Issueが自己修復できなくなるため、
    ここで厳密に絞り込む。"""
    if not isinstance(error, subprocess.CalledProcessError):
        return False
    stderr = error.stderr
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    text = stderr or ""
    return any(marker in text for marker in _CAS_REJECTION_MARKERS)


class AutoMergeChildIntegrationStep(IntegrationComponent):
    """non-force pushで子統合を親へ確定し、最終mainマージは人に委ねる。"""

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
                base_branch = ctx.base_branch.removeprefix("origin/")
                # #435: 通常pushは親branchが進んだ場合にnon-fast-forwardで拒否
                # される。--forceを使わないため古いCI結果で上書きしない。
                run_git(
                    ["push", "origin", f"HEAD:refs/heads/{base_branch}"],
                    cwd=ctx.repository_root,
                    check=True,
                )
            except (subprocess.CalledProcessError, OSError) as error:
                print(
                    "Warning: Failed to update parent branch for integration PR "
                    f"#{ctx.integration_pr_number}: {error}",
                    file=sys.stderr,
                )
                self._comment_on_merge_failure(ctx, error)
                # #437レビュー対応: 認証エラー・ネットワーク障害・ブランチ保護
                # 拒否等の一時的なpush失敗まで無条件に「陳腐化」としてカウント
                # すると、一時障害が解消してもエスカレーション済みの子Issueが
                # 手動でのラベル除去なしに自己修復できなくなる。実際に
                # non-fast-forward（CAS拒否）だったpush失敗のみを陳腐化として
                # 記録・カウントする。
                if _is_parent_branch_cas_rejection(error):
                    self._handle_parent_branch_staleness(ctx, error)
                return {
                    "status": IntegrationStatus.PARENT_BRANCH_ADVANCED,
                    "error": str(error),
                    "auto_merged": False,
                }

        # #437: 親branchの更新（またはその検証）に成功したため、陳腐化マーカー
        # ラベルをクリアする（「連続」失敗の検知であり累積総数ではないため）。
        # ラベルが付いていない場合`remove_label`は無害な no-op（#381の
        # `transition_status_label`と同じ前提）なので、事前の存在確認は不要。
        if ctx.config.parent_issue_number is not None:
            try:
                ctx.forge.remove_label(
                    ctx.config.parent_issue_number, _PARENT_BRANCH_STALE_LABEL
                )
            except Exception as label_error:
                print(
                    "Warning: Failed to clear parent-branch-stale label on "
                    f"parent issue #{ctx.config.parent_issue_number}: {label_error}",
                    file=sys.stderr,
                )

        # 不要になったリモートブランチをクリーンアップ
        task_by_subtask_id = {
            task.subtask_id: task for task in ctx.active_done_tasks if task.subtask_id
        }
        branches_to_delete = []
        for subtask_id in ctx.merged_tasks:
            task = task_by_subtask_id.get(subtask_id)
            if task is not None:
                branches_to_delete.append(
                    f"claude/issue-{task.issue_number}-{task.subtask_id}"
                )
        if ctx.temp_branch:
            branches_to_delete.append(ctx.temp_branch)

        for branch_name in branches_to_delete:
            try:
                if ctx.forge.branch_exists(branch_name):
                    ctx.forge.delete_branch(branch_name)
            except Exception as error:
                print(
                    f"Warning: Failed to delete remote branch '{branch_name}': {error}",
                    file=sys.stderr,
                )

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
                        f"#{ctx.integration_pr_number} の親ブランチ更新に失敗しました"
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

    def _handle_parent_branch_staleness(
        self, ctx: IntegrationContext, error: Exception
    ) -> None:
        """#437: 個々の子Issueへのコメントに加え、親Issue側にも陳腐化イベントを
        記録する。子Issueを1件ずつ追わなくても、親Issueだけを見れば衝突が発生
        した事実を事後に説明できるようにするため。実際にnon-fast-forward
        （CAS拒否）と判定できた場合のみ呼び出される。

        直前サイクルでも同じ陳腐化が起きていたか（＝2サイクル連続）を、
        `_PARENT_BRANCH_STALE_LABEL`マーカーラベルの有無で判定する。#437
        レビュー対応: ローカルのstate fileへ回数を永続化する設計だと、GitHub
        Actionsのスケジュール実行ではジョブごとに新しいランナーを使うため
        サイクルをまたいでファイルが残らず、判定が機能しない。GitHub上の
        ラベルという、ランナーをまたいでも失われない場所に状態を置くことで
        この問題を回避する。2サイクル連続で検知した場合のみ、対象の子Issueを
        `status:blocked-human-review`へエスカレーションする。"""
        if ctx.config.parent_issue_number is None:
            return

        try:
            ctx.forge.add_comment(
                ctx.config.parent_issue_number,
                (
                    "⚠️ 親ブランチの陳腐化を検知しました（CAS拒否）\n\n"
                    f"統合PR #{ctx.integration_pr_number} による "
                    f"`{ctx.base_branch.removeprefix('origin/')}` への更新が、"
                    f"第三者による先行pushのためnon-fast-forwardで拒否されました"
                    f"（{error}）。親ブランチへの部分マージは残っていません。"
                    "次回のディスパッチサイクルで自動的に再試行されます。"
                ),
            )
        except Exception as comment_error:
            print(
                "Warning: Failed to comment on merge failure for parent issue "
                f"#{ctx.config.parent_issue_number}: {comment_error}",
                file=sys.stderr,
            )

        try:
            already_stale = _PARENT_BRANCH_STALE_LABEL in ctx.forge.get_issue_labels(
                ctx.config.parent_issue_number
            )
        except Exception as label_error:
            print(
                "Warning: Failed to read labels for parent issue "
                f"#{ctx.config.parent_issue_number}: {label_error}",
                file=sys.stderr,
            )
            return

        if not already_stale:
            try:
                ctx.forge.add_label(
                    ctx.config.parent_issue_number, _PARENT_BRANCH_STALE_LABEL
                )
            except Exception as label_error:
                print(
                    "Warning: Failed to mark parent issue "
                    f"#{ctx.config.parent_issue_number} as parent-branch-stale: "
                    f"{label_error}",
                    file=sys.stderr,
                )
            return

        # 2サイクル連続で陳腐化 → 設定/運用構成の異常の可能性が高いため、
        # 対象の子Issueをエスカレーションする。
        try:
            ctx.forge.remove_label(
                ctx.config.parent_issue_number, _PARENT_BRANCH_STALE_LABEL
            )
        except Exception as label_error:
            print(
                "Warning: Failed to clear parent-branch-stale label on "
                f"parent issue #{ctx.config.parent_issue_number}: {label_error}",
                file=sys.stderr,
            )

        task_by_subtask_id = {
            task.subtask_id: task for task in ctx.active_done_tasks if task.subtask_id
        }
        for subtask_id in ctx.merged_tasks:
            task = task_by_subtask_id.get(subtask_id)
            if task is None:
                continue
            try:
                apply_human_review_escalation(
                    task.issue_number,
                    task.status_labels,
                    (
                        "親ブランチの陳腐化（CAS拒否）が2サイクル連続で"
                        "発生したため、`status:blocked-human-review`へ"
                        "エスカレーションしました。設定または運用構成の異常"
                        "（例: 意図しない第三者による親ブランチへの書き込み）の"
                        "可能性があるため、人間による確認が必要です。"
                    ),
                    forge=ctx.forge,
                )
            except Exception as escalation_error:
                print(
                    "Warning: Failed to escalate issue "
                    f"#{task.issue_number} to status:blocked-human-review: "
                    f"{escalation_error}",
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
