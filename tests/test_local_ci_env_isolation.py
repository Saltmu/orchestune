"""Regression checks for completion context leaking into CI's test runner."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_COMPLETION_CONTEXT = (
    "ORCHESTUNE_EXPECTED_HEAD",
    "ORCHESTUNE_EXPECTED_TREE",
    "ORCHESTUNE_BASE_SHA",
    "ORCHESTUNE_BASE_REF",
    "ORCHESTUNE_STATE_PATH",
    "ORCHESTUNE_ISSUE_NUMBER",
    "ORCHESTUNE_CI_EVIDENCE_PATH",
    "PYTHONWARNDEFAULTENCODING",
)
_PHASES = (
    "preflight",
    "python_dependencies",
    "ruff_format",
    "ruff_lint",
    "mypy",
    "quint_toolchain",
    "pytest",
    "bloat",
    "gitleaks",
    "evidence_record",
)
_TIMING_LINE = re.compile(
    r"^\[local-ci-timing\] phase=([a-z_]+) elapsed_seconds=(\d+) exit_code=(-?\d+)$"
)
_MOCK_TOOL_SOURCE = """\
import json
import os
import sys
from pathlib import Path

NAMES = (
    "ORCHESTUNE_EXPECTED_HEAD",
    "ORCHESTUNE_EXPECTED_TREE",
    "ORCHESTUNE_BASE_SHA",
    "ORCHESTUNE_BASE_REF",
    "ORCHESTUNE_STATE_PATH",
    "ORCHESTUNE_ISSUE_NUMBER",
    "ORCHESTUNE_CI_EVIDENCE_PATH",
    "PYTHONWARNDEFAULTENCODING",
)


def _selected_env() -> dict[str, str]:
    return {name: os.environ[name] for name in NAMES if name in os.environ}


def _fail_if(kind: str) -> None:
    if os.environ.get("CI_FAIL_AT") == kind:
        raise SystemExit(int(os.environ.get("CI_FAIL_CODE", "1")))


args = sys.argv[1:]
if args[:2] == ["run", "mypy"]:
    _fail_if("mypy")
    raise SystemExit(0)
if args[:2] == ["run", "pytest"]:
    key = "CI_PYTEST_ENV"
    summary = (
        Path(os.environ["ORCHESTUNE_QUINT_REPLAY_DIR"])
        / "test_bounded_exploration_replays_on_production"
    )
    summary.mkdir(parents=True, exist_ok=True)
    (summary / "exploration-summary.json").write_text("{}")
    _fail_if("pytest")
elif args[:5] == [
    "run",
    "python",
    "-m",
    "orchestune.complete.ci_evidence",
    "record",
]:
    key = "CI_RECORD_ENV"
    Path(os.environ["CI_RECORD_ARGS"]).write_text(json.dumps(args))
    _fail_if("record")
else:
    raise SystemExit(0)
