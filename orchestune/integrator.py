from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from orchestune import github
from orchestune.dag import SubTask, build_dag
from orchestune.dispatcher import Task, file_lock, parse_task_from_issue
from orchestune.integration_coordinator import IntegrationCoordinator

_UNSAFE_WORKTREE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def _worktree_dir_name(temp_branch: str) -> str:
    """#51: 統合対象（`temp_branch`）ごとに一意なworktreeディレクトリ名を作る。

    親Issueごとに`temp_branch`が既に一意なため（`IntegratorConfig.__post_init__`
    参照）、これをそのままファイルシステム安全な名前へ変換するだけで、
    異なる統合対象が同じworktreeパスを共有しないようにできる。
    """
    return _UNSAFE_WORKTREE_NAME_CHARS.sub("-", temp_branch)


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

    def run(self) -> dict:
        sorted_done_tasks = self._get_sorted_done_tasks()
        active_done_tasks = [
            t
            for t in sorted_done_tasks
            if t.issue_state != "CLOSED" and t.parent_state != "CLOSED"
        ]
        if not active_done_tasks:
            return {"status": "no_done_tasks", "merged": []}

        temp_worktree_path = None
        lock_ctx: contextlib.AbstractContextManager[None] = contextlib.nullcontext()
        if self.config.apply:
            worktree_name = _worktree_dir_name(self.config.temp_branch)
            temp_worktree_path = (
                self.config.repository_root / "worktrees" / worktree_name
            )
            # #51: ロックファイルはworktree本体の外側（兄弟ファイル）に置き、
            # rmtree/worktree addのサイクルをまたいでロック自体が存続するようにする。
            lock_path = (
                self.config.repository_root / "worktrees" / f"{worktree_name}.lock"
            )
            lock_ctx = file_lock(lock_path)

        try:
            with lock_ctx:
                if self.config.apply and temp_worktree_path is not None:
                    worktree_error = self._create_temp_worktree(temp_worktree_path)
                    if worktree_error is not None:
                        return worktree_error

                try:
                    if not self._create_temp_branch():
                        return {
                            "status": "failed_to_create_temp_branch",
                            "error": "Failed to create temp branch",
                        }

                    merged_tasks, failed_tasks = self._merge_and_test_tasks(
                        active_done_tasks
                    )

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
                                print(
                                    f"Warning: Failed to push temp branch: {pe.stderr.decode()}",
                                    file=sys.stderr,
                                )

                            integration_pr_number = self._ensure_integration_pr(
                                merged_tasks
                            )

                            # 意味的レビュー（LLMによる統合diffのバグ検知）は
                            # fire-and-forgetで起動するのみ。結果は統合PRへの
                            # コメントとして残るだけで、Python側は合否の追跡・
                            # 自動マージ等の後続処理を一切行わない
                            # （最終マージは常に人間）。
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
                            }
                        return {"status": "success", "merged": merged_tasks}

                    return {
                        "status": "partial_success" if merged_tasks else "failure",
                        "merged": merged_tasks,
                        "failed": failed_tasks,
                        "failed_reasons": self.failed_reasons,
                    }
                finally:
                    if temp_worktree_path:
                        self._cleanup_temp_worktree(temp_worktree_path)
        except RuntimeError as e:
            # #51: file_lock()が別の実行中インスタンスを検知した場合の
            # ロック取得失敗のみをここで捕捉する。
            return {"status": "failed_to_acquire_lock", "error": str(e)}

    def _create_temp_worktree(self, temp_worktree_path: Path) -> dict | None:
        """`temp_worktree_path`にgit worktreeを作成する。

        成功時は`None`を、失敗時はrun()がそのまま返すべきエラー辞書を返す。
        成功時、`self.config.repository_root`を新しいworktreeのパスへ
        差し替える副作用を持つ。
        """
        try:
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=str(self.config.repository_root),
                capture_output=True,
            )
            if temp_worktree_path.exists():
                # #51: 所有権確認なしにディレクトリを削除しない。
                # 実際にgit worktreeらしい（`.git`エントリを持つ）場合のみ削除する。
                if not (temp_worktree_path / ".git").exists():
                    return {
                        "status": "failed_to_create_temp_worktree",
                        "error": (
                            "Refusing to remove non-worktree directory "
                            f"at {temp_worktree_path}"
                        ),
                    }
                import shutil

                try:
                    shutil.rmtree(temp_worktree_path)
                except Exception:
                    pass
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
            return None
        except (subprocess.CalledProcessError, OSError) as e:
            return {
                "status": "failed_to_create_temp_worktree",
                "error": f"Failed to create temp worktree: {e}",
            }

    def _cleanup_temp_worktree(self, temp_worktree_path: Path) -> None:
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(temp_worktree_path)],
                cwd=str(self.original_root),
                capture_output=True,
                check=True,
            )
        except Exception:
            pass

    def _ensure_integration_pr(self, merged_tasks: list[str]) -> int | None:
        """統合ブランチ(`temp_branch`)から`base_branch`へのPRを作成/再利用する。

        既にopenなPRがあれば重複作成せずその番号を返す。PR作成自体に失敗しても
        （差分無し等）Integrator全体は失敗させず、警告ログのみ出して`None`を返す。
        """
        try:
            existing = [
                pr
                for pr in github.list_open_prs()
                if pr.head_ref == self.config.temp_branch
            ]
            if existing:
                return existing[0].number

            base = self.config.base_branch.removeprefix("origin/")
            return github.create_pull_request(
                head=self.config.temp_branch,
                base=base,
                title=f"Integrate completed tasks ({', '.join(merged_tasks)})",
                body=(
                    "Orchestune Integrator が仮マージCI通過後に作成した統合PRです。\n"
                    f"統合済みタスク: {', '.join(merged_tasks)}\n\n"
                    "最終マージは人間が行ってください。"
                ),
            )
        except Exception as e:
            print(f"Warning: Failed to ensure integration PR: {e}", file=sys.stderr)
            return None

    def _get_sorted_done_tasks(self) -> list[Task]:
        done_issues = github.list_issues_by_label("status:done", state="all")
        if not done_issues:
            return []

        all_issues = []
        for label in [
            "status:queued",
            "status:in-progress",
            "status:blocked",
            "status:external-lock",
            "status:done",
        ]:
            state = "all" if label == "status:done" else "open"
            all_issues.extend(github.list_issues_by_label(label, state=state))

        # parent_issue_number が指定されている場合、親Issueが一致する子Issueのみにフィルタリングする
        if self.config.parent_issue_number is not None:
            done_issues = [
                i
                for i in done_issues
                if i.parent
                and i.parent.get("number") == self.config.parent_issue_number
            ]
            all_issues = [
                i
                for i in all_issues
                if i.parent
                and i.parent.get("number") == self.config.parent_issue_number
            ]

        seen_numbers = set()
        unique_issues = []
        for issue in all_issues:
            if issue.number not in seen_numbers:
                seen_numbers.add(issue.number)
                unique_issues.append(issue)

        # すべてのIssueについて、YAML内の subtask_id を事前スキャンしてマッピングを構築する
        issue_to_subtask_id = self._build_issue_to_subtask_id_map(
            unique_issues + done_issues
        )

        tasks = [
            parse_task_from_issue(issue, issue_to_subtask_id) for issue in unique_issues
        ]
        subtasks = [
            SubTask(
                id=task.subtask_id,
                description="",
                footprint=task.footprint,
                symbols=task.symbols,
                depends_on=task.depends_on,
                risk=task.risk,
                risk_reasons=(),
            )
            for task in tasks
            if task.subtask_id
        ]

        try:
            dag = build_dag(subtasks)
            topological_order = dag.topological_order
        except Exception as e:
            print(f"Warning: Failed to build DAG: {e}", file=sys.stderr)
            topological_order = [t.id for t in subtasks]

        done_tasks = [
            parse_task_from_issue(issue, issue_to_subtask_id) for issue in done_issues
        ]
        done_task_map = {t.subtask_id: t for t in done_tasks if t.subtask_id}

        sorted_done_tasks = []
        for subtask_id in topological_order:
            if subtask_id in done_task_map:
                sorted_done_tasks.append(done_task_map[subtask_id])

        for t in done_tasks:
            if t.subtask_id and t.subtask_id not in [
                x.subtask_id for x in sorted_done_tasks
            ]:
                sorted_done_tasks.append(t)

        return sorted_done_tasks

    def _build_issue_to_subtask_id_map(
        self, issues: list[github.IssueRecord]
    ) -> dict[int, str]:
        import yaml

        from orchestune.dispatch_scoring import _FOOTPRINT_BLOCK_PATTERN

        issue_to_subtask_id = {}
        for issue in issues:
            match = _FOOTPRINT_BLOCK_PATTERN.search(issue.body)
            if match:
                try:
                    data = yaml.safe_load(match.group(1))
                    if isinstance(data, dict):
                        sub_id = data.get("subtask_id")
                        if sub_id:
                            issue_to_subtask_id[issue.number] = str(sub_id)
                except Exception:
                    pass
        return issue_to_subtask_id

    def _create_temp_branch(self) -> bool:
        if not self.config.apply:
            return True
        try:
            subprocess.run(
                [
                    "git",
                    "checkout",
                    "-B",
                    self.config.temp_branch,
                    self.config.base_branch,
                ],
                cwd=str(self.config.repository_root),
                check=True,
                capture_output=True,
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def _merge_and_test_tasks(
        self, sorted_done_tasks: list[Task]
    ) -> tuple[list[str], list[str]]:
        merged_tasks = []
        failed_tasks = []

        if self.config.apply:
            self._ensure_git_identity()
            self._ensure_full_history()

        for task in sorted_done_tasks:
            branch_name = (
                f"claude/issue-{task.issue_number}-{task.subtask_id or 'task'}"
            )

            if self.config.apply:
                # actions/checkout のデフォルト（単一ブランチの浅いclone）では
                # `origin/{branch_name}` のremote-trackingブランチが存在しないため、
                # refspecを明示してfetchしないと後続のmergeが常に
                # 「not something we can merge」で失敗する（内容衝突ではない）。
                try:
                    subprocess.run(
                        [
                            "git",
                            "fetch",
                            "origin",
                            f"{branch_name}:refs/remotes/origin/{branch_name}",
                        ],
                        cwd=str(self.config.repository_root),
                        check=True,
                        capture_output=True,
                    )
                except subprocess.CalledProcessError as e:
                    # ブランチ削除済みの正常系は、同じhead/baseのPRがGitHub上で
                    # 実際にmergedと確認できた場合に限って統合済みとして扱う。
                    # 確認APIの障害を含む不確実なケースはfail closedにする。
                    base_branch_name = self.config.base_branch.removeprefix("origin/")
                    try:
                        already_merged = github.is_branch_merged_into(
                            branch_name, base_branch_name
                        )
                    except Exception as lookup_error:
                        already_merged = False
                        print(
                            "Warning: Failed to verify whether "
                            f"{branch_name} was merged into {base_branch_name}: "
                            f"{lookup_error}",
                            file=sys.stderr,
                        )

                    if already_merged:
                        print(
                            f"[Integrator] Branch {branch_name} could not be fetched, "
                            f"but its PR into {base_branch_name} is merged. "
                            "Skipping integration merge."
                        )
                        merged_tasks.append(task.subtask_id)
                        continue

                    fetch_error = (e.stderr or b"").decode(errors="replace")
                    self._handle_failure(task, f"Failed to fetch branch: {fetch_error}")
                    failed_tasks.append(task.subtask_id)
                    continue

                try:
                    subprocess.run(
                        [
                            "git",
                            "merge",
                            "--no-ff",
                            "-m",
                            f"Temp merge {branch_name}",
                            f"origin/{branch_name}",
                        ],
                        cwd=str(self.config.repository_root),
                        check=True,
                        capture_output=True,
                    )
                except subprocess.CalledProcessError as e:
                    self._abort_merge()
                    self._handle_failure(task, f"Merge conflict: {e.stderr.decode()}")
                    failed_tasks.append(task.subtask_id)
                    continue

                ci_success, ci_output = self._run_ci_with_flaky_check()
                if not ci_success:
                    subprocess.run(
                        ["git", "reset", "--hard", "HEAD~1"],
                        cwd=str(self.config.repository_root),
                        check=True,
                        capture_output=True,
                    )
                    self._handle_failure(
                        task, "CI verification failed", ci_output=ci_output
                    )
                    failed_tasks.append(task.subtask_id)
                    continue

            merged_tasks.append(task.subtask_id)

        return merged_tasks, failed_tasks

    def _ensure_git_identity(self) -> None:
        # CI環境（actions/checkout等）ではgit committer identityが未設定のことがあり、
        # `git merge --no-ff`でマージコミットを作成する際に
        # "Committer identity unknown" で必ず失敗するため、事前に設定しておく。
        subprocess.run(
            ["git", "config", "user.name", "orchestune-integrator"],
            cwd=str(self.config.repository_root),
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "config",
                "user.email",
                "orchestune-integrator@users.noreply.github.com",
            ],
            cwd=str(self.config.repository_root),
            capture_output=True,
        )

    def _ensure_full_history(self) -> None:
        # actions/checkout@v6 のデフォルト（浅い・単一ブランチのclone）のままだと、
        # タスクブランチをrefspec指定でfetchしても取得されるのはそのブランチの
        # 先端コミット1つのみ（親情報を持たない）になり、mainと共通の祖先が
        # 見つからず `refusing to merge unrelated histories` でmergeが必ず
        # 失敗する。浅いリポジトリの場合のみ、ベースブランチの履歴を深くしておく。
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--is-shallow-repository"],
                cwd=str(self.config.repository_root),
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            return

        if (result.stdout or b"").decode().strip() != "true":
            return

        base_branch_name = self.config.base_branch.removeprefix("origin/")
        try:
            subprocess.run(
                ["git", "fetch", "--unshallow", "origin", base_branch_name],
                cwd=str(self.config.repository_root),
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            pass

    def _abort_merge(self) -> None:
        # マージ失敗時にMERGE_HEADを残したままにすると、後続タスクのマージが
        # 「進行中の未完了マージがある」ために巻き添えで失敗してしまうため、
        # 一時ブランチの直前の状態へ確実に戻す。
        try:
            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=str(self.config.repository_root),
                capture_output=True,
            )
        except (subprocess.CalledProcessError, OSError):
            pass

    def _run_ci_with_flaky_check(self) -> tuple[bool, str]:
        # #208: 丸ごとの再実行はしない。既知のflakyテストは呼び出し先の
        # pytest-rerunfailures（quarantineリストに基づく個別リトライ）が
        # 内部で吸収するため、ここで通しの再実行を重ねる必要はない。
        # quarantine対象外のテストが不安定な場合は、そのままCI失敗として
        # 正しく検知させ、人間がquarantineリストへの追加を判断する。
        ci_cmd = self.config.ci_command or ["./scripts/local-ci.sh"]

        env = os.environ.copy()
        venv_path = self.original_root / ".venv"
        if "tools/orchestune" in str(venv_path):
            parent_venv = venv_path.parent.parent.parent / ".venv"
            if parent_venv.exists():
                venv_path = parent_venv

        if venv_path.exists():
            env["VIRTUAL_ENV"] = str(venv_path.resolve())
            bin_path = venv_path / "bin"
            if bin_path.exists():
                env["PATH"] = f"{bin_path.resolve()}{os.pathsep}{env.get('PATH', '')}"

        try:
            subprocess.run(
                ci_cmd,
                cwd=str(self.config.repository_root),
                check=True,
                capture_output=True,
                env=env,
            )
            return True, ""
        except subprocess.CalledProcessError as e:
            stdout = (e.stdout or b"").decode(errors="replace")
            stderr = (e.stderr or b"").decode(errors="replace")
            return False, f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"

    # #295: GitHubコメントの肥大化を避けるため、末尾のみを埋め込む。
    # エラーメッセージ本体は通常出力の末尾に現れるため、これで十分な情報量を確保する。
    _CI_OUTPUT_COMMENT_TAIL_CHARS = 4000

    def _handle_failure(
        self, task: Task, reason: str, ci_output: str | None = None
    ) -> None:
        self.failed_reasons[task.subtask_id] = reason
        if ci_output:
            # #295: ジョブログ（stderr）には切り詰めずに全文を残し、
            # コメントに書ききれない詳細もそこから追跡できるようにする。
            print(
                f"[Integrator] CI failure output for {task.subtask_id}:\n{ci_output}",
                file=sys.stderr,
            )
        if self.config.apply:
            github.remove_label(task.issue_number, "status:done")
            github.add_label(task.issue_number, "status:queued")
            comment_body = (
                f"仮マージCIでエラーが検出されたため、マージを取り消し差し戻しました。\n"
                f"理由: {reason}\n"
            )
            if ci_output:
                truncated = ci_output[-self._CI_OUTPUT_COMMENT_TAIL_CHARS :]
                comment_body += (
                    "\n<details><summary>CI出力（末尾"
                    f"{self._CI_OUTPUT_COMMENT_TAIL_CHARS}文字）</summary>\n\n"
                    f"```\n{truncated}\n```\n</details>\n"
                )
            comment_body += "自動修復エージェントの再起動を待ちます。"
            github.add_comment(task.issue_number, comment_body)
