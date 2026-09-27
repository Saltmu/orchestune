"""Isolate process environment settings inherited by the test suite."""

import pytest

from orchestune.infra.git_cli import DANGEROUS_GIT_ENV_VARS


@pytest.fixture(autouse=True)
def _isolate_git_env(monkeypatch: pytest.MonkeyPatch):
    """Ensure tests run in an isolated Git environment where GIT_* variables are stripped."""
    for var in DANGEROUS_GIT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_jev_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Require tests to opt into Jev instead of using real session credentials,

    and isolate Jev log output to a temporary directory by default.
    """
    for name in (
        "JEV_API_KEY",
        "JEV_BASE_URL",
        "JEV_API_URL",
        "JEV_THRESHOLD",
    ):
        monkeypatch.delenv(name, raising=False)

    tmp_jev_log = tmp_path_factory.mktemp("jev_isolated") / "evaluations.jsonl"
    monkeypatch.setenv("JEV_LOG_PATH", str(tmp_jev_log))
