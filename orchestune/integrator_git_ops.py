from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from orchestune.dispatch_labels import TERMINAL_ESCALATION_LABELS
from orchestune.forge import Forge, GitHubForge
from orchestune.git_cli import run_git
from orchestune.integrator_pr import handle_merge_failure
from orchestune.models import Task
from orchestune.process_utils import default_ci_command


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
        if pyproject_path.exists():
            try:
                subprocess.run(
                    ["poetry", "install"],
                    cwd=str(self.repository_root),
                    check=True,
                    capture_output=True,
                    env=env,
                )
            except (subprocess.CalledProcessError, OSError) as exc:
                return env, f"Failed to install Poetry dependencies: {exc}"

        venv_path = None
        if pyproject_path.exists():
            try:
                res = subprocess.run(
                    ["poetry", "env", "info", "--path"],
                    cwd=str(self.repository_root),
                    capture_output=True,
                    text=True,
                    check=True,
                    env=env,
                )
                p = Path(res.stdout.strip())
                if p.exists():
                    venv_path = p
            except (subprocess.CalledProcessError, OSError):
                pass

        if venv_path is None:
            venv_path = self.repository_root / ".venv"
            if not venv_path.exists():
                venv_path = self.original_root / ".venv"
                if not venv_path.exists():
                    venv_path = self._find_ancestor_venv(self.original_root)

        if venv_path and venv_path.exists():
            env["VIRTUAL_ENV"] = str(venv_path.resolve())
            bin_path = venv_path / "bin"
            if bin_path.exists():
                env["PATH"] = f"{bin_path.resolve()}{os.pathsep}{env.get('PATH', '')}"

        return env, None

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

    def run_ci_with_flaky_check(self) -> tuple[bool, str]:
        # #208: 丸ごとの再実行はしない。既知のflakyテストは呼び出し先の
        # pytest-rerunfailures（quarantineリストに基づく個別リトライ）が
        # 内部で吸収するため、ここで通しの再実行を重ねる必要はない。
        # quarantine対象外のテストが不安定な場合は、そのままCI失敗として
        # 正しく検知させ、人間がquarantineリストへの追加を判断する。
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
            run_git(
                [
                    "fetch",
                    "origin",
                    f"{branch_name}:refs/remotes/origin/{branch_name}",
                ],
                cwd=self.repository_root,
                check=True,
            )
            return True, False, ""
        except subprocess.CalledProcessError as e:
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

            fetch_error = e.stderr or ""
            return False, False, f"Failed to fetch branch: {fetch_error}"

    def _merge_task_branch(self, branch_name: str) -> tuple[bool, str, str]:
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
            pre_merge_sha = self.current_head_sha()
        except subprocess.CalledProcessError as e:
            head_error = e.stderr or ""
            return False, "", f"Failed to capture pre-merge HEAD: {head_error}"

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
        except subprocess.CalledProcessError as e:
            self.abort_merge()
            merge_error = e.stderr or ""
            return False, pre_merge_sha, f"Merge conflict: {merge_error}"

    def _verify_ci_and_rollback(
        self, pre_merge_sha: str
    ) -> tuple[bool, str, str | None]:
        """Run CI and roll back to `pre_merge_sha` if CI verification fails.

        Returns `(success, failure_reason, ci_output)`.
        """
        ci_success, ci_output = self.run_ci_with_flaky_check()
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
    ) -> tuple[list[str], list[str], list[str], dict[str, str], dict[str, str]]:
        merged_tasks: list[str] = []
        failed_tasks: list[str] = []
        blocked_tasks: list[str] = []
        failed_reasons: dict[str, str] = {}
        blocked_reasons: dict[str, str] = {}
        # #50: 失敗またはblockedになったsubtask_idを集約する。sorted_done_tasksは
        # 依存関係のトポロジカル順で渡されるため、1回の順走査で後続タスクへ
        # 推移的にblocked状態を伝播できる。
        unavailable_ids: set[str] = set()

        if apply:
            self.ensure_git_identity()
            self.ensure_full_history(base_branch)

        def handle_failure(
            task: Task, reason: str, ci_output: str | None = None
        ) -> None:
            failed_reasons[task.subtask_id] = reason
            handle_merge_failure(task, reason, apply, ci_output, forge=self.forge)

        for task in sorted_done_tasks:
            # #374: このタスクの処理中に想定外の例外（subprocess.CalledProcessError
            # 以外）が発生しても、他タスクの処理・戻り値の一貫性を守るため、
            # ループ本体全体を1タスク単位のtry/exceptで囲む。merge成功後の未検証
            # コミットを後続タスクへ持ち越さないよう、captured_pre_merge_shaへ
            # merge直前のHEADを記録しておき、except側でロールバックに使う。
            captured_pre_merge_sha: str | None = None
            try:
                blocked_reason = self._check_task_blocking(task, unavailable_ids)
                if blocked_reason:
                    print(
                        f"[Integrator] Skipping {task.subtask_id}: {blocked_reason}",
                        file=sys.stderr,
                    )
                    blocked_reasons[task.subtask_id] = blocked_reason
                    blocked_tasks.append(task.subtask_id)
                    unavailable_ids.add(task.subtask_id)
                    continue

                if apply:
                    branch_name = (
                        f"claude/issue-{task.issue_number}-{task.subtask_id or 'task'}"
                    )
                    fetch_ok, already_merged, fetch_err = self._fetch_task_branch(
                        branch_name, base_branch
                    )
                    if not fetch_ok:
                        handle_failure(task, fetch_err)
                        failed_tasks.append(task.subtask_id)
                        unavailable_ids.add(task.subtask_id)
                        continue
                    if already_merged:
                        merged_tasks.append(task.subtask_id)
                        continue

                    merge_ok, pre_merge_sha, merge_err = self._merge_task_branch(
                        branch_name
                    )
                    captured_pre_merge_sha = pre_merge_sha
                    if not merge_ok:
                        handle_failure(task, merge_err)
                        failed_tasks.append(task.subtask_id)
                        unavailable_ids.add(task.subtask_id)
                        continue

                    ci_ok, ci_reason, ci_out = self._verify_ci_and_rollback(
                        pre_merge_sha
                    )
                    if not ci_ok:
                        handle_failure(task, ci_reason, ci_output=ci_out)
                        failed_tasks.append(task.subtask_id)
                        unavailable_ids.add(task.subtask_id)
                        continue

                merged_tasks.append(task.subtask_id)
            except Exception as error:
                self._recover_from_unexpected_task_error(
                    task, error, captured_pre_merge_sha, handle_failure
                )
                failed_tasks.append(task.subtask_id)
                unavailable_ids.add(task.subtask_id)

        return (
            merged_tasks,
            failed_tasks,
            blocked_tasks,
            failed_reasons,
            blocked_reasons,
        )
