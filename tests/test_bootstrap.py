import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import orchestune.bootstrap as bootstrap_module
import orchestune.forge.admin as forge_admin
from orchestune.bootstrap import main, prepare_github_labels, run_bootstrap
from orchestune.forge import (
    REQUIRED_LABELS,
    BootstrapResult,
    ForgeAuthError,
    ForgeError,
    GitHubRepoAdminMixin,
    LabelSpec,
)


def _fake_forge(*, auth_error: ForgeAuthError | None = None, result=None):
    forge = MagicMock()
    if auth_error is not None:
        forge.check_auth.side_effect = auth_error
    if result is not None:
        forge.ensure_labels.return_value = result
    return forge


class TestRunBootstrap:
    def test_returns_1_and_prints_error_on_auth_failure(self, capsys):
        forge = _fake_forge(auth_error=ForgeAuthError("boom"))

        exit_code = run_bootstrap(forge=forge)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "[FAIL] Error: boom" in captured.err
        forge.ensure_labels.assert_not_called()

    def test_returns_0_and_prints_summary_on_success(self, capsys):
        forge = _fake_forge(
            result=BootstrapResult(created_labels=("a",), existing_labels=("b", "c"))
        )

        exit_code = run_bootstrap(forge=forge)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "[OK] Labels created: 1" in captured.out
        assert "[INFO] Labels already present: 2" in captured.out

    def test_returns_1_and_prints_error_when_label_listing_may_be_truncated(
        self, capsys
    ):
        forge = MagicMock()
        forge.ensure_labels.side_effect = ForgeError(
            "既存ラベル一覧の取得件数が上限に達しました。取得が打ち切られた可能性があります。"
        )

        exit_code = run_bootstrap(forge=forge)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "打ち切" in captured.err

    def test_uses_injected_forge(self, fake_forge):
        fake_forge.ensure_labels.return_value = BootstrapResult(
            created_labels=(), existing_labels=()
        )

        exit_code = run_bootstrap(forge=fake_forge)

        fake_forge.check_auth.assert_called_once_with()
        fake_forge.ensure_labels.assert_called_once_with(REQUIRED_LABELS)
        assert exit_code == 0


class TestMain:
    def test_exits_with_run_bootstrap_return_code(self):
        with (
            patch("orchestune.bootstrap.run_bootstrap", autospec=True, return_value=1),
            patch("sys.argv", ["orchestune-bootstrap"]),
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1


@pytest.mark.integration
class TestEmptyRepoInit:
    def test_empty_repo_initialization(self, tmp_path):
        import subprocess

        # リモート用のベアリポジトリを作成
        remote_dir = tmp_path / "remote.git"
        remote_dir.mkdir()
        subprocess.run(["git", "init", "--bare"], cwd=str(remote_dir), check=True)

        # ローカル用のリポジトリを作成
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(local_dir), check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote_dir)],
            cwd=str(local_dir),
            check=True,
        )

        # コミットがないことを確認
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(local_dir), capture_output=True
        )
        assert res.returncode != 0

        forge = _fake_forge(
            result=BootstrapResult(created_labels=(), existing_labels=())
        )

        # run_bootstrap を実行する
        # この時点では cwd 引数をサポートしていないため、エラーになるか、あるいは初期コミットが作成されないはず
        exit_code = run_bootstrap(forge=forge, cwd=local_dir)

        assert exit_code == 0

        # 初期コミットが作成されていることを確認
        res_log = subprocess.run(
            ["git", "log", "-n", "1", "--oneline"],
            cwd=str(local_dir),
            capture_output=True,
            text=True,
        )
        assert res_log.returncode == 0
        assert "Initial commit" in res_log.stdout

        # README.md が作成されていることを確認
        assert (local_dir / "README.md").exists()

        # リモートに push されていることを確認
        res_remote = subprocess.run(
            ["git", "ls-remote", "origin", "refs/heads/main"],
            cwd=str(local_dir),
            capture_output=True,
            text=True,
        )
        assert "refs/heads/main" in res_remote.stdout

    def test_empty_repo_initialization_fails_with_pre_staged_files(
        self, tmp_path, capsys
    ):
        import subprocess

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(local_dir), check=True)

        # 事前にステージされたファイルを作成
        secret_file = local_dir / "secret.txt"
        secret_file.write_text("sensitive content")
        subprocess.run(["git", "add", "secret.txt"], cwd=str(local_dir), check=True)

        forge = _fake_forge(
            result=BootstrapResult(created_labels=(), existing_labels=())
        )

        exit_code = run_bootstrap(forge=forge, cwd=local_dir)

        # 事前ステージされたファイルがある場合は失敗（1）することを期待する
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "staged" in captured.err.lower()

    def test_empty_repo_initialization_surfaces_git_stderr_on_push_failure(
        self, tmp_path, capsys
    ):
        """#294レビュー対応: run_git移行後もgitコマンド失敗時の実際のエラー
        メッセージ（stderr）がユーザーに見えること（終了コードのみにならない）。"""
        import subprocess

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(local_dir), check=True)
        # 存在しないリモートを指すoriginを設定し、pushを確実に失敗させる。
        subprocess.run(
            ["git", "remote", "add", "origin", str(tmp_path / "does-not-exist.git")],
            cwd=str(local_dir),
            check=True,
        )

        forge = _fake_forge(
            result=BootstrapResult(created_labels=(), existing_labels=())
        )

        exit_code = run_bootstrap(forge=forge, cwd=local_dir)

        assert exit_code == 1
        captured = capsys.readouterr()
        # 単に終了コードだけでなく、gitの実際のエラー詳細が含まれていること。
        assert (
            "does-not-exist.git" in captured.err or "not found" in captured.err.lower()
        )


