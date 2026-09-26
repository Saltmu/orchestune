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
def _isolate_jev_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Require tests to opt into Jev instead of using real session credentials."""
    for name in ("JEV_API_KEY", "JEV_BASE_URL", "JEV_API_URL", "JEV_THRESHOLD"):
        monkeypatch.delenv(name, raising=False)
