"""#1248: Integrator CI never inherits the parent process's ``ORCHESTUNE_*`` variables.

The temporary merge worktree's CI must not write the parent task's CI evidence,
resolve its base from the parent task, or reuse the parent dispatcher's report.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from orchestune.infra.managed_process import (
    ManagedProcessResult,
    ManagedProcessSpec,
    ProcessOutcome,
)
from orchestune.infra.python_env import DEPENDENCY_STAGE
from orchestune.integrator.ci_execution import (
    CI_STAGE,
    prepare_environment,
    run_ci_stages,
)
from orchestune.integrator.timeout_policy import IntegrationExecutionPolicy

LEAKED = {
    "ORCHESTUNE_DISPATCH_REPORT_PATH": "/parent/report.json",
    "ORCHESTUNE_CHILD_REVIEW_GATE": "required",
    "ORCHESTUNE_CI_EVIDENCE_PATH": "/parent/evidence.json",
    "ORCHESTUNE_ROUTINE_TOKEN": "secret",
    "ORCHESTUNE_FUTURE_FLAG": "1",
}


class RecordingRunner:
    """Succeed every managed command and remember its spec."""

    def __init__(self) -> None:
        self.calls: list[ManagedProcessSpec] = []

    def run(self, spec: ManagedProcessSpec) -> ManagedProcessResult:
        self.calls.append(spec)
        return ManagedProcessResult(
            outcome=ProcessOutcome.SUCCESS,
            stage=spec.stage,
            returncode=0,
            elapsed_seconds=0.0,
            timeout_seconds=spec.timeout_seconds,
            stop_confirmed=True,
        )


@pytest.fixture
def leaked_parent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in LEAKED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("KEEP_ME", "kept")


def _repository(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    return tmp_path


def _orchestune_keys(env: dict[str, str]) -> list[str]:
    return sorted(key for key in env if key.startswith("ORCHESTUNE_"))


@pytest.mark.usefixtures("leaked_parent_env")
def test_prepared_environment_drops_every_orchestune_variable(tmp_path: Path):
    repository = _repository(tmp_path)

    env, failure = prepare_environment(
        repository,
        repository,
        policy=IntegrationExecutionPolicy(),
        runner=RecordingRunner(),
    )

    assert failure is None
    assert _orchestune_keys(env) == []
    assert env["KEEP_ME"] == "kept"
    assert env["PYTHON_KEYRING_BACKEND"] == "keyring.backends.null.Keyring"


@pytest.mark.usefixtures("leaked_parent_env")
def test_dependency_and_ci_stages_run_without_orchestune_variables(tmp_path: Path):
    repository = _repository(tmp_path)
    runner = RecordingRunner()

    result = run_ci_stages(
        ["./scripts/local-ci.sh"],
        repository,
        repository,
        policy=IntegrationExecutionPolicy(),
        runner=runner,
    )

    assert result.ok
    assert [spec.stage for spec in runner.calls] == [DEPENDENCY_STAGE, CI_STAGE]
    for spec in runner.calls:
        assert spec.env is not None
        assert _orchestune_keys(dict(spec.env)) == []
        assert spec.env["KEEP_ME"] == "kept"
    for name, value in LEAKED.items():
        assert os.environ[name] == value
