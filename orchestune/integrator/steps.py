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

from orchestune.dispatch.escalation import apply_human_review_escalation
from orchestune.dispatch.gc.git import prune_stale_integration_temp_branches
from orchestune.dispatch.worktree import file_lock
from orchestune.forge import REQUIRED_LABELS
from orchestune.infra.git_cli import run_git
from orchestune.infra.process_utils import default_ci_command
from orchestune.integrator.git_ops import IntegrationMerger
from orchestune.integrator.pr import ensure_integration_pr
from orchestune.integrator.tasks import get_sorted_done_tasks
from orchestune.integrator.types import (
    IntegrationComponent,
    IntegrationContext,
    IntegrationReport,
    IntegrationStatus,
)
from orchestune.integrator.worktree import IntegrationWorktree
from orchestune.pr_link_notice import (
    ensure_pr_merged_notice,
    render_merged_notice,
)


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

        # #437レビュー対応: status:blocked-human-reviewエスカレーション済みの
        # タスクは、ここで`active_done_tasks`から完全に除外してはいけない。
        # 除外すると、そのタスクに依存する後続タスク（特にスタッキングにより
        # 既にその未マージのコミットを含んだブランチを持つ後続タスク）を
        # ブロックする既存の推移的依存判定（`IntegrationMerger.merge_and_test_tasks`
        # の`unavailable_ids`）が、除外されたタスクの存在自体を認識できず
        # 素通りしてしまう。人間の確認待ちのタスクをマージ対象から外す処理
        # 自体は、この依存判定と同じ場所（`merge_and_test_tasks`）で行い、
        # 依存元・依存先を通して一貫してブロックされるようにする。
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
            self._prepare_worktree(ctx)
        except (subprocess.CalledProcessError, OSError, RuntimeError) as error:
            return {
                "status": IntegrationStatus.FAILED_TO_CREATE_TEMP_WORKTREE,
                "error": f"Failed to create temp worktree: {error}",
            }
        return {"status": IntegrationStatus.SUCCESS}

    def _prepare_worktree(self, ctx: IntegrationContext) -> None:
        manager = IntegrationWorktree(ctx.original_root, ctx.temp_branch)
        self._run_garbage_collection(manager, ctx)
        with file_lock(manager.lock_path()):
            self._fetch_parent_base_if_needed(manager, ctx)
            manager.reclaim(manager.temp_path())
            run_git(
                ["worktree", "add", str(manager.temp_path()), ctx.base_branch],
                cwd=ctx.original_root,
                check=True,
            )
        ctx.repository_root = manager.temp_path()
        ctx.config.repository_root = manager.temp_path()
        ctx.temp_worktree_path = manager.temp_path()

    @staticmethod
    def _run_garbage_collection(
        manager: IntegrationWorktree, ctx: IntegrationContext
    ) -> None:
        try:
            with _retry_file_lock(manager.gc_lock_path()):
                run_git(["worktree", "prune"], cwd=ctx.original_root, check=False)
                prune_stale_integration_temp_branches(
                    ctx.original_root, forge=ctx.config.forge
                )
        except RuntimeError as error:
            print(
                f"Warning: Skipping integration GC due to lock contention: {error}",
                file=sys.stderr,
            )

    @staticmethod
    def _fetch_parent_base_if_needed(
        manager: IntegrationWorktree, ctx: IntegrationContext
    ) -> None:
        if ctx.config.parent_issue_number is None:
            return
        base_name = ctx.base_branch.removeprefix("origin/")
        with _retry_file_lock(manager.base_ref_lock_path(ctx.base_branch)):
            run_git(
                ["fetch", "origin", f"{base_name}:refs/remotes/origin/{base_name}"],
                cwd=ctx.original_root,
                check=True,
            )