Path(os.environ[key]).write_text(json.dumps(_selected_env()))
"""


def _prepare_repo(tmp_path: Path, script_name: str) -> Path:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(Path("scripts") / script_name, scripts / script_name)
    # The Quint step (#1276) runs scripts/quint-check.* and creates its replay
    # session directory; this test is about the pytest environment, so the check
    # itself is a stub (the real one is tested in test_quint_dependency_replay.py).
    suffix = Path(script_name).suffix
    shutil.copy2(Path("scripts") / f"create-session-dir{suffix}", scripts)
    (scripts / f"quint-check{suffix}").write_text(
        "exit 0\n" if suffix == ".ps1" else "#!/usr/bin/env bash\nexit 0\n",
        encoding="utf-8",
    )
    (scripts / f"quint-check{suffix}").chmod(0o755)
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
    )
    (repo / "file.txt").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "init",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _prepare_mock_tools(tmp_path: Path, *, include_node: bool) -> tuple[Path, Path]:
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    mock_tool = mock_bin / "mock_uv.py"
    mock_tool.write_text(_MOCK_TOOL_SOURCE, encoding="utf-8")
    names = ["uv", "gitleaks"]
    if include_node:
        names.extend(["node", "npm"])
    if sys.platform == "win32":
        _write_executable(
            mock_bin / "uv.cmd",
            '@echo off\n"%CI_MOCK_PYTHON%" "%CI_MOCK_TOOL%" %*\nexit /b %ERRORLEVEL%\n',
        )
        for name in names:
            if name == "uv":
                continue
            _write_executable(mock_bin / f"{name}.cmd", "@exit /b 0\n")
    else:
        _write_executable(
            mock_bin / "uv", '#!/bin/sh\nexec "$CI_MOCK_PYTHON" "$CI_MOCK_TOOL" "$@"\n'
        )
        for name in names:
            if name == "uv":
                continue
            _write_executable(mock_bin / name, "#!/bin/sh\nexit 0\n")
    return mock_bin, mock_tool


def _path_hiding_node(mock_bin: Path, inherited: str) -> str:
    kept: list[str] = []
    for entry in inherited.split(os.pathsep):
        if not entry:
            continue
        directory = Path(entry)
        names = ("node", "node.exe", "npm", "npm.cmd")
        if any((directory / name).exists() for name in names):
            continue
        kept.append(entry)
    return os.pathsep.join([str(mock_bin), *kept])


@dataclass(frozen=True)
class _CiOptions:
    timing: str | None = None
    fail_at: str | None = None
    fail_code: int = 1
    hide_node: bool = False


@dataclass(frozen=True)
class _CiRun:
    result: subprocess.CompletedProcess[str]
    evidence: Path
    pytest_env: Path
    record_env: Path
    record_args: Path


def _command_env(
    base: dict[str, str],
    mock_bin: Path,
    mock_tool: Path,
    outputs: tuple[Path, Path, Path],
    options: _CiOptions,
) -> dict[str, str]:
    pytest_env, record_env, record_args = outputs
    env = base.copy()
    env.update({name: "a" * 40 for name in _COMPLETION_CONTEXT[:-1]})
    env.pop("ORCHESTUNE_LOCAL_CI_TIMING", None)
    if options.timing is not None:
        env["ORCHESTUNE_LOCAL_CI_TIMING"] = options.timing
    if options.fail_at is not None:
        env["CI_FAIL_AT"] = options.fail_at
        env["CI_FAIL_CODE"] = str(options.fail_code)
    path = env["PATH"]
    if options.hide_node:
        path = _path_hiding_node(mock_bin, path)
    else:
        path = f"{mock_bin}{os.pathsep}{path}"
    env.update(
        {
            "PYTHONWARNDEFAULTENCODING": "0",
            "PATH": path,
            "GITLEAKS_INSTALL_DIR": str(mock_bin),
            "CI_PYTEST_ENV": str(pytest_env),
            "CI_RECORD_ENV": str(record_env),
            "CI_RECORD_ARGS": str(record_args),
            "CI_MOCK_PYTHON": sys.executable,
            "CI_MOCK_TOOL": str(mock_tool),
        }
    )
    return env


def _run_ci(
    tmp_path: Path,
    script_name: str,
    command: list[str],
    *,
    timing: str | None = None,
    fail_at: str | None = None,
    fail_code: int = 1,
    hide_node: bool = False,
    plant_evidence: bool = False,
    quint_body: str | None = None,
) -> _CiRun:
    repo = _prepare_repo(tmp_path, script_name)
    if quint_body is not None:
        suffix = Path(script_name).suffix
        quint = repo / "scripts" / f"quint-check{suffix}"
        quint.write_text(quint_body, encoding="utf-8")
        quint.chmod(0o755)
    mock_bin, mock_tool = _prepare_mock_tools(tmp_path, include_node=not hide_node)
    evidence = repo / ("a" * 40)
    if plant_evidence:
        evidence.write_text("old\n", encoding="utf-8")
    pytest_env = tmp_path / "pytest-env.txt"
    record_env = tmp_path / "record-env.txt"
    record_args = tmp_path / "record-args.txt"
    env = _command_env(
        os.environ.copy(),
        mock_bin,
        mock_tool,
        (pytest_env, record_env, record_args),
        _CiOptions(timing, fail_at, fail_code, hide_node),
    )
    result = subprocess.run(
        [*command, str(repo / "scripts" / script_name)],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return _CiRun(result, evidence, pytest_env, record_env, record_args)


def _assert_isolation(run: _CiRun) -> None:
    assert json.loads(run.pytest_env.read_text(encoding="utf-8")) == {
        "PYTHONWARNDEFAULTENCODING": "1"
    }
    assert json.loads(run.record_env.read_text(encoding="utf-8")) == {
        **{name: "a" * 40 for name in _COMPLETION_CONTEXT[:-1]},
        "PYTHONWARNDEFAULTENCODING": "0",
    }
    args = json.loads(run.record_args.read_text(encoding="utf-8"))
    assert {"--expected-head", "--expected-tree"} <= set(args)
    # Base drift no longer invalidates evidence (#1047).
    assert "--expected-base" not in args


def _timing_rows(stderr: str) -> list[tuple[str, int, int]]:
    rows: list[tuple[str, int, int]] = []
    for raw in stderr.splitlines():
        match = _TIMING_LINE.fullmatch(raw.strip())
        if match is None:
            continue
        rows.append((match.group(1), int(match.group(2)), int(match.group(3))))
    return rows


def _assert_ci_isolation(tmp_path: Path, script_name: str, command: list[str]) -> None:
    run = _run_ci(tmp_path, script_name, command)
    output = run.result.stdout + run.result.stderr
    assert run.result.returncode == 0, output
    assert "[local-ci-timing]" not in output
    _assert_isolation(run)


def _assert_timed_success(run: _CiRun) -> None:
    output = run.result.stdout + run.result.stderr
    assert run.result.returncode == 0, output
    assert "[local-ci-timing]" not in run.result.stdout
    rows = _timing_rows(run.result.stderr)
    assert [phase for phase, _, _ in rows] == list(_PHASES)
    assert all(elapsed >= 0 and code == 0 for _, elapsed, code in rows)
    _assert_isolation(run)


def _assert_timed_failure(
    run: _CiRun,
    failed: str,
    code: int,
    *,
    record_started: bool,
) -> None:
    output = run.result.stdout + run.result.stderr
    assert run.result.returncode == code, output
    rows = _timing_rows(run.result.stderr)
    expect = list(_PHASES[: _PHASES.index(failed) + 1])
    assert [phase for phase, _, _ in rows] == expect
    assert all(elapsed >= 0 and exit_code == 0 for _, elapsed, exit_code in rows[:-1])
    assert rows[-1][1] >= 0
    assert rows[-1][2] == code
    assert not run.evidence.exists()
    assert run.record_args.exists() is record_started


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _powershell_command() -> list[str]:
    powershell = _powershell()
    if powershell is None:
        pytest.skip("PowerShell is unavailable")
    return [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell CI path")
def test_posix_ci_keeps_completion_context_out_of_pytest(tmp_path: Path) -> None:
    _assert_ci_isolation(tmp_path, "local-ci.sh", ["bash"])


def test_powershell_ci_keeps_completion_context_out_of_pytest(tmp_path: Path) -> None:
    _assert_ci_isolation(tmp_path, "local-ci.ps1", _powershell_command())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell CI path")
def test_posix_timing_reports_each_phase_once(tmp_path: Path) -> None:
    run = _run_ci(tmp_path, "local-ci.sh", ["bash"], timing="1")
    _assert_timed_success(run)


def test_powershell_timing_reports_each_phase_once(tmp_path: Path) -> None:
    run = _run_ci(tmp_path, "local-ci.ps1", _powershell_command(), timing="1")
    _assert_timed_success(run)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell CI path")
def test_posix_timing_stops_at_missing_node(tmp_path: Path) -> None:
    run = _run_ci(
        tmp_path,
        "local-ci.sh",
        ["bash"],
        timing="1",
        hide_node=True,
        plant_evidence=True,
    )
    _assert_timed_failure(run, "preflight", 2, record_started=False)


def test_powershell_timing_stops_at_missing_node(tmp_path: Path) -> None:
    run = _run_ci(
        tmp_path,
        "local-ci.ps1",
        _powershell_command(),
        timing="1",
        hide_node=True,
        plant_evidence=True,
    )
    _assert_timed_failure(run, "preflight", 2, record_started=False)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell CI path")
@pytest.mark.parametrize(
    ("fail_at", "phase", "code", "record_started"),
    [
        ("mypy", "mypy", 3, False),
        ("pytest", "pytest", 4, False),
        ("record", "evidence_record", 5, True),
    ],
)
def test_posix_timing_keeps_injected_failure(
    tmp_path: Path, fail_at: str, phase: str, code: int, record_started: bool
) -> None:
    run = _run_ci(
        tmp_path,
        "local-ci.sh",
        ["bash"],
        timing="1",
        fail_at=fail_at,
        fail_code=code,
        plant_evidence=True,
    )
    _assert_timed_failure(run, phase, code, record_started=record_started)


@pytest.mark.parametrize(
    ("fail_at", "phase", "code", "record_started"),
    [
        ("mypy", "mypy", 3, False),
        ("pytest", "pytest", 4, False),
        ("record", "evidence_record", 5, True),
    ],
)
def test_powershell_timing_keeps_injected_failure(
    tmp_path: Path, fail_at: str, phase: str, code: int, record_started: bool
) -> None:
    run = _run_ci(
        tmp_path,
        "local-ci.ps1",
        _powershell_command(),
        timing="1",
        fail_at=fail_at,
        fail_code=code,
        plant_evidence=True,
    )
    _assert_timed_failure(run, phase, code, record_started=record_started)


def test_powershell_timing_records_terminating_exception(tmp_path: Path) -> None:
    run = _run_ci(
        tmp_path,
        "local-ci.ps1",
        _powershell_command(),
        timing="1",
        quint_body="throw 'boom'\n",
    )
    _assert_timed_failure(run, "quint_toolchain", 1, record_started=False)
