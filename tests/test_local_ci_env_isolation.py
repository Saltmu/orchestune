"""Regression checks for completion context leaking into CI's test runner."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_COMPLETION_CONTEXT = (
    "ORCHESTUNE_EXPECTED_HEAD",
    "ORCHESTUNE_EXPECTED_TREE",
    "ORCHESTUNE_EXPECTED_BASE",
    "ORCHESTUNE_BASE_SHA",
    "ORCHESTUNE_BASE_REF",
    "ORCHESTUNE_STATE_PATH",
    "ORCHESTUNE_ISSUE_NUMBER",
    "ORCHESTUNE_CI_EVIDENCE_PATH",
)


def _prepare_repo(tmp_path: Path, script_name: str) -> Path:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(Path("scripts") / script_name, scripts / script_name)
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


def _prepare_mock_tools(tmp_path: Path) -> tuple[Path, Path]:
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    mock_tool = mock_bin / "mock_uv.py"
    mock_tool.write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        f"names = {_COMPLETION_CONTEXT!r}\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['run', 'pytest']:\n"
        "    key = 'CI_PYTEST_ENV'\n"
        "elif args[:5] == ['run', 'python', '-m', 'orchestune.complete.ci_evidence', 'record']:\n"
        "    key = 'CI_RECORD_ENV'\n"
        "    Path(os.environ['CI_RECORD_ARGS']).write_text(json.dumps(args))\n"
        "else:\n    sys.exit(0)\n"
        "Path(os.environ[key]).write_text(json.dumps({name: os.environ[name] for name in names if name in os.environ}))\n",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        (mock_bin / "uv.cmd").write_text(
            '@echo off\n"%CI_MOCK_PYTHON%" "%CI_MOCK_TOOL%" %*\nexit /b %ERRORLEVEL%\n',
            encoding="utf-8",
        )
        (mock_bin / "gitleaks.cmd").write_text("@exit /b 0\n", encoding="utf-8")
    else:
        for name, body in (
            ("uv", 'exec "$CI_MOCK_PYTHON" "$CI_MOCK_TOOL" "$@"\n'),
            ("gitleaks", "exit 0\n"),
        ):
            tool = mock_bin / name
            tool.write_text("#!/bin/sh\n" + body, encoding="utf-8")
            tool.chmod(0o755)
    return mock_bin, mock_tool


def _assert_ci_isolation(tmp_path: Path, script_name: str, command: list[str]) -> None:
    repo = _prepare_repo(tmp_path, script_name)
    mock_bin, mock_tool = _prepare_mock_tools(tmp_path)
    pytest_env = tmp_path / "pytest-env.txt"
    record_env = tmp_path / "record-env.txt"
    record_args = tmp_path / "record-args.txt"
    env = os.environ.copy()
    env.update({name: "a" * 40 for name in _COMPLETION_CONTEXT})
    env.update(
        {
            "PATH": f"{mock_bin}{os.pathsep}{env['PATH']}",
            "GITLEAKS_INSTALL_DIR": str(mock_bin),
            "CI_PYTEST_ENV": str(pytest_env),
            "CI_RECORD_ENV": str(record_env),
            "CI_RECORD_ARGS": str(record_args),
            "CI_MOCK_PYTHON": sys.executable,
            "CI_MOCK_TOOL": str(mock_tool),
        }
    )
    run = subprocess.run(
        [*command, str(repo / "scripts" / script_name)],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(pytest_env.read_text(encoding="utf-8")) == {}
    assert json.loads(record_env.read_text(encoding="utf-8")) == {
        name: "a" * 40 for name in _COMPLETION_CONTEXT
    }
    args = json.loads(record_args.read_text(encoding="utf-8"))
    assert {"--expected-head", "--expected-tree", "--expected-base"} <= set(args)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell CI path")
def test_posix_ci_keeps_completion_context_out_of_pytest(tmp_path: Path) -> None:
    _assert_ci_isolation(tmp_path, "local-ci.sh", ["bash"])


def test_powershell_ci_keeps_completion_context_out_of_pytest(tmp_path: Path) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")
    _assert_ci_isolation(
        tmp_path,
        "local-ci.ps1",
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"],
    )
