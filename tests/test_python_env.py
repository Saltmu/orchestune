from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from orchestune.infra import python_env


def _completed(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout)


class TestInstallDependencies:
    def test_skips_repository_without_pyproject(self, tmp_path: Path) -> None:
        env = {"PATH": "/bin"}

        with patch("orchestune.infra.python_env.subprocess.run") as run:
            error = python_env.install_dependencies(tmp_path, env)

        assert error is None
        run.assert_not_called()

    def test_installs_with_poetry(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").touch()
        env = {"PATH": "/bin"}

        with patch(
            "orchestune.infra.python_env.subprocess.run", return_value=_completed()
        ) as run:
            error = python_env.install_dependencies(tmp_path, env)

        assert error is None
        run.assert_called_once_with(
            ["poetry", "install"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
            env=env,
        )

    def test_returns_error_when_poetry_install_fails(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").touch()

        with patch(
            "orchestune.infra.python_env.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["poetry", "install"]),
        ):
            error = python_env.install_dependencies(tmp_path, {})

        assert (
            error
            == "Failed to install Poetry dependencies: Command '['poetry', 'install']' returned non-zero exit status 1."
        )


class TestResolveVirtualenvPath:
    def test_uses_path_reported_by_poetry(self, tmp_path: Path) -> None:
        repository_root = tmp_path / "repo"
        repository_root.mkdir()
        (repository_root / "pyproject.toml").touch()
        reported_venv = tmp_path / "managed-venv"
        reported_venv.mkdir()

        with patch(
            "orchestune.infra.python_env.subprocess.run",
            return_value=_completed(f"{reported_venv}\n"),
        ) as run:
            resolved = python_env.resolve_virtualenv_path(
                repository_root, tmp_path / "original", {}
            )

        assert resolved == reported_venv
        run.assert_called_once_with(
            ["poetry", "env", "info", "--path"],
            cwd=str(repository_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            env={},
        )

    def test_falls_back_to_repository_venv_when_poetry_cannot_resolve(
        self, tmp_path: Path
    ) -> None:
        repository_root = tmp_path / "repo"
        repository_root.mkdir()
        (repository_root / "pyproject.toml").touch()
        repository_venv = repository_root / ".venv"
        repository_venv.mkdir()
        original_root = tmp_path / "original"
        (original_root / ".venv").mkdir(parents=True)

        with patch(
            "orchestune.infra.python_env.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["poetry", "env"]),
        ):
            resolved = python_env.resolve_virtualenv_path(
                repository_root, original_root, {}
            )

        assert resolved == repository_venv

    def test_falls_back_to_nearest_ancestor_venv(self, tmp_path: Path) -> None:
        repository_root = tmp_path / "repo"
        repository_root.mkdir()
        original_root = tmp_path / "workspace" / "nested" / "project"
        original_root.mkdir(parents=True)
        nearest_venv = tmp_path / "workspace" / ".venv"
        nearest_venv.mkdir()
        (tmp_path / ".venv").mkdir()

        resolved = python_env.resolve_virtualenv_path(
            repository_root, original_root, {}
        )

        assert resolved == nearest_venv

    def test_ignores_nonexistent_poetry_path_and_returns_none(
        self, tmp_path: Path
    ) -> None:
        repository_root = tmp_path / "repo"
        repository_root.mkdir()
        (repository_root / "pyproject.toml").touch()
        original_root = tmp_path / "original"
        original_root.mkdir()

        with patch(
            "orchestune.infra.python_env.subprocess.run",
            return_value=_completed(f"{tmp_path / 'missing'}\n"),
        ):
            resolved = python_env.resolve_virtualenv_path(
                repository_root, original_root, {}
            )

        assert resolved is None

    def test_falls_back_when_poetry_env_info_raises(self, tmp_path: Path) -> None:
        repository_root = tmp_path / "repo"
        repository_root.mkdir()
        (repository_root / "pyproject.toml").touch()
        original_root = tmp_path / "original"
        original_venv = original_root / ".venv"
        original_venv.mkdir(parents=True)

        with patch(
            "orchestune.infra.python_env.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["poetry", "env"]),
        ):
            resolved = python_env.resolve_virtualenv_path(
                repository_root, original_root, {}
            )

        assert resolved == original_venv
