from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestune.infra import python_env
from orchestune.infra.execution_deadline import (
    CleanupBudget,
    ExecutionScope,
    activate_scope,
)
from orchestune.infra.managed_process import (
    ManagedProcessResult,
    ManagedProcessRunner,
    ManagedProcessSpec,
    ProcessOutcome,
)


def _completed(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout)


class TestInstallDependencies:
    def test_skips_repository_without_pyproject(self, tmp_path: Path) -> None:
        env = {"PATH": "/bin"}

        with patch("orchestune.infra.python_env.subprocess.run") as run:
            error = python_env.install_dependencies(tmp_path, env)

        assert error is None
        run.assert_not_called()

    def test_installs_with_uv(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").touch()
        env = {"PATH": "/bin"}

        with patch(
            "orchestune.infra.python_env.subprocess.run", return_value=_completed()
        ) as run:
            error = python_env.install_dependencies(tmp_path, env)

        assert error is None
        run.assert_called_once_with(
            ["uv", "sync"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
            env=env,
        )

    def test_returns_error_when_uv_sync_fails(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").touch()

        with patch(
            "orchestune.infra.python_env.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["uv", "sync"]),
        ):
            error = python_env.install_dependencies(tmp_path, {})

        assert (
            error
            == "Failed to sync uv dependencies: Command '['uv', 'sync']' returned non-zero exit status 1."
        )


class TestResolveVirtualenvPath:
    def test_uses_repository_venv_created_by_uv(self, tmp_path: Path) -> None:
        repository_root = tmp_path / "repo"
        repository_root.mkdir()
        (repository_root / "pyproject.toml").touch()
        reported_venv = repository_root / ".venv"
        reported_venv.mkdir()

        with patch("orchestune.infra.python_env.subprocess.run") as run:
            resolved = python_env.resolve_virtualenv_path(
                repository_root, tmp_path / "original", {}
            )

        assert resolved == reported_venv
        run.assert_not_called()

    def test_prefers_repository_venv_over_original_root(self, tmp_path: Path) -> None:
        repository_root = tmp_path / "repo"
        repository_root.mkdir()
        (repository_root / "pyproject.toml").touch()
        repository_venv = repository_root / ".venv"
        repository_venv.mkdir()
        original_root = tmp_path / "original"
        (original_root / ".venv").mkdir(parents=True)

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

    def test_returns_none_without_a_nearby_venv(self, tmp_path: Path) -> None:
        repository_root = tmp_path / "repo"
        repository_root.mkdir()
        (repository_root / "pyproject.toml").touch()
        original_root = tmp_path / "original"
        original_root.mkdir()

        resolved = python_env.resolve_virtualenv_path(
            repository_root, original_root, {}
        )

        assert resolved is None

    def test_falls_back_to_original_root_venv(self, tmp_path: Path) -> None:
        repository_root = tmp_path / "repo"
        repository_root.mkdir()
        (repository_root / "pyproject.toml").touch()
        original_root = tmp_path / "original"
        original_venv = original_root / ".venv"
        original_venv.mkdir(parents=True)

        resolved = python_env.resolve_virtualenv_path(
            repository_root, original_root, {}
        )

        assert resolved == original_venv


class _RecordingRunner:
    """Captures the managed command spec and answers with a canned result."""

    def __init__(self, outcome: ProcessOutcome, **fields: object) -> None:
        self.outcome = outcome
        self.fields = fields
        self.specs: list[ManagedProcessSpec] = []

    def run(self, spec: ManagedProcessSpec) -> ManagedProcessResult:
        self.specs.append(spec)
        defaults: dict[str, object] = {
            "returncode": 0 if self.outcome is ProcessOutcome.SUCCESS else 1,
            "stop_confirmed": True,
            "detail": "",
        }
        defaults.update(self.fields)
        return ManagedProcessResult(
            outcome=self.outcome,
            stage=spec.stage,
            elapsed_seconds=0.0,
            timeout_seconds=spec.timeout_seconds,
            **defaults,  # type: ignore[arg-type]
        )


class TestSyncDependenciesBounded:
    def test_skips_a_repository_without_pyproject(self, tmp_path: Path) -> None:
        runner = _RecordingRunner(ProcessOutcome.SUCCESS)

        assert python_env.sync_dependencies(tmp_path, {}, runner=runner) is None
        assert runner.specs == []

    def test_runs_uv_sync_once_under_the_given_limit(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").touch()
        env = {"PATH": "/bin"}
        runner = _RecordingRunner(ProcessOutcome.SUCCESS)

        result = python_env.sync_dependencies(
            tmp_path, env, timeout_seconds=42, runner=runner
        )

        assert result is not None and result.ok
        (spec,) = runner.specs
        assert list(spec.args) == ["uv", "sync"]
        assert spec.stage == "dependency"
        assert spec.timeout_seconds == 42
        assert spec.cwd == tmp_path
        assert spec.env == env

    def test_the_default_limit_is_finite(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").touch()
        runner = _RecordingRunner(ProcessOutcome.SUCCESS)

        python_env.sync_dependencies(tmp_path, {}, runner=runner)

        assert (
            runner.specs[0].timeout_seconds == python_env.FALLBACK_SYNC_TIMEOUT_SECONDS
        )

    def test_an_active_scope_caps_the_limit_to_the_remaining_cycle(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "pyproject.toml").touch()
        runner = _RecordingRunner(ProcessOutcome.SUCCESS)
        now = 100.0
        scope = ExecutionScope(
            cycle_seconds=15,
            cleanup_seconds=5,
            command_seconds=5,
            clock=lambda: now,
        )
        now += 3.0

        with activate_scope(scope):
            python_env.sync_dependencies(
                tmp_path, {}, timeout_seconds=600, runner=runner
            )

        assert runner.specs[0].timeout_seconds == 12

    @pytest.mark.parametrize(
        ("outcome", "fields", "expected"),
        [
            (ProcessOutcome.TIMED_OUT, {}, "uv sync timed out after 5s"),
            (
                ProcessOutcome.STOP_UNCONFIRMED,
                {"detail": "still alive", "stop_confirmed": False},
                "could not be confirmed stopped: still alive",
            ),
            (ProcessOutcome.START_FAILED, {"detail": "no uv"}, "no uv"),
            (
                ProcessOutcome.NONZERO_EXIT,
                {"returncode": 2},
                "returned non-zero exit status 2",
            ),
        ],
    )
    def test_the_compat_wrapper_names_the_cause(
        self,
        tmp_path: Path,
        outcome: ProcessOutcome,
        fields: dict[str, object],
        expected: str,
    ) -> None:
        (tmp_path / "pyproject.toml").touch()

        error = python_env.install_dependencies(
            tmp_path,
            {},
            timeout_seconds=5,
            runner=_RecordingRunner(outcome, **fields),
        )

        assert error is not None
        assert error.startswith("Failed to sync uv dependencies: ")
        assert expected in error

    @pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX uv shim")
    def test_a_hanging_uv_is_stopped_by_the_real_runner(self, tmp_path: Path) -> None:
        shim_dir = tmp_path / "bin"
        shim_dir.mkdir()
        shim = shim_dir / "uv"
        shim.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(120)\n")
        shim.chmod(0o755)
        (tmp_path / "pyproject.toml").touch()
        env = {"PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
        started = time.monotonic()

        result = python_env.sync_dependencies(
            tmp_path,
            env,
            timeout_seconds=1,
            cleanup=CleanupBudget(5),
            runner=ManagedProcessRunner(),
        )

        assert result is not None
        assert result.outcome is ProcessOutcome.TIMED_OUT
        assert result.stop_confirmed is True
        assert result.stage == "dependency"
        assert time.monotonic() - started < 20
