from unittest.mock import MagicMock, patch

import pytest

from orchestune.bootstrap import main, run_bootstrap
from orchestune.forge import BootstrapResult, ForgeAuthError


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
        assert "boom" in captured.err
        forge.ensure_labels.assert_not_called()

    def test_returns_0_and_prints_summary_on_success(self, capsys):
        forge = _fake_forge(
            result=BootstrapResult(created_labels=("a",), existing_labels=("b", "c"))
        )

        exit_code = run_bootstrap(forge=forge)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Labels created: 1" in captured.out
        assert "Labels already present: 2" in captured.out

    def test_uses_github_forge_by_default(self):
        with patch("orchestune.bootstrap.GitHubForge") as mock_forge_cls:
            mock_forge_cls.return_value = _fake_forge(
                result=BootstrapResult(created_labels=(), existing_labels=())
            )
            exit_code = run_bootstrap()

        mock_forge_cls.assert_called_once()
        assert exit_code == 0


class TestMain:
    def test_exits_with_run_bootstrap_return_code(self):
        with (
            patch("orchestune.bootstrap.run_bootstrap", return_value=1),
            patch("sys.argv", ["orchestune-bootstrap"]),
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1


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
