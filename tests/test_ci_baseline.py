"""Tests for the agent-local CI baseline wrapper."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "ci_baseline.py"


@pytest.fixture
def ci_baseline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("ci_baseline_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        module, "BASELINE_PATH", tmp_path / ".orchestune" / "baseline.json"
    )
    monkeypatch.setattr(module, "_merge_base", lambda _base_branch: "base-sha")
    return module


def _ci_result(exit_code: int, output: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["./scripts/local-ci.sh"], exit_code, output, "")


def test_default_base_branch_is_origin_main(ci_baseline: Any) -> None:
    assert ci_baseline._parse_args(["check"]).base_branch == "origin/main"
    assert ci_baseline._parse_args(["check"]).no_format is False


def test_check_without_baseline_uses_absolute_ci_exit_code(
    ci_baseline: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ci_baseline.subprocess, "run", lambda *args, **kwargs: _ci_result(1, "broken\n")
    )

    assert ci_baseline.main(["check", "--no-format"]) == 1
    assert not ci_baseline.BASELINE_PATH.exists()


def test_check_rerecords_a_stale_baseline(
    ci_baseline: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ci_baseline._write_state(
        {
            "version": ci_baseline.STATE_VERSION,
            "base_sha": "old-base-sha",
            "ci_command": ci_baseline.DEFAULT_CI_COMMAND,
            "baseline": {"exit_code": 0, "failures": []},
            "failure_counts": {"old": 2},
        }
    )
    monkeypatch.setattr(
        ci_baseline.subprocess, "run", lambda *args, **kwargs: _ci_result(1, "broken\n")
    )

    assert ci_baseline.main(["check", "--no-format"]) == 1
    state = ci_baseline._read_state()
    assert state is not None
    assert state["base_sha"] == "base-sha"
    assert state["baseline"] == {
        "exit_code": 1,
        "failures": ["ci-output:cdd6c109503d4e19"],
    }
    assert state["failure_counts"] == {}


def test_check_classifies_only_baseline_failures_as_base_branch_red(
    ci_baseline: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ci_baseline._write_state(
        {
            "version": ci_baseline.STATE_VERSION,
            "base_sha": "base-sha",
            "ci_command": ci_baseline.DEFAULT_CI_COMMAND,
            "baseline": {"exit_code": 1, "failures": ["tests/test_old.py::test_old"]},
            "failure_counts": {},
        }
    )
    monkeypatch.setattr(
        ci_baseline.subprocess,
        "run",
        lambda *args, **kwargs: _ci_result(
            1, "FAILED tests/test_old.py::test_old - broken\n"
        ),
    )

    assert (
        ci_baseline.main(["check", "--no-format"]) == ci_baseline.EXIT_BASE_BRANCH_RED
    )
    state = ci_baseline._read_state()
    assert state is not None
    assert state["failure_counts"] == {"tests/test_old.py::test_old": 1}


def test_check_preserves_failure_counts_between_resumed_runs(
    ci_baseline: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ci_baseline._write_state(
        {
            "version": ci_baseline.STATE_VERSION,
            "base_sha": "base-sha",
            "ci_command": ci_baseline.DEFAULT_CI_COMMAND,
            "baseline": {"exit_code": 1, "failures": ["tests/test_old.py::test_old"]},
            "failure_counts": {"tests/test_old.py::test_old": 1},
        }
    )
    monkeypatch.setattr(
        ci_baseline.subprocess,
        "run",
        lambda *args, **kwargs: _ci_result(
            1, "FAILED tests/test_old.py::test_old - broken\n"
        ),
    )

    assert (
        ci_baseline.main(["check", "--no-format"]) == ci_baseline.EXIT_BASE_BRANCH_RED
    )
    state = ci_baseline._read_state()
    assert state is not None
    assert state["failure_counts"] == {"tests/test_old.py::test_old": 2}


def test_check_returns_original_exit_code_for_a_new_failure(
    ci_baseline: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ci_baseline._write_state(
        {
            "version": ci_baseline.STATE_VERSION,
            "base_sha": "base-sha",
            "ci_command": ci_baseline.DEFAULT_CI_COMMAND,
            "baseline": {"exit_code": 1, "failures": ["tests/test_old.py::test_old"]},
            "failure_counts": {},
        }
    )
    monkeypatch.setattr(
        ci_baseline.subprocess,
        "run",
        lambda *args, **kwargs: _ci_result(
            5, "FAILED tests/test_new.py::test_new - broken\n"
        ),
    )

    assert ci_baseline.main(["check", "--no-format"]) == 5


def test_check_rerecords_when_ci_command_changes(
    ci_baseline: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ci_baseline._write_state(
        {
            "version": ci_baseline.STATE_VERSION,
            "base_sha": "base-sha",
            "ci_command": "old-ci-command",
            "baseline": {"exit_code": 0, "failures": []},
            "failure_counts": {"old": 2},
        }
    )
    monkeypatch.setattr(
        ci_baseline.subprocess, "run", lambda *args, **kwargs: _ci_result(1, "broken\n")
    )

    assert (
        ci_baseline.main(["check", "--ci-command", "new-ci-command", "--no-format"])
        == 1
    )
    state = ci_baseline._read_state()
    assert state is not None
    assert state["ci_command"] == "new-ci-command"
    assert state["failure_counts"] == {}


def test_check_formats_before_running_local_ci(
    ci_baseline: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[tuple[str, ...]] = []

    def run(
        command: Any, *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        command_tuple = tuple(command)
        commands.append(command_tuple)
        exit_code = 1 if command_tuple == ("run-ci",) else 0
        return subprocess.CompletedProcess(command_tuple, exit_code, "", "")

    monkeypatch.setattr(ci_baseline.subprocess, "run", run)

    assert ci_baseline.main(["check", "--ci-command", "run-ci"]) == 1
    assert commands == [*ci_baseline.RUFF_FORMAT_COMMANDS, ("run-ci",)]


def test_check_skips_formatting_when_ci_is_set(
    ci_baseline: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[tuple[str, ...]] = []

    def run(
        command: Any, *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        command_tuple = tuple(command)
        commands.append(command_tuple)
        return subprocess.CompletedProcess(command_tuple, 1, "", "")

    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(ci_baseline.subprocess, "run", run)

    assert ci_baseline.main(["check", "--ci-command", "run-ci"]) == 1
    assert commands == [("run-ci",)]


def test_check_stops_when_formatting_fails(
    ci_baseline: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[tuple[str, ...]] = []

    def run(
        command: Any, *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        command_tuple = tuple(command)
        commands.append(command_tuple)
        return subprocess.CompletedProcess(command_tuple, 23, "", "")

    monkeypatch.setattr(ci_baseline.subprocess, "run", run)

    assert ci_baseline.main(["check", "--ci-command", "run-ci"]) == 23
    assert commands == [ci_baseline.RUFF_FORMAT_COMMANDS[0]]
