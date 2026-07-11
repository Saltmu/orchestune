import json
from unittest.mock import MagicMock, patch

import pytest

from orchestune.bootstrap import ensure_claude_settings, main, run_bootstrap
from orchestune.forge import BootstrapResult, ForgeAuthError


def _fake_forge(*, auth_error: ForgeAuthError | None = None, result=None):
    forge = MagicMock()
    if auth_error is not None:
        forge.check_auth.side_effect = auth_error
    if result is not None:
        forge.ensure_labels.return_value = result
    return forge


class TestRunBootstrap:
    @patch("orchestune.bootstrap.setup_agy_permissions")
    def test_returns_1_and_prints_error_on_auth_failure(self, mock_setup, capsys):
        forge = _fake_forge(auth_error=ForgeAuthError("boom"))

        exit_code = run_bootstrap(forge=forge)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "boom" in captured.err
        forge.ensure_labels.assert_not_called()
        mock_setup.assert_not_called()

    @patch("orchestune.bootstrap.setup_agy_permissions")
    def test_returns_0_and_prints_summary_on_success(self, mock_setup, capsys):
        forge = _fake_forge(
            result=BootstrapResult(created_labels=("a",), existing_labels=("b", "c"))
        )

        exit_code = run_bootstrap(forge=forge)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Labels created: 1" in captured.out
        assert "Labels already present: 2" in captured.out
        mock_setup.assert_called_once()

    @patch("orchestune.bootstrap.setup_agy_permissions")
    def test_uses_github_forge_by_default(self, mock_setup):
        with patch("orchestune.bootstrap.GitHubForge") as mock_forge_cls:
            mock_forge_cls.return_value = _fake_forge(
                result=BootstrapResult(created_labels=(), existing_labels=())
            )
            exit_code = run_bootstrap()

        mock_forge_cls.assert_called_once()
        assert exit_code == 0


class TestEnsureClaudeSettings:
    def test_creates_settings_file_when_missing(self, tmp_path):
        created = ensure_claude_settings(tmp_path)

        settings_path = tmp_path / ".claude" / "settings.json"
        assert created is True
        assert settings_path.exists()
        data = json.loads(settings_path.read_text())
        allow = data["permissions"]["allow"]
        assert "Bash(git push *)" in allow
        assert "Bash(gh pr merge *)" in allow

    def test_skips_when_settings_file_already_exists(self, tmp_path):
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir()
        settings_path = settings_dir / "settings.json"
        settings_path.write_text('{"custom": true}')

        created = ensure_claude_settings(tmp_path)

        assert created is False
        assert settings_path.read_text() == '{"custom": true}'


class TestRunBootstrapClaudeSettings:
    @patch("orchestune.bootstrap.setup_agy_permissions")
    def test_provisions_claude_settings_on_success(self, mock_setup, tmp_path):
        forge = _fake_forge(
            result=BootstrapResult(created_labels=(), existing_labels=())
        )

        exit_code = run_bootstrap(forge=forge, repo_root=tmp_path)

        assert exit_code == 0
        assert (tmp_path / ".claude" / "settings.json").exists()

    @patch("orchestune.bootstrap.setup_agy_permissions")
    def test_does_not_provision_claude_settings_on_auth_failure(
        self, mock_setup, tmp_path
    ):
        forge = _fake_forge(auth_error=ForgeAuthError("boom"))

        run_bootstrap(forge=forge, repo_root=tmp_path)

        assert not (tmp_path / ".claude" / "settings.json").exists()


