"""Real-process CI observation of a Layer-2 integration attempt (#1362, #1499).

Moved out of ``test_postcycle_semantic_review.py`` to mirror the
:mod:`orchestune.dispatch.semantic_review_ci` split and keep both files within the
test file-size limit.

The CI commands are the running Python interpreter rather than the POSIX ``true`` /
``false`` utilities (#1499): on Windows those resolve to Git for Windows' MSYS
binaries, whose runtime start-up is not reliable under a fully loaded parallel test
run, so the success case failed while ``false`` passed for any reason at all. Every
stage the real runner executes is recorded, so the assertion names the stage and
outcome that actually decided the conclusion.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.postcycle_semantic_review import verify_semantic_ci
from orchestune.infra.managed_process import (
    ManagedProcessResult,
    ManagedProcessRunner,
    ManagedProcessSpec,
    ProcessOutcome,
)
from orchestune.integrator.ci_execution import CI_STAGE
from orchestune.review.semantic_gate import SemanticCiConclusion, SemanticCiObservation

TEMP_BRANCH = "integration/temp-x"
EXIT_SUCCESS = [sys.executable, "-c", "raise SystemExit(0)"]
EXIT_FAILURE = [sys.executable, "-c", "raise SystemExit(1)"]


class RecordingRunner:
    """The real managed runner, keeping every result for the assertion message."""

    def __init__(self) -> None:
        self.results: list[ManagedProcessResult] = []

    def run(self, spec: ManagedProcessSpec) -> ManagedProcessResult:
        result = ManagedProcessRunner().run(spec)
        self.results.append(result)
        return result

    def describe(self) -> str:
        return "; ".join(
            f"stage={r.stage} outcome={r.outcome} returncode={r.returncode} "
            f"elapsed={r.elapsed_seconds:.1f}s detail={r.detail!r} "
            f"stderr={r.stderr_tail[-500:]!r}"
            for r in self.results
        )


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _config(tmp_path: Path, command: list[str]) -> DispatcherConfig:
    return DispatcherConfig(
        parent_issue_number=1033,
        forge=MagicMock(),
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "run_state.json",
        dispatch_target=cast(Any, SimpleNamespace(target_name="claude-cli")),
        apply=True,
        ci_command=command,
    )


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    _git(tmp_path, "init", "--bare", "-q", str(origin))
    _git(tmp_path, "init", "-q", str(work))
    _git(
        work,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "c",
    )
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", f"HEAD:refs/heads/{TEMP_BRANCH}")
    return work, _git(work, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    ("command", "conclusion", "returncode"),
    [
        pytest.param(EXIT_SUCCESS, "success", 0, id="success"),
        pytest.param(EXIT_FAILURE, "failure", 1, id="failure"),
    ],
)
def test_runs_ci_on_the_expected_head(
    repo: tuple[Path, str],
    tmp_path: Path,
    command: list[str],
    conclusion: str,
    returncode: int,
) -> None:
    work, sha = repo
    runner = RecordingRunner()
    observed = verify_semantic_ci(
        TEMP_BRANCH,
        sha,
        config=_config(tmp_path, command),
        repository_root=work,
        runner=runner,
    )
    assert observed == SemanticCiObservation(
        sha, SemanticCiConclusion(conclusion)
    ), runner.describe()
    # The conclusion came from the CI command itself, not from a start or stop failure.
    assert [(r.stage, r.returncode) for r in runner.results] == [
        (CI_STAGE, returncode)
    ], runner.describe()
    expected_outcome = (
        ProcessOutcome.SUCCESS if returncode == 0 else ProcessOutcome.NONZERO_EXIT
    )
    assert runner.results[0].outcome is expected_outcome, runner.describe()
    assert "head" not in _git(work, "worktree", "list")


def test_discards_when_the_remote_head_differs(
    repo: tuple[Path, str], tmp_path: Path
) -> None:
    work, _ = repo
    runner = RecordingRunner()
    assert (
        verify_semantic_ci(
            TEMP_BRANCH,
            "f" * 40,
            config=_config(tmp_path, EXIT_SUCCESS),
            repository_root=work,
            runner=runner,
        )
        is None
    )
    assert runner.results == []
