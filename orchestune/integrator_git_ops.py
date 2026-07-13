from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from orchestune import github
from orchestune.dispatcher import Task
from orchestune.integrator_pr import handle_merge_failure


class IntegrationMerger:
    """統合ブランチ上での git 操作（配管処理とマージループ）を担う。"""

    def __init__(
        self, repository_root: Path, original_root: Path, ci_command: list[str]
    ):
        self.repository_root = repository_root
        self.original_root = original_root
        self.ci_command = ci_command

    def create_temp_branch(
        self, temp_branch: str, base_branch: str, apply: bool
    ) -> bool:
        if not apply:
            return True
        try:
            subprocess.run(
                ["git", "checkout", "-B", temp_branch, base_branch],
                cwd=str(self.repository_root),
                check=True,
                capture_output=True,
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def ensure_git_identity(self) -> None:
        # CI環境（actions/checkout等）ではgit committer identityが未設定のことがあり、
        # `git merge --no-ff`でマージコミットを作成する際に
        # "Committer identity unknown" で必ず失敗するため、事前に設定しておく。
        subprocess.run(
            ["git", "config", "user.name", "orchestune-integrator"],
            cwd=str(self.repository_root),
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "config",
                "user.email",
                "orchestune-integrator@users.noreply.github.com",
            ],
            cwd=str(self.repository_root),
            capture_output=True,
        )

    def ensure_full_history(self, base_branch: str) -> None:
        # actions/checkout@v6 のデフォルト（浅い・単一ブランチのclone）のままだと、
        # タスクブランチをrefspec指定でfetchしても取得されるのはそのブランチの
        # 先端コミット1つのみ（親情報を持たない）になり、mainと共通の祖先が
        # 見つからず `refusing to merge unrelated histories` でmergeが必ず
        # 失敗する。浅いリポジトリの場合のみ、ベースブランチの履歴を深くしておく。
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--is-shallow-repository"],
                cwd=str(self.repository_root),
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            return

        if (result.stdout or b"").decode().strip() != "true":
            return

        base_branch_name = base_branch.removeprefix("origin/")
        try:
            subprocess.run(
                ["git", "fetch", "--unshallow", "origin", base_branch_name],
                cwd=str(self.repository_root),
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            pass

    def current_head_sha(self) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(self.repository_root),
            check=True,
            capture_output=True,
        )
        return (result.stdout or b"").decode(errors="replace").strip()

    def rollback_to(self, sha: str) -> bool:
        try:
            subprocess.run(
                ["git", "reset", "--hard", sha],
                cwd=str(self.repository_root),
                check=True,
                capture_output=True,
            )
            return True
        except (subprocess.CalledProcessError, OSError):
            return False

    def abort_merge(self) -> None:
        # マージ失敗時にMERGE_HEADを残したままにすると、後続タスクのマージが
        # 「進行中の未完了マージがある」ために巻き添えで失敗してしまうため、
        # 一時ブランチの直前の状態へ確実に戻す。
        try:
            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=str(self.repository_root),
                capture_output=True,
            )
        except (subprocess.CalledProcessError, OSError):
            pass

    def run_ci_with_flaky_check(self) -> tuple[bool, str]:
        # #208: 丸ごとの再実行はしない。既知のflakyテストは呼び出し先の
        # pytest-rerunfailures（quarantineリストに基づく個別リトライ）が
        # 内部で吸収するため、ここで通しの再実行を重ねる必要はない。
        # quarantine対象外のテストが不安定な場合は、そのままCI失敗として
        # 正しく検知させ、人間がquarantineリストへの追加を判断する。
        ci_cmd = self.ci_command or ["./scripts/local-ci.sh"]

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
            except subprocess.CalledProcessError as pie:
                print(
                    f"Warning: Failed to run poetry install: {(pie.stderr or b'').decode(errors='replace')}",
                    file=sys.stderr,
                )

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
                stdout_str = (
                    res.stdout.decode(errors="replace")
                    if isinstance(res.stdout, bytes)
                    else res.stdout
                )
                p = Path(stdout_str.strip())
                if p.exists():
                    venv_path = p
            except subprocess.CalledProcessError:
                pass

        if venv_path is None:
            venv_path = self.repository_root / ".venv"
            if not venv_path.exists():
                venv_path = self.original_root / ".venv"
                if "tools/orchestune" in str(venv_path):
                    parent_venv = venv_path.parent.parent.parent / ".venv"
                    if parent_venv.exists():
                        venv_path = parent_venv

        if venv_path and venv_path.exists():
            env["VIRTUAL_ENV"] = str(venv_path.resolve())
            bin_path = venv_path / "bin"
            if bin_path.exists():
                env["PATH"] = f"{bin_path.resolve()}{os.pathsep}{env.get('PATH', '')}"

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

    def merge_and_test_tasks(
        self, sorted_done_tasks: list[Task], base_branch: str, apply: bool
    ) -> tuple[list[str], list[str], list[str], dict[str, str], dict[str, str]]:
        merged_tasks = []
        failed_tasks = []
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
            handle_merge_failure(task, reason, apply, ci_output)

        for task in sorted_done_tasks:
            blocking_deps = sorted(
                dep for dep in task.depends_on if dep in unavailable_ids
            )
            if blocking_deps:
                reason = (
                    "依存タスク "
                    f"{', '.join(blocking_deps)} が失敗または依存失敗のため、"
                    "統合を実行せずスキップしました。"
                )
                print(
                    f"[Integrator] Skipping {task.subtask_id}: {reason}",
                    file=sys.stderr,
                )
                blocked_reasons[task.subtask_id] = reason
                blocked_tasks.append(task.subtask_id)
                unavailable_ids.add(task.subtask_id)
                continue

            branch_name = (
                f"claude/issue-{task.issue_number}-{task.subtask_id or 'task'}"
            )

            if apply:
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
                        cwd=str(self.repository_root),
                        check=True,
                        capture_output=True,
                    )
                except subprocess.CalledProcessError as e:
                    # ブランチ削除済みの正常系は、同じhead/baseのPRがGitHub上で
                    # 実際にmergedと確認できた場合に限って統合済みとして扱う。
                    # 確認APIの障害を含む不確実なケースはfail closedにする。
                    base_branch_name = base_branch.removeprefix("origin/")
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
                    handle_failure(task, f"Failed to fetch branch: {fetch_error}")
                    failed_tasks.append(task.subtask_id)
                    unavailable_ids.add(task.subtask_id)
                    continue

                # #53: mergeが新規コミットを作った（=1コミット戻せばよい）という
                # 仮定に依存すると、対象ブランチの先端が既にHEADへ含まれている場合の
                # `git merge --no-ff`が新規コミットを作らず"Already up to date"に
                # なるケースで、CI失敗時のrollbackが直前の無関係なコミットを
                # 巻き添えで削除してしまう。merge試行前のHEAD SHAを保存しておき、
                # CI失敗時はそのSHAへ確実に戻す。
                try:
                    pre_merge_sha = self.current_head_sha()
                except subprocess.CalledProcessError as e:
                    head_error = (e.stderr or b"").decode(errors="replace")
                    handle_failure(
                        task, f"Failed to capture pre-merge HEAD: {head_error}"
                    )
                    failed_tasks.append(task.subtask_id)
                    unavailable_ids.add(task.subtask_id)
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
                        cwd=str(self.repository_root),
                        check=True,
                        capture_output=True,
                    )
                except subprocess.CalledProcessError as e:
                    self.abort_merge()
                    merge_error = (e.stderr or b"").decode(errors="replace")
                    handle_failure(task, f"Merge conflict: {merge_error}")
                    failed_tasks.append(task.subtask_id)
                    unavailable_ids.add(task.subtask_id)
                    continue

                ci_success, ci_output = self.run_ci_with_flaky_check()
                if not ci_success:
                    reason = "CI verification failed"
                    if not self.rollback_to(pre_merge_sha):
                        reason += (
                            ". さらに、merge前のコミット"
                            f"({pre_merge_sha})へのrollbackにも失敗しました。"
                            "統合ブランチの状態を手動で確認してください。"
                        )
                    handle_failure(task, reason, ci_output=ci_output)
                    failed_tasks.append(task.subtask_id)
                    unavailable_ids.add(task.subtask_id)
                    continue

            merged_tasks.append(task.subtask_id)

        return (
            merged_tasks,
            failed_tasks,
            blocked_tasks,
            failed_reasons,
            blocked_reasons,
        )
