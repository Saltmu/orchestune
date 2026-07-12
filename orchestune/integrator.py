from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
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


class Integrator:
    def __init__(self, config: IntegratorConfig):
        self.config = config
        if self.config.ci_command is None:
            self.config.ci_command = ["./scripts/local-ci.sh"]
        self.original_root = Path(self.config.repository_root).resolve()
        self.config.repository_root = self.original_root
        self.failed_reasons: dict[str, str] = {}
        self.blocked_reasons: dict[str, str] = {}
        self.unparsable_done_tasks: list[Task] = []
        self._worktree = IntegrationWorktree(
            self.original_root, self.config.temp_branch
        )

    def _worktree_key(self) -> str:
        return self._worktree.key()

    def _temp_worktree_path(self) -> Path:
        return self._worktree.temp_path()

    def _worktree_lock_path(self) -> Path:
        return self._worktree.lock_path()

    def _reclaim_worktree_path(self, path: Path) -> None:
        self._worktree.reclaim(path)

    def run(self) -> dict:
        sorted_done_tasks, self.unparsable_done_tasks = get_sorted_done_tasks(
            self.config.parent_issue_number
        )
        self._warn_and_flag_unparsable_done_tasks()

        active_done_tasks = [
            t
            for t in sorted_done_tasks
            if t.issue_state != "CLOSED" and t.parent_state != "CLOSED"
        ]
        if not active_done_tasks:
            return self._attach_unparsable_info(
                {"status": "no_done_tasks", "merged": []}
            )

        if not self.config.apply:
            return self._attach_unparsable_info(
                self._run_integration(active_done_tasks)
            )

        lock_path = self._worktree_lock_path()
        try:
            with file_lock(lock_path):
                return self._attach_unparsable_info(
                    self._run_integration(active_done_tasks)
                )
        except RuntimeError as e:
            return self._attach_unparsable_info(
                {
                    "status": "integration_branch_locked",
                    "error": str(e),
                }
            )

    def _warn_and_flag_unparsable_done_tasks(self) -> None:
        """#54: Footprint YAMLから`subtask_id`を抽出できなかった`status:done`タスクは、
        マージ対象のIDに紐付けられないため統合できないが、それを警告もエラー報告も
        なく黙って処理対象から消してしまうと、統合待ちのタスクが放置され続けている
        ことに誰も気づけない。少なくとも警告ログを出し、apply時は人間が気づけるよう
        Issueへコメントする。
        """
        for task in self.unparsable_done_tasks:
            print(
                f"Warning: status:done issue #{task.issue_number} has no extractable "
                "subtask_id (Footprint YAML block missing or malformed); excluded "
                "from integration without being marked merged or failed.",
                file=sys.stderr,
            )
            if self.config.apply:
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

    def _attach_unparsable_info(self, result: dict) -> dict:
        if self.unparsable_done_tasks:
            result["unparsable_done_issues"] = [
                t.issue_number for t in self.unparsable_done_tasks
            ]
        return result

    def _run_integration(self, active_done_tasks: list[Task]) -> dict:
        temp_worktree_path = None
        if self.config.apply:
            temp_worktree_path = self._temp_worktree_path()
            try:
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=str(self.config.repository_root),
                    capture_output=True,
                )
                self._reclaim_worktree_path(temp_worktree_path)
                subprocess.run(
                    [
                        "git",
                        "worktree",
                        "add",
                        str(temp_worktree_path),
                        self.config.base_branch,
                    ],
                    cwd=str(self.config.repository_root),
                    check=True,
                    capture_output=True,
                )
                self.config.repository_root = temp_worktree_path
            except (subprocess.CalledProcessError, OSError, RuntimeError) as e:
                return {
                    "status": "failed_to_create_temp_worktree",
                    "error": f"Failed to create temp worktree: {e}",
                }

        merger = IntegrationMerger(
            repository_root=self.config.repository_root,
            original_root=self.original_root,
            ci_command=self.config.ci_command or ["./scripts/local-ci.sh"],
        )

        try:
            if not merger.create_temp_branch(
                self.config.temp_branch, self.config.base_branch, self.config.apply
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
                active_done_tasks, self.config.base_branch, self.config.apply
            )
            self.failed_reasons.update(failed_reasons)
            self.blocked_reasons.update(blocked_reasons)

            if merged_tasks and not failed_tasks:
                if self.config.apply:
                    try:
                        subprocess.run(
                            [
                                "git",
                                "push",
                                "--force",
                                "origin",
                                self.config.temp_branch,
                            ],
                            cwd=str(self.config.repository_root),
                            check=True,
                            capture_output=True,
                        )
                    except subprocess.CalledProcessError as pe:
                        push_error = (pe.stderr or b"").decode(errors="replace")
                        print(
                            f"Failed to push temp branch: {push_error}",
                            file=sys.stderr,
                        )
                        # #52: pushが失敗した状態でPR作成/再利用やsemantic reviewを
                        # 続行すると、既存の統合PRがある場合にリモート上の古いdiffを
                        # 対象にレビューが起動されてしまう。pushの成功をその前提条件
                        # にし、失敗時は明示的な失敗ステータスで即座に終了する。
                        return {
                            "status": "failed_to_push_temp_branch",
                            "merged": merged_tasks,
                            "error": push_error,
                        }

                    integration_pr_number = self._ensure_integration_pr(merged_tasks)

                    # #139: 統合の安全確定（push成功＋統合PR確保成功）を
                    # 確認できた場合にのみ、対象Issueへ`integration:included`を
                    # 記帳する。
                    newly_included: list[str] = []
                    if integration_pr_number is not None:
                        newly_included = self._mark_newly_included(
                            active_done_tasks, merged_tasks
                        )

                    # 意味的レビュー（LLMによる統合diffのバグ検知）はfire-and-forgetで
                    # 起動するのみ。結果は統合PRへのコメントとして残るだけで、Python側は
                    # 合否の追跡・自動マージ等の後続処理を一切行わない（最終マージは常に人間）。
                    semantic_review_dispatched = False
                    if (
                        self.config.enable_semantic_review
                        and self.config.coordinator is not None
                        and integration_pr_number is not None
                    ):
                        try:
                            self.config.coordinator.dispatch_review(
                                temp_branch=self.config.temp_branch,
                                base_branch=self.config.base_branch,
                                pr_number=integration_pr_number,
                                parent_issue_number=self.config.parent_issue_number,
                                merged_subtask_ids=merged_tasks,
                            )
                            semantic_review_dispatched = True
                        except Exception as e:
                            print(
                                f"Warning: Failed to dispatch semantic review: {e}",
                                file=sys.stderr,
                            )

                    return {
                        "status": "success",
                        "merged": merged_tasks,
                        "integration_pr_number": integration_pr_number,
                        "semantic_review_dispatched": semantic_review_dispatched,
                        "newly_included": newly_included,
                    }
                return {"status": "success", "merged": merged_tasks}

            return {
                "status": "partial_success" if merged_tasks else "failure",
                "merged": merged_tasks,
                "failed": failed_tasks,
                "failed_reasons": self.failed_reasons,
                "blocked": blocked_tasks,
                "blocked_reasons": self.blocked_reasons,
            }
        finally:
            if temp_worktree_path:
                try:
                    subprocess.run(
                        [
                            "git",
                            "worktree",
                            "remove",
                            "--force",
                            str(temp_worktree_path),
                        ],
                        cwd=str(self.original_root),
                        capture_output=True,
                        check=True,
                    )
                except Exception:
                    pass

    def _ensure_integration_pr(self, merged_tasks: list[str]) -> int | None:
        return ensure_integration_pr(
            self.config.temp_branch, self.config.base_branch, merged_tasks
        )

    def _mark_newly_included(
        self, active_done_tasks: list[Task], merged_tasks: list[str]
    ) -> list[str]:
        """#139: `status:done`自体は変更せず（依存解決判定や外部ロック解除条件
        など他サブシステムが引き続き参照するため）、統合済みを示す直交ラベル
        `integration:included`のみを記帳する。統合ブランチは毎回base_branchから
        再構築されるため、既に記帳済みのタスクもre-merge対象からは除外しない
        （除外すると再構築のたびに統合ブランチから消えてしまう）。
        """
        newly_included: list[str] = []
        task_by_subtask_id = {
            t.subtask_id: t for t in active_done_tasks if t.subtask_id
        }
        for subtask_id in merged_tasks:
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
