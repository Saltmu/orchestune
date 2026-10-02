"""Isolate process environment settings inherited by the test suite."""

import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from orchestune.infra.git_cli import DANGEROUS_GIT_ENV_VARS


@pytest.fixture
def worktree_tmp_path(request: pytest.FixtureRequest) -> Iterator[Path]:
    """Create test scratch beneath this worktree to exercise ancestor discovery."""
    path = (
        Path(request.config.rootpath)
        / ".orchestune"
        / "tmp"
        / f"pytest-worktree-{uuid.uuid4().hex}"
    )
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolate_git_env(monkeypatch: pytest.MonkeyPatch):
    """Ensure tests run in an isolated Git environment where GIT_* variables are stripped."""
    for var in DANGEROUS_GIT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_github_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ambient GitHub credentials and host overrides out of each test."""
    for name in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GH_HOST",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _stub_jev_context_for_review_result_tests(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep Jev review-result tests from fetching PR metadata with the real gh CLI."""
    test_class = getattr(request.node, "cls", None)
    if (
        getattr(test_class, "__name__", None)
        != "TestJevFilterIntegrationWithWaitForReview"
    ):
        return

    from scripts.jev_context import JevReviewContext

    monkeypatch.setattr(
        "scripts.wait_for_review.collect_review_context",
        lambda pr_number: JevReviewContext(pr={"number": pr_number}, missing=["pr"]),
    )


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