class MergeAndTestStep(IntegrationComponent):
    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        try:
            merger = self._new_merger(ctx)
            if not merger.create_temp_branch(
                ctx.temp_branch, ctx.base_branch, ctx.config.apply
            ):
                return {
                    "status": IntegrationStatus.FAILED_TO_CREATE_TEMP_BRANCH,
                    "error": "Failed to create temp branch",
                }

            results = merger.merge_and_test_tasks(
                ctx.active_done_tasks, ctx.base_branch, ctx.config.apply
            )
            return self._record_merge_results(ctx, results)
        except Exception as error:
            return {
                "status": IntegrationStatus.FAILURE,
                "error": f"Error during merge and test: {error}",
            }

    @staticmethod
    def _new_merger(ctx: IntegrationContext) -> IntegrationMerger:
        return IntegrationMerger(
            ctx.repository_root,
            ctx.original_root,
            ctx.config.ci_command or default_ci_command(),
            ctx.config.forge,
        )

    @staticmethod
    def _record_merge_results(
        ctx: IntegrationContext,
        results: tuple[list[str], list[str], list[str], dict[str, str], dict[str, str]],
    ) -> IntegrationReport:
        merged, failed, blocked, failed_reasons, blocked_reasons = results
        ctx.merged_tasks.extend(merged)
        ctx.failed_tasks.extend(failed)
        ctx.blocked_tasks.extend(blocked)
        ctx.failed_reasons.update(failed_reasons)
        ctx.blocked_reasons.update(blocked_reasons)
        if not failed and merged:
            return {"status": IntegrationStatus.SUCCESS}
        return {
            "status": IntegrationStatus.PARTIAL_SUCCESS
            if merged
            else IntegrationStatus.FAILURE,
            "merged": ctx.merged_tasks,
            "failed": ctx.failed_tasks,
            "failed_reasons": ctx.failed_reasons,
            "blocked": ctx.blocked_tasks,
            "blocked_reasons": ctx.blocked_reasons,
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

#: #437レビュー対応: `orchestune bootstrap`は`ensure_labels`の唯一の呼び出し元
#: であり、`orchestune dispatch`からは自動的に再実行されない。そのため、この
#: リリース以前に一度bootstrap済みのリポジトリには`_PARENT_BRANCH_STALE_LABEL`
#: がまだ存在せず、`add_label`がリポジトリ側にラベルが無いことを理由に失敗し、
#: マーカーが一切永続化されずエスカレーション自体が機能しなくなる。初回付与の
#: 直前で`ensure_labels`により自己修復的にラベルを用意することでこれを防ぐ。
#: `forge_admin.REQUIRED_LABELS`の定義を単一の情報源として再利用する。
_PARENT_BRANCH_STALE_LABEL_SPEC = next(
    label for label in REQUIRED_LABELS if label.name == _PARENT_BRANCH_STALE_LABEL
)

#: `clear_parent_branch_stale_marker`が`remove_label`を試行する回数。
#: 一時的なAPI障害でマーカーが残置され、後の無関係なCAS拒否を誤って
#: 「2サイクル連続」と誤判定させないための少数回リトライ（#437レビュー対応）。
_CLEAR_STALE_MARKER_ATTEMPTS = 3


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


def clear_parent_branch_stale_marker(ctx: IntegrationContext) -> None:
    """#437: `_PARENT_BRANCH_STALE_LABEL`マーカーを親Issueから除去する。

    #437レビュー対応: このマーカーは「サイクルが実際にCAS拒否で終わったか」
    だけを追う必要があるため、クリア自体は`IntegrationPipeline.execute`
    （`orchestune/integrator.py`）から、パイプラインがどのステップで終わった
    かに関わらず一律に呼ばれる。当初`AutoMergeChildIntegrationStep`内の
    push成功時／non-CAS失敗時にのみクリアしていたが、`SetupWorktreeStep`・
    `MergeAndTestStep`・`PushTempBranchStep`等、このステップに到達する前に
    サイクルが終わるケースを見落としており、直前のCAS拒否のマーカーが古いまま
    残ってしまっていた（レビュー指摘）。CAS拒否を確認したサイクルだけは
    `ctx.parent_branch_cas_rejected_this_cycle`を立てることで、このクリア処理
    をスキップし、マーカーの管理を`_handle_parent_branch_staleness`自身に
    委ねる。

    #437レビュー対応（既知の限度）: この除去はラベルの単純な上書きであり、
    GitHub側にCAS（compare-and-swap）機構が無いため、他ランナーが同時に
    マーカーを新規付与した直後にここが実行されると、その新しいイベントを
    消してしまう可能性が理論上ある。分散ロックを避ける#437自身の設計方針
    （footprintの事前検証により競合率が低い前提で悲観ロックより楽観的並行
    制御を選ぶ）と同じ理由で、ここでも追加のロック機構は導入しない。
    cross-runner競合そのものは、推奨される`concurrency`グループ設定
    （#436, docs/ja/setup.md#6）により実運用上は発生しない前提であり、この
    残存リスクはその設定を行わない場合にのみ顕在化する。

    #437レビュー対応: `remove_label`が一時的なAPI障害で失敗した場合、単に
    警告を出すだけで古いマーカーを残置すると、後で発生する無関係なCAS拒否が
    誤って「2サイクル連続」と判定され誤エスカレーションしうる（読み取り側の
    レース＝上記の既知の限度とは異なり、これは書き込みの単純な失敗であり、
    リトライで解消できる）。少数回・短い間隔でリトライする。
    """
    if ctx.config.parent_issue_number is None:
        return
    last_error: Exception | None = None
    for attempt in range(_CLEAR_STALE_MARKER_ATTEMPTS):
        try:
            ctx.forge.remove_label(
                ctx.config.parent_issue_number, _PARENT_BRANCH_STALE_LABEL
            )
            return
        except Exception as label_error:
            last_error = label_error
            if attempt + 1 < _CLEAR_STALE_MARKER_ATTEMPTS:
                time.sleep(0.05 * (attempt + 1))
    print(
        "Warning: Failed to clear parent-branch-stale label on parent issue "
        f"#{ctx.config.parent_issue_number} after {_CLEAR_STALE_MARKER_ATTEMPTS} "
        f"attempts: {last_error}",
        file=sys.stderr,
    )


class AutoMergeChildIntegrationStep(IntegrationComponent):
    """non-force pushで子統合を親へ確定し、最終mainマージは人に委ねる。"""

    def execute(self, ctx: IntegrationContext) -> IntegrationReport:
        if not ctx.config.apply or ctx.config.parent_issue_number is None:
            return {"status": IntegrationStatus.SUCCESS}
        if ctx.failed_tasks or not ctx.merged_tasks:
            return {"status": ctx.status}
        failure = self._update_parent_branch(ctx)
        if failure is not None:
            return failure
        self._delete_merged_branches(ctx)
        newly_included = _mark_tasks_included(ctx)
        ctx.newly_included = newly_included
        return {
            "status": IntegrationStatus.SUCCESS,
            "auto_merged": ctx.integration_pr_number is not None,
            "closed_issues": self._close_merged_child_issues(ctx),
            "newly_included": newly_included,
        }

    def _update_parent_branch(
        self, ctx: IntegrationContext
    ) -> IntegrationReport | None:
        if ctx.integration_pr_number is None:
            return (
                None if self._verify_already_integrated(ctx) else {"status": ctx.status}
            )
        try:
            base_branch = ctx.base_branch.removeprefix("origin/")
            run_git(
                ["push", "origin", f"HEAD:refs/heads/{base_branch}"],
                cwd=ctx.repository_root,
                check=True,
            )
            return None
        except (subprocess.CalledProcessError, OSError) as error:
            print(
                f"Warning: Failed to update parent branch for integration PR #{ctx.integration_pr_number}: {error}",
                file=sys.stderr,
            )
            self._comment_on_merge_failure(ctx, error)
            if _is_parent_branch_cas_rejection(error):
                ctx.parent_branch_cas_rejected_this_cycle = True
                self._handle_parent_branch_staleness(ctx, error)
            return {
                "status": IntegrationStatus.PARENT_BRANCH_ADVANCED,
                "error": str(error),
                "auto_merged": False,
            }

    @staticmethod
    def _merged_branch_names(ctx: IntegrationContext) -> list[str]:
        tasks = {
            task.subtask_id: task for task in ctx.active_done_tasks if task.subtask_id
        }
        names = [
            f"claude/issue-{tasks[task_id].issue_number}-{task_id}"
            for task_id in ctx.merged_tasks
            if task_id in tasks
        ]
        return names + ([ctx.temp_branch] if ctx.temp_branch else [])

    def _delete_merged_branches(self, ctx: IntegrationContext) -> None:
        for branch_name in self._merged_branch_names(ctx):
            try:
                if ctx.forge.branch_exists(branch_name):
                    ctx.forge.delete_branch(branch_name)
            except Exception as error:
                print(
                    f"Warning: Failed to delete remote branch '{branch_name}': {error}",
                    file=sys.stderr,
                )

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

        self._record_parent_branch_staleness(ctx, error)

        already_stale = self._parent_is_already_stale(ctx)
        if already_stale is None:
            return

        if not already_stale:
            self._mark_parent_branch_stale(ctx)
            return

        # 2サイクル連続で陳腐化 → 設定/運用構成の異常の可能性が高いため、
        # 対象の子Issueをエスカレーションする。
        # #437レビュー対応: マーカーのクリアは、対象の子Issueすべてのエスカレー
        # ションが成功した後にのみ行う。先にクリアしてしまうと、一時的な
        # GitHub API障害等で一部のエスカレーションが失敗した場合、その子Issue
        # は統合対象に残ったままなのにマーカーだけが消え、次のCAS拒否が誤って
        # 「1回目」として扱われて再エスカレーションまでさらに2サイクル分
        # 遅延してしまう。
        task_by_subtask_id = {
            task.subtask_id: task for task in ctx.active_done_tasks if task.subtask_id
        }
        all_escalations_succeeded = True
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
                all_escalations_succeeded = False
                print(
                    "Warning: Failed to escalate issue "
                    f"#{task.issue_number} to status:blocked-human-review: "
                    f"{escalation_error}",
                    file=sys.stderr,
                )

        if all_escalations_succeeded:
            clear_parent_branch_stale_marker(ctx)

    @staticmethod
    def _record_parent_branch_staleness(
        ctx: IntegrationContext, error: Exception
    ) -> None:
        parent_issue_number = ctx.config.parent_issue_number
        if parent_issue_number is None:
            return
        try:
            ctx.forge.add_comment(
                parent_issue_number,
                (
                    "⚠️ 親ブランチの陳腐化を検知しました（CAS拒否）\n\n"
                    f"統合PR #{ctx.integration_pr_number} による `{ctx.base_branch.removeprefix('origin/')}` への更新が、"
                    f"第三者による先行pushのためnon-fast-forwardで拒否されました（{error}）。親ブランチへの部分マージは残っていません。次回のディスパッチサイクルで自動的に再試行されます。"
                ),
            )
        except Exception as comment_error:
            print(
                f"Warning: Failed to comment on merge failure for parent issue #{ctx.config.parent_issue_number}: {comment_error}",
                file=sys.stderr,
            )

    @staticmethod
    def _parent_is_already_stale(ctx: IntegrationContext) -> bool | None:
        parent_issue_number = ctx.config.parent_issue_number
        if parent_issue_number is None:
            return None
        try:
            return _PARENT_BRANCH_STALE_LABEL in ctx.forge.get_issue_labels(
                parent_issue_number
            )
        except Exception as label_error:
            print(
                f"Warning: Failed to read labels for parent issue #{ctx.config.parent_issue_number}: {label_error}",
                file=sys.stderr,
            )
            return None

    @staticmethod
    def _mark_parent_branch_stale(ctx: IntegrationContext) -> None:
        parent_issue_number = ctx.config.parent_issue_number
        if parent_issue_number is None:
            return
        try:
            ctx.forge.ensure_labels((_PARENT_BRANCH_STALE_LABEL_SPEC,))
        except Exception as error:
            print(
                f"Warning: Failed to ensure parent-branch-stale label exists: {error}",
                file=sys.stderr,
            )
        try:
            ctx.forge.add_label(parent_issue_number, _PARENT_BRANCH_STALE_LABEL)
        except Exception as error:
            print(
                f"Warning: Failed to mark parent issue #{ctx.config.parent_issue_number} as parent-branch-stale: {error}",
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
                    comment=_close_comment(ctx, task.issue_number),
                )
                closed_issues.append(task.issue_number)
            except Exception as error:
                print(
                    f"Warning: Failed to close issue #{task.issue_number} "
                    f"after auto-merge: {error}",
                    file=sys.stderr,
                )
        return closed_issues


def _close_comment(ctx: IntegrationContext, issue_number: int) -> str | None:
    """#676: マージ完了通知をクローズ前に残し、クローズコメントを決める。

    GitHubは既定ブランチ以外を対象とするPRをIssueの「Development」欄へ自動
    リンクしないため、この通知が子Issue側から親ブランチへのマージを辿る唯一の
    手掛かりになる。PR#684レビュー対応(Codex P2): 通知はクローズコメントに
    同梱せず、クローズ**前**に独立したコメントとして投稿する。同梱すると、
    クローズだけが失敗した場合に次サイクルの`RetryChildIssueCloseStep`が
    （統合PR番号を復元できないまま）汎用コメントでクローズし直し、リンクが
    恒久的に失われる。

    戻り値は`close_issue`へ渡すコメント本文:
    - 通知を残せた場合は`None`（重複を避け、クローズだけを行う）
    - 通知の投稿に失敗した場合は最後の手段として通知本文そのもの
    - リンクすべき統合PRが無い場合は従来どおりの文面
    """
    base_branch = ctx.base_branch.removeprefix("origin/")
    if ctx.integration_pr_number is None:
        return (
            "Integratorが親ブランチへの自動マージを完了したため、"
            "このIssueを自動的にクローズしました。"
        )
    if ensure_pr_merged_notice(
        ctx.forge, issue_number, ctx.integration_pr_number, base_branch
    ):
        return None
    return render_merged_notice(ctx.integration_pr_number, base_branch)


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
