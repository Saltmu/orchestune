from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from orchestune.branch_naming import (
    build_task_branch_name,
    find_unique_matching_pr_branch,
)
from orchestune.dispatch.labels import TERMINAL_ESCALATION_LABELS
from orchestune.forge import Forge, GitHubForge
from orchestune.infra.git_cli import fetch_remote_branch, run_git
from orchestune.infra.process_utils import default_ci_command
from orchestune.integrator.pr import handle_merge_failure
from orchestune.models import PrRecord, Task


class IntegrationMerger:
    """統合ブランチ上での git 操作（配管処理とマージループ）を担う。"""

    def __init__(
        self,
        repository_root: Path,
        original_root: Path,
        ci_command: list[str],
        forge: Forge | None = None,
    ):
        self.repository_root = repository_root
        self.original_root = original_root
        self.ci_command = ci_command
        self.forge = forge or GitHubForge()
        # #777: 1サイクル内の複数タスクでPR一覧を使い回すためのキャッシュ
        # （IntegrationMergerはサイクルごとに新規生成されるためcycleをまたがない）。
        self._open_prs_cache: list[PrRecord] | None = None

    def create_temp_branch(
        self, temp_branch: str, base_branch: str, apply: bool
    ) -> bool:
        if not apply:
            return True
        try:
            run_git(
                ["checkout", "-B", temp_branch, base_branch],
                cwd=self.repository_root,
                check=True,
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def ensure_git_identity(self) -> None:
        # CI環境（actions/checkout等）ではgit committer identityが未設定のことがあり、
        # `git merge --no-ff`でマージコミットを作成する際に
        # "Committer identity unknown" で必ず失敗するため、事前に設定しておく。
        run_git(
            ["config", "user.name", "orchestune-integrator"],
            cwd=self.repository_root,
            check=False,
        )
        run_git(
            [
                "config",
                "user.email",
                "orchestune-integrator@users.noreply.github.com",
            ],
            cwd=self.repository_root,
            check=False,
        )

    def ensure_full_history(self, base_branch: str) -> None:
        # actions/checkout@v6 のデフォルト（浅い・単一ブランチのclone）のままだと、
        # タスクブランチをrefspec指定でfetchしても取得されるのはそのブランチの
        # 先端コミット1つのみ（親情報を持たない）になり、mainと共通の祖先が
        # 見つからず `refusing to merge unrelated histories` でmergeが必ず
        # 失敗する。浅いリポジトリの場合のみ、ベースブランチの履歴を深くしておく。
        try:
            result = run_git(
                ["rev-parse", "--is-shallow-repository"],
                cwd=self.repository_root,
                check=True,
            )
        except subprocess.CalledProcessError:
            return

        if result.stdout.strip() != "true":
            return

        base_branch_name = base_branch.removeprefix("origin/")
        try:
            run_git(
                ["fetch", "--unshallow", "origin", base_branch_name],
                cwd=self.repository_root,
                check=True,
            )
        except subprocess.CalledProcessError:
            pass

    def current_head_sha(self) -> str:
        result = run_git(["rev-parse", "HEAD"], cwd=self.repository_root, check=True)
        return result.stdout.strip()

    def rollback_to(self, sha: str) -> bool:
        try:
            run_git(["reset", "--hard", sha], cwd=self.repository_root, check=True)
            return True
        except (subprocess.CalledProcessError, OSError):
            return False

    def abort_merge(self) -> None:
        # マージ失敗時にMERGE_HEADを残したままにすると、後続タスクのマージが
        # 「進行中の未完了マージがある」ために巻き添えで失敗してしまうため、
        # 一時ブランチの直前の状態へ確実に戻す。
        try:
            run_git(["merge", "--abort"], cwd=self.repository_root, check=False)
        except (subprocess.CalledProcessError, OSError):
            pass

    @staticmethod
    def _find_ancestor_venv(start: Path) -> Path | None:
        """#376: `start`の祖先ディレクトリを遡り、`.venv`を持つ最初の祖先を
        返す。monorepo内でこのパッケージがネストして配置されている場合でも、
        固定の文字列一致やネストの深さに依存せず実際のvenvを見つけられる。"""
        for ancestor in start.parents:
            candidate = ancestor / ".venv"
            if candidate.exists():
                return candidate
        return None

    def _prepare_ci_environment(self) -> tuple[dict[str, str], str | None]:
        env = os.environ.copy()
        env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
        pyproject_path = self.repository_root / "pyproject.toml"
        error = self._install_poetry_dependencies(pyproject_path, env)
        if error:
            return env, error
        self._configure_virtualenv(pyproject_path, env)
        return env, None

    def _install_poetry_dependencies(
        self, pyproject_path: Path, env: dict[str, str]
    ) -> str | None:
        if not pyproject_path.exists():
            return None
        try:
            subprocess.run(
                ["poetry", "install"],
                cwd=str(self.repository_root),
                check=True,
                capture_output=True,
                env=env,
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            return f"Failed to install Poetry dependencies: {exc}"
        return None

    def _configure_virtualenv(self, pyproject_path: Path, env: dict[str, str]) -> None:
        venv_path = (
            self._poetry_virtualenv_path(pyproject_path, env)
            or self._fallback_venv_path()
        )
        if venv_path and venv_path.exists():
            env["VIRTUAL_ENV"] = str(venv_path.resolve())
            bin_path = venv_path / "bin"
            if bin_path.exists():
                env["PATH"] = f"{bin_path.resolve()}{os.pathsep}{env.get('PATH', '')}"

    def _poetry_virtualenv_path(
        self, pyproject_path: Path, env: dict[str, str]
    ) -> Path | None:
        if not pyproject_path.exists():
            return None
        try:
            result = subprocess.run(
                ["poetry", "env", "info", "--path"],
                cwd=str(self.repository_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
                env=env,
            )
            path = Path(result.stdout.strip())
            return path if path.exists() else None
        except (subprocess.CalledProcessError, OSError):
            return None

    def _fallback_venv_path(self) -> Path | None:
        for path in (self.repository_root / ".venv", self.original_root / ".venv"):
            if path.exists():
                return path
        return self._find_ancestor_venv(self.original_root)

    def _execute_ci_command(self, env: dict[str, str]) -> tuple[bool, str]:
        ci_cmd = self.ci_command or default_ci_command()
        try:
            subprocess.run(
                ci_cmd,
                cwd=str(self.repository_root),
                check=True,
                capture_output=True,
                env=env,
            )
            return True, ""
        except subprocess.CalledProcessError as e:
            stdout = (e.stdout or b"").decode(errors="replace")
            stderr = (e.stderr or b"").decode(errors="replace")
            return False, f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"

    def run_ci_in_worktree(self) -> tuple[bool, str]:
        """Prepare the worktree CI environment and run the configured command.

        Dependency installation and virtualenv resolution complete before the
        CI command runs. The integration gate deliberately does not retry the
        command; the caller rolls back when it fails (#208).
        """
        env, error = self._prepare_ci_environment()
        if error is not None:
            return False, error
        return self._execute_ci_command(env)

    def _check_task_blocking(self, task: Task, unavailable_ids: set[str]) -> str | None:
        # #437レビュー対応: status:blocked-human-review（親branchの
        # 連続陳腐化等でエスカレーション済み）のタスクはここでマージ
        # せずblocked扱いにする。`PrepareTasksStep`側で完全に除外して
        # しまうと、このタスクに依存する後続タスク（特にスタッキングで
        # 既にこのタスクの未マージコミットを含んだブランチを持つ後続
        # タスク）が、下のブロック伝播（`unavailable_ids`）の対象から
        # 漏れてそのまま独立にマージされてしまい、エスカレーションで
        # 意図した人間の確認をブロックされたタスクの内容そのものが
        # 迂回してparent branchへ入ってしまう。
        escalated_labels = sorted(
            label for label in TERMINAL_ESCALATION_LABELS if label in task.status_labels
        )
        if escalated_labels:
            return (
                f"{', '.join(escalated_labels)}へエスカレーション済みのため、"
                "人間の確認が完了するまで統合を実行せずスキップしました。"
            )

        blocking_deps = sorted(dep for dep in task.depends_on if dep in unavailable_ids)
        if blocking_deps:
            return (
                "依存タスク "
                f"{', '.join(blocking_deps)} が失敗または依存失敗のため、"
                "統合を実行せずスキップしました。"
            )
        return None

    def _fetch_task_branch(
        self, branch_name: str, base_branch: str
    ) -> tuple[bool, bool, str]:
        """Fetch remote task branch into remote-tracking ref.

        Returns `(success, already_merged, error_message)`.
        """
        # actions/checkout のデフォルト（単一ブランチの浅いclone）では
        # `origin/{branch_name}` のremote-trackingブランチが存在しないため、
        # refspecを明示してfetchしないと後続のmergeが常に
        # 「not something we can merge」で失敗する（内容衝突ではない）。
        try:
            fetch_remote_branch(self.repository_root, branch_name)
            return True, False, ""
        except (subprocess.CalledProcessError, ValueError, OSError) as e:
            # GitHub上の現在のbranch tip SHAがbaseに含まれると証明できた
            # 場合だけ統合済みとして扱う。同名branchの過去PRだけでは、
            # branch再利用後の新commitを見落とすため根拠にしない。
            # branch削除/API障害を含む不確実なケースはfail closedにする。
            base_branch_name = base_branch.removeprefix("origin/")
            try:
                already_merged = self.forge.is_current_branch_tip_merged_into(
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
                    f"[Integrator] Branch {branch_name} could not be "
                    "fetched, but its current remote tip is contained "
                    f"in {base_branch_name}. "
                    "Skipping integration merge."
                )
                return True, True, ""

            fetch_error = getattr(e, "stderr", None) or str(e)
            return False, False, f"Failed to fetch branch: {fetch_error}"

    def _merge_task_branch(self, branch_name: str) -> tuple[bool, str | None, str]:
        """Attempt to merge `origin/{branch_name}` into current temporary branch.

        Returns `(success, pre_merge_sha, error_message)`.
        """
        # #53: mergeが新規コミットを作った（=1コミット戻せばよい）という
        # 仮定に依存すると、対象ブランチの先端が既にHEADへ含まれている場合の
        # `git merge --no-ff`が新規コミットを作らず"Already up to date"に
        # なるケースで、CI失敗時のrollbackが直前の無関係なコミットを
        # 巻き添えで削除してしまう。merge試行前のHEAD SHAを保存しておき、
        # CI失敗時はそのSHAへ確実に戻す。
        try:
            pre_merge_sha: str | None = self.current_head_sha()
        except (subprocess.CalledProcessError, OSError) as e:
            head_error = getattr(e, "stderr", None) or str(e)
            return False, None, f"Failed to capture pre-merge HEAD: {head_error}"

        try:
            run_git(
                [
                    "merge",
                    "--no-ff",
                    "-m",
                    f"Temp merge {branch_name}",
                    f"origin/{branch_name}",
                ],
                cwd=self.repository_root,
                check=True,
            )
            return True, pre_merge_sha, ""
        except (subprocess.CalledProcessError, OSError) as e:
            self.abort_merge()
            merge_error = getattr(e, "stderr", None) or str(e)
            return False, pre_merge_sha, f"Merge conflict: {merge_error}"

    def _verify_ci_and_rollback(
        self, pre_merge_sha: str
    ) -> tuple[bool, str, str | None]:
        """Run CI and roll back to `pre_merge_sha` if CI verification fails.

        Returns `(success, failure_reason, ci_output)`.
        """
        ci_success, ci_output = self.run_ci_in_worktree()
        if ci_success:
            return True, "", None

        reason = "CI verification failed"
        if not self.rollback_to(pre_merge_sha):
            reason += (
                ". さらに、merge前のコミット"
                f"({pre_merge_sha})へのrollbackにも失敗しました。"
                "統合ブランチの状態を手動で確認してください。"
            )
        return False, reason, ci_output

    def _recover_from_unexpected_task_error(
        self,
        task: Task,
        error: Exception,
        captured_pre_merge_sha: str | None,
        handle_failure: Callable[[Task, str], None],
    ) -> None:
        """#374: 1タスクの処理中に発生した想定外の例外（subprocess.
        CalledProcessError以外）の後始末をまとめて行う。呼び出し元の
        `merge_and_test_tasks`本体の分岐を増やさないよう、abort・rollback・
        失敗記録（それ自体が失敗する場合を含む）をここへ切り出す。"""
        print(
            f"[Integrator] Unexpected error while processing {task.subtask_id}: "
            f"{error}",
            file=sys.stderr,
        )
        # 進行中の未完了マージが残っていれば後続タスクを巻き添えにしないよう
        # 先に中断する（マージ未着手の場合はabort_merge自体がcheck=Falseで
        # 静かに失敗するだけなので無条件に呼んでよい）。
        self.abort_merge()
        if captured_pre_merge_sha is not None:
            self.rollback_to(captured_pre_merge_sha)
        try:
            handle_failure(task, f"Unexpected error during merge/test: {error}")
        except Exception as handle_error:
            print(
                f"Warning: Failed to record failure for {task.subtask_id}: "
                f"{handle_error}",
                file=sys.stderr,
            )

    def merge_and_test_tasks(
        self, sorted_done_tasks: list[Task], base_branch: str, apply: bool
    ) -> tuple[
        list[str], list[str], list[str], dict[str, str], dict[str, str], dict[str, str]
    ]:
        merged_tasks: list[str] = []
        failed_tasks: list[str] = []
        blocked_tasks: list[str] = []
        failed_reasons: dict[str, str] = {}
        blocked_reasons: dict[str, str] = {}
        unavailable_ids: set[str] = set()
        fallback_merged_ids: dict[str, str] = {}
        if apply:
            self.ensure_git_identity()
            self.ensure_full_history(base_branch)
        self._merge_task_list(
            sorted_done_tasks,
            base_branch,
            apply,
            merged_tasks,
            failed_tasks,
            blocked_tasks,
            failed_reasons,
            blocked_reasons,
            unavailable_ids,
            fallback_merged_ids,
        )
        return (
            merged_tasks,
            failed_tasks,
            blocked_tasks,
            failed_reasons,
            blocked_reasons,
            fallback_merged_ids,
        )

    def _merge_task_list(
        self,
        tasks: list[Task],
        base_branch: str,
        apply: bool,
        merged: list[str],
        failed: list[str],
        blocked: list[str],
        failed_reasons: dict[str, str],
        blocked_reasons: dict[str, str],
        unavailable: set[str],
        fallback_merged: dict[str, str],
    ) -> None:
        for task in tasks:
            self._merge_one_task(
                task,
                base_branch,
                apply,
                merged,
                failed,
                blocked,
                failed_reasons,
                blocked_reasons,
                unavailable,
                fallback_merged,
            )

    def _merge_one_task(
        self,
        task: Task,
        base_branch: str,
        apply: bool,
        merged: list[str],
        failed: list[str],
        blocked: list[str],
        failed_reasons: dict[str, str],
        blocked_reasons: dict[str, str],
        unavailable: set[str],
        fallback_merged: dict[str, str],
    ) -> None:
        pre_merge_sha: str | None = None
        try:
            blocked_reason = self._check_task_blocking(task, unavailable)
            if blocked_reason:
                print(
                    f"[Integrator] Skipping {task.subtask_id}: {blocked_reason}",
                    file=sys.stderr,
                )
                blocked_reasons[task.subtask_id] = blocked_reason
                blocked.append(task.subtask_id)
                unavailable.add(task.subtask_id)
                return
            pre_merge_sha = self._merge_task_if_needed(
                task,
                base_branch,
                apply,
                merged,
                failed,
                failed_reasons,
                unavailable,
                fallback_merged,
            )
        except Exception as error:

            def handle_failure(failed_task: Task, reason: str) -> None:
                failed_reasons[failed_task.subtask_id] = reason
                handle_merge_failure(failed_task, reason, apply, forge=self.forge)

            self._recover_from_unexpected_task_error(
                task, error, pre_merge_sha, handle_failure
            )
            failed.append(task.subtask_id)
            unavailable.add(task.subtask_id)

    def _candidate_prs_for_fallback(self) -> list[PrRecord]:
        if self._open_prs_cache is None:
            try:
                self._open_prs_cache = self.forge.list_prs(state="open")
            except Exception:
                self._open_prs_cache = []
        return self._open_prs_cache

    def _canonical_branch_confirmed_absent(self, canonical_branch: str) -> bool:
        """正規ブランチが確実に存在しないと判定できた場合のみ`True`を返す。

        #777 Codexレビュー(Round3): `_fetch_task_branch`のfetch失敗は
        「ブランチが存在しない」と「一時的なネットワーク/認証/local git
        エラー」を区別しない。両者を区別せずに②へフォールバックすると、
        正規ブランチが実在するのに一時的なfetch失敗だけで別ブランチを
        mergeしてしまいうる。`branch_exists()`で不在を積極的に確認できた
        場合のみフォールバックを許可し、確認自体が失敗した場合は
        fail-closed（フォールバックしない）とする。
        """
        try:
            return not self.forge.branch_exists(canonical_branch)
        except Exception:
            return False

    def _resolve_merge_branch(
        self, task: Task, base_branch: str
    ) -> tuple[str, bool, bool, str]:
        """マージ対象ブランチ名を解決する（#777: fail-closedな段階的解決）。

        ①Orchestuneが起動時に割り当てた正規ブランチ名への完全一致でのfetchを
        まず試み、それが失敗し、かつ正規ブランチの不在を積極的に確認できた
        場合に限り②厳密一致する単一のPR head_ref（`issue_number`と
        `subtask_id`の両方が一致し、かつマッチが一意な場合のみ）へ
        フォールバックする。②で複数の異なるブランチが一致する、一致する
        ブランチが無い、または正規ブランチの不在を確認できない場合は、
        ①の失敗結果をそのまま返してfail-closedにする（別のブランチを
        掴んでmergeしない）。
        """
        canonical_branch = build_task_branch_name(task.issue_number, task.subtask_id)
        fetched, already_merged, reason = self._fetch_task_branch(
            canonical_branch, base_branch
        )
        if fetched:
            return canonical_branch, fetched, already_merged, reason

        if not self._canonical_branch_confirmed_absent(canonical_branch):
            return canonical_branch, fetched, already_merged, reason

        fallback_branch = find_unique_matching_pr_branch(
            self._candidate_prs_for_fallback(), task.issue_number, task.subtask_id
        )
        if fallback_branch is None or fallback_branch == canonical_branch:
            return canonical_branch, fetched, already_merged, reason

        return (fallback_branch, *self._fetch_task_branch(fallback_branch, base_branch))

    def _merge_task_if_needed(
        self,
        task: Task,
        base_branch: str,
        apply: bool,
        merged: list[str],
        failed: list[str],
        failed_reasons: dict[str, str],
        unavailable: set[str],
        fallback_merged: dict[str, str],
    ) -> str | None:
        if not apply:
            merged.append(task.subtask_id)
            return None
        branch_name, fetched, already_merged, reason = self._resolve_merge_branch(
            task, base_branch
        )
        if not fetched:
            self._record_failure(
                task, reason, apply, failed, failed_reasons, unavailable
            )
            return None
        canonical_branch = build_task_branch_name(task.issue_number, task.subtask_id)
        if branch_name != canonical_branch:
            # #777 Codexレビュー(Round3/Round5): ②由来のブランチでマージ
            # されたタスクは、削除では正規名を一切使わせない（正規名が
            # その後の別経緯で存在するようになっても、未マージの別物を
            # 誤って削除しないため）。一方で統合済み検証は、この実際に
            # マージしたブランチ名（正規名ではない）でなら安全に行える
            # ため、削除除外用の印だけでなく実名も保持しておく。
            fallback_merged[task.subtask_id] = branch_name
        if already_merged:
            merged.append(task.subtask_id)
            return None
        merged_ok, pre_merge_sha, reason = self._merge_task_branch(branch_name)
        if not merged_ok or pre_merge_sha is None:
            self._record_failure(
                task, reason, apply, failed, failed_reasons, unavailable
            )
            return pre_merge_sha
        try:
            ci_ok, reason, output = self._verify_ci_and_rollback(pre_merge_sha)
        except Exception:
            self.abort_merge()
            self.rollback_to(pre_merge_sha)
            raise
        if not ci_ok:
            self._record_failure(
                task, reason, apply, failed, failed_reasons, unavailable, output
            )
            return pre_merge_sha
        merged.append(task.subtask_id)
        return pre_merge_sha

    def _record_failure(
        self,
        task: Task,
        reason: str,
        apply: bool,
        failed: list[str],
        reasons: dict[str, str],
        unavailable: set[str],
        ci_output: str | None = None,
    ) -> None:
        reasons[task.subtask_id] = reason
        handle_merge_failure(task, reason, apply, ci_output, forge=self.forge)
        failed.append(task.subtask_id)
        unavailable.add(task.subtask_id)