class TestMain:
    def test_exits_with_run_bootstrap_return_code(self):
        with (
            patch("orchestune.bootstrap.run_bootstrap", return_value=1),
            patch("sys.argv", ["orchestune-bootstrap"]),
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1


class TestSetupAgyPermissions:
    @patch("shutil.which")
    def test_setup_agy_permissions_success(self, mock_which, tmp_path):
        from orchestune.bootstrap import setup_agy_permissions

        # agy/git/gh が存在する場合
        mock_which.side_effect = (
            lambda cmd: "/usr/bin/" + cmd if cmd in ("agy", "git", "gh") else None
        )

        mock_home = tmp_path / "home"
        mock_home.mkdir()
        projects_dir = mock_home / ".gemini" / "config" / "projects"
        projects_dir.mkdir(parents=True)

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        repo_uri = f"file://{repo_root.resolve()}"

        project_file = projects_dir / "test-project.json"
        project_file.write_text(
            json.dumps(
                {
                    "id": "test-project",
                    "name": "Test Project",
                    "projectResources": {
                        "resources": [{"gitFolder": {"folderUri": repo_uri}}]
                    },
                }
            )
        )

        setup_agy_permissions(repo_root, mock_home)

        updated_data = json.loads(project_file.read_text())
        allow_list = updated_data["permissionGrants"]["permissionGrants"]["allow"]
        assert "command(git)" in allow_list
        assert "command(gh)" in allow_list
        assert "command(claude)" not in allow_list  # claude は which で None

    @patch("shutil.which")
    def test_claude_command_added_when_exists(self, mock_which, tmp_path):
        from orchestune.bootstrap import setup_agy_permissions

        # agy と claude の両方が存在する場合
        mock_which.side_effect = (
            lambda cmd: "/usr/bin/" + cmd if cmd in ("agy", "claude") else None
        )

        mock_home = tmp_path / "home"
        mock_home.mkdir()
        projects_dir = mock_home / ".gemini" / "config" / "projects"
        projects_dir.mkdir(parents=True)

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        repo_uri = f"file://{repo_root.resolve()}"

        project_file = projects_dir / "test-project.json"
        project_file.write_text(
            json.dumps(
                {
                    "id": "test-project",
                    "projectResources": {
                        "resources": [{"gitFolder": {"folderUri": repo_uri}}]
                    },
                }
            )
        )

        setup_agy_permissions(repo_root, mock_home)

        updated_data = json.loads(project_file.read_text())
        allow_list = updated_data["permissionGrants"]["permissionGrants"]["allow"]
        # claude が存在する場合は command(claude) が許可リストに追加される
        assert "command(claude)" in allow_list

    @patch("shutil.which")
    def test_skipped_when_agy_and_gemini_not_present(self, mock_which, tmp_path):
        from orchestune.bootstrap import setup_agy_permissions

        # 何も存在しない
        mock_which.return_value = None

        mock_home = tmp_path / "home"
        mock_home.mkdir()

        repo_root = tmp_path / "repo"
        repo_root.mkdir()

        setup_agy_permissions(repo_root, mock_home)

        # .gemini ディレクトリが作成されていないことを確認
        assert not (mock_home / ".gemini").exists()

    @patch("shutil.which")
    def test_no_duplicate_permissions(self, mock_which, tmp_path):
        from orchestune.bootstrap import setup_agy_permissions

        mock_which.side_effect = lambda cmd: "/usr/bin/" + cmd if cmd == "git" else None

        mock_home = tmp_path / "home"
        mock_home.mkdir()
        projects_dir = mock_home / ".gemini" / "config" / "projects"
        projects_dir.mkdir(parents=True)

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        repo_uri = f"file://{repo_root.resolve()}"

        # 既に command(git) が存在する状態
        project_file = projects_dir / "test-project.json"
        project_file.write_text(
            json.dumps(
                {
                    "id": "test-project",
                    "projectResources": {
                        "resources": [{"gitFolder": {"folderUri": repo_uri}}]
                    },
                    "permissionGrants": {
                        "permissionGrants": {"allow": ["command(git)"]}
                    },
                }
            )
        )

        setup_agy_permissions(repo_root, mock_home)

        updated_data = json.loads(project_file.read_text())
        allow_list = updated_data["permissionGrants"]["permissionGrants"]["allow"]
        # 重複なく1件のみ
        assert allow_list.count("command(git)") == 1