class _GhScript:
    """admin.py の gh 呼び出しだけを置き換える。"""

    def __init__(
        self,
        *,
        effective: str = "owner/repo",
        view_code: int = 0,
        view_stdout: str | None = None,
        fail_create: str | None = None,
    ) -> None:
        self.effective = effective
        self.view_code = view_code
        self.view_stdout = view_stdout
        self.fail_create = fail_create
        self.created: list[str] = []
        self.calls: list[list[str]] = []
        self.view_cwd: Path | None = None

    def __call__(
        self, argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        self.calls.append(command)
        if command[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["gh", "repo", "view"]:
            cwd = _kwargs.get("cwd")
            self.view_cwd = cwd if isinstance(cwd, Path) else None
            body = self.view_stdout
            if body is None:
                body = json.dumps(
                    {
                        "nameWithOwner": self.effective,
                        "url": f"https://github.com/{self.effective}",
                    }
                )
            return subprocess.CompletedProcess(command, self.view_code, body, "no repo")
        if command[:3] == ["gh", "label", "list"]:
            body = json.dumps([{"name": name} for name in self.created])
            return subprocess.CompletedProcess(command, 0, body, "")
        if command[:3] == ["gh", "label", "create"]:
            name = command[3]
            if name == self.fail_create:
                self.fail_create = None
                raise subprocess.CalledProcessError(1, command, stderr="create failed")
            self.created.append(name)
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(command)

    def label_commands(self) -> list[list[str]]:
        return [command for command in self.calls if command[1] == "label"]


class _AuthOnlyForge:
    def __init__(self) -> None:
        self.ensured = False

    def check_auth(self) -> None:
        return None

    def ensure_labels(
        self, labels: object, repository: str | None = None
    ) -> BootstrapResult:
        self.ensured = True
        raise AssertionError((labels, repository))


class _UnusableRepositoryForge(_AuthOnlyForge):
    def read_effective_repository(self) -> str:
        return "not a slug"


def _git_with_origin(tmp_path: Path, origin: str) -> Path:
    local = tmp_path / "local"
    local.mkdir()
    subprocess.run(["git", "init"], cwd=local, check=True)
    subprocess.run(["git", "remote", "add", "origin", origin], cwd=local, check=True)
    return local


def _prepare(cwd: Path, script: _GhScript) -> int:
    real_run = subprocess.run

    def run(argv, **kwargs):
        if argv and argv[0] == "git":
            return real_run(argv, **kwargs)
        return script(argv, **kwargs)

    with patch.object(forge_admin.subprocess, "run", side_effect=run):
        return prepare_github_labels(cwd=cwd)


@pytest.mark.integration
class TestPrepareGitHubLabels:
    def test_does_not_commit_or_push_an_empty_repository(self, tmp_path: Path) -> None:
        local = _git_with_origin(tmp_path, "git@github.com:owner/repo.git")
        script = _GhScript()
        script.created = [label.name for label in REQUIRED_LABELS]

        exit_code = _prepare(local, script)

        assert exit_code == 0
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=local)
        assert head.returncode != 0
        assert not (local / "README.md").exists()
        listed = script.label_commands()
        assert listed
        assert all(
            command[command.index("--repo") + 1] == "github.com/owner/repo"
            for command in listed
        )
        assert [command[2] for command in listed] == ["list"]
        assert script.view_cwd == local

    def test_refuses_when_effective_repository_differs(
        self, tmp_path: Path, capsys
    ) -> None:
        local = _git_with_origin(tmp_path, "https://github.com/owner/repo.git")
        script = _GhScript(effective="other/repo")

        exit_code = _prepare(local, script)

        assert exit_code == 1
        assert script.label_commands() == []
        captured = capsys.readouterr()
        assert "owner/repo" in captured.err
        assert "other/repo" in captured.err

    def test_refuses_when_origin_is_not_a_github_repository(
        self, tmp_path: Path
    ) -> None:
        local = _git_with_origin(tmp_path, "https://gitlab.com/owner/repo.git")
        script = _GhScript()

        exit_code = _prepare(local, script)

        assert exit_code == 1
        assert script.calls == []

    @pytest.mark.parametrize(
        "origin",
        [
            "https://notgithub.com/owner/repo.git",
            "/mirrors/github.com/owner/repo.git",
            "https://www.github.com/owner/repo.git",
        ],
    )
    def test_refuses_when_origin_host_is_not_github_com(
        self, tmp_path: Path, origin: str
    ) -> None:
        local = _git_with_origin(tmp_path, origin)
        script = _GhScript(effective="owner/repo")

        exit_code = _prepare(local, script)

        assert exit_code == 1
        assert script.calls == []

    def test_accepts_ssh_url_with_github_host(self, tmp_path: Path) -> None:
        local = _git_with_origin(tmp_path, "ssh://git@github.com/owner/repo.git")
        script = _GhScript()
        script.created = [label.name for label in REQUIRED_LABELS]

        exit_code = _prepare(local, script)

        assert exit_code == 0
        assert script.view_cwd == local
        listed = script.label_commands()
        assert listed
        assert all(
            command[command.index("--repo") + 1] == "github.com/owner/repo"
            for command in listed
        )

    def test_refuses_when_effective_host_is_not_github_com(
        self, tmp_path: Path
    ) -> None:
        local = _git_with_origin(tmp_path, "https://github.com/owner/repo.git")
        script = _GhScript(
            view_stdout=json.dumps(
                {
                    "nameWithOwner": "owner/repo",
                    "url": "https://ghe.example/owner/repo",
                }
            )
        )

        exit_code = _prepare(local, script)

        assert exit_code == 1
        assert script.label_commands() == []

    def test_refuses_when_effective_repository_cannot_be_read(
        self, tmp_path: Path
    ) -> None:
        local = _git_with_origin(tmp_path, "https://github.com/owner/repo.git")
        script = _GhScript(view_code=1, view_stdout="")

        exit_code = _prepare(local, script)

        assert exit_code == 1
        assert script.label_commands() == []

    def test_binds_checkout_spelling_when_only_case_differs(
        self, tmp_path: Path
    ) -> None:
        local = _git_with_origin(tmp_path, "https://github.com/Saltmu/Orchestune.git")
        script = _GhScript(effective="saltmu/orchestune")

        exit_code = _prepare(local, script)

        assert exit_code == 0
        listed = script.label_commands()
        assert listed
        assert all(
            command[command.index("--repo") + 1] == "github.com/Saltmu/Orchestune"
            for command in listed
        )

    def test_retry_creates_only_labels_missing_after_partial_failure(
        self, tmp_path: Path
    ) -> None:
        local = _git_with_origin(tmp_path, "https://github.com/owner/repo.git")
        failed = REQUIRED_LABELS[1].name
        script = _GhScript(fail_create=failed)

        first = _prepare(local, script)
        created_before_retry = list(script.created)
        second = _prepare(local, script)

        assert first == 1
        assert second == 0
        assert created_before_retry == [REQUIRED_LABELS[0].name]
        assert script.created == [label.name for label in REQUIRED_LABELS]
        assert script.created.count(REQUIRED_LABELS[0].name) == 1

    def test_refuses_when_origin_is_missing(self, tmp_path: Path) -> None:
        local = tmp_path / "local"
        local.mkdir()
        subprocess.run(["git", "init"], cwd=local, check=True)
        script = _GhScript()

        exit_code = _prepare(local, script)

        assert exit_code == 1
        assert script.calls == []

    def test_refuses_when_origin_url_is_not_owner_and_name(
        self, tmp_path: Path
    ) -> None:
        local = _git_with_origin(tmp_path, "https://github.com/owner/repo/extra")
        script = _GhScript()

        exit_code = _prepare(local, script)

        assert exit_code == 1
        assert script.calls == []

    def test_accepts_origin_without_git_suffix(self, tmp_path: Path) -> None:
        local = _git_with_origin(tmp_path, "https://github.com/owner/repo")
        script = _GhScript()
        script.created = [label.name for label in REQUIRED_LABELS]

        exit_code = _prepare(local, script)

        assert exit_code == 0
        assert script.label_commands()

    def test_refuses_when_forge_cannot_report_effective_repository(
        self, tmp_path: Path
    ) -> None:
        local = _git_with_origin(tmp_path, "https://github.com/owner/repo.git")
        forge = _AuthOnlyForge()

        exit_code = prepare_github_labels(forge=forge, cwd=local)

        assert exit_code == 1
        assert forge.ensured is False

    def test_refuses_when_reported_repository_is_unusable(self, tmp_path: Path) -> None:
        local = _git_with_origin(tmp_path, "https://github.com/owner/repo.git")
        forge = _UnusableRepositoryForge()

        exit_code = prepare_github_labels(forge=forge, cwd=local)

        assert exit_code == 1
        assert forge.ensured is False

    def test_refuses_when_effective_repository_json_is_unusable(
        self, tmp_path: Path
    ) -> None:
        local = _git_with_origin(tmp_path, "https://github.com/owner/repo.git")
        script = _GhScript(view_stdout="{")

        exit_code = _prepare(local, script)

        assert exit_code == 1
        assert script.label_commands() == []

    def test_refuses_when_origin_cannot_be_executed(self, tmp_path: Path) -> None:
        with patch.object(
            bootstrap_module, "run_git", side_effect=OSError("git missing")
        ):
            exit_code = prepare_github_labels(cwd=tmp_path)

        assert exit_code == 1

    @pytest.mark.parametrize("approved", [True, False])
    def test_confirms_verified_repository_before_label_writes(
        self, tmp_path: Path, approved: bool
    ) -> None:
        local = _git_with_origin(tmp_path, "https://github.com/Owner/Repo.git")
        forge = MagicMock()
        forge.read_effective_repository.return_value = "owner/repo"

        def confirm(repository: str) -> bool:
            assert repository == "Owner/Repo"
            forge.check_auth.assert_called_once_with()
            forge.read_effective_repository.assert_called_once_with(cwd=local)
            forge.ensure_labels.assert_not_called()
            return approved

        assert prepare_github_labels(forge=forge, cwd=local, confirm=confirm) == 0
        if approved:
            forge.ensure_labels.assert_called_once_with(
                REQUIRED_LABELS, repository="Owner/Repo"
            )
        else:
            forge.ensure_labels.assert_not_called()

    def test_auth_failure_does_not_create_labels(self, tmp_path: Path, capsys) -> None:
        local = _git_with_origin(tmp_path, "https://github.com/owner/repo.git")
        forge = MagicMock()
        forge.check_auth.side_effect = ForgeAuthError("boom")

        confirm = MagicMock()
        exit_code = prepare_github_labels(forge=forge, cwd=local, confirm=confirm)

        assert exit_code == 1
        assert "boom" in capsys.readouterr().err
        confirm.assert_not_called()
        forge.ensure_labels.assert_not_called()
        forge.read_effective_repository.assert_not_called()


class TestRepositoryBinding:
    def test_rejects_repository_before_subprocess(self) -> None:
        labels = (LabelSpec("status:queued", "0E8A16", "queued"),)
        with patch.object(forge_admin.subprocess, "run") as run:
            with pytest.raises(ValueError, match="リポジトリ"):
                GitHubRepoAdminMixin().ensure_labels(labels, repository="not a repo")
        run.assert_not_called()

    def test_read_effective_repository_rejects_unusable_slug(self) -> None:
        with patch.object(forge_admin.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, stdout='{"nameWithOwner": "not a slug"}', stderr=""
            )
            with pytest.raises(ForgeError, match="照合"):
                GitHubRepoAdminMixin().read_effective_repository()

    def test_read_effective_repository_views_the_checkout(self, tmp_path: Path) -> None:
        with patch.object(forge_admin.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                [],
                0,
                stdout=(
                    '{"nameWithOwner": "owner/repo",'
                    ' "url": "https://github.com/owner/repo"}'
                ),
                stderr="",
            )
            slug = GitHubRepoAdminMixin().read_effective_repository(cwd=tmp_path)
        assert slug == "owner/repo"
        assert run.call_args.kwargs["cwd"] == tmp_path
