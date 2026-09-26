"""Regression checks for completion context leaking into CI's test runner."""

from __future__ import annotations

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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell CI path")
def test_posix_ci_keeps_completion_context_out_of_pytest(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(Path("scripts/local-ci.sh"), scripts / "local-ci.sh")
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

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    mock_uv = mock_bin / "uv"
    mock_uv.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = run ] && [ "$2" = pytest ]; then\n'
        '  env | grep "^ORCHESTUNE_" > "$CI_PYTEST_ENV" || true\n'
        "fi\n"
        'if [ "$1" = run ] && [ "$2" = python ] && [ "$3" = -m ] && [ "$5" = record ]; then\n'
        '  env | grep "^ORCHESTUNE_" > "$CI_RECORD_ENV" || true\n'
        '  printf "%s\\n" "$@" > "$CI_RECORD_ARGS"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    mock_uv.chmod(0o755)
    mock_gitleaks = mock_bin / "gitleaks"
    mock_gitleaks.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    mock_gitleaks.chmod(0o755)

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
        }
    )
    run = subprocess.run(
        ["bash", str(scripts / "local-ci.sh")],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert not any(
        line.startswith(f"{name}=")
        for line in pytest_env.read_text(encoding="utf-8").splitlines()
        for name in _COMPLETION_CONTEXT
    )
    assert "ORCHESTUNE_BASE_SHA=" in record_env.read_text(encoding="utf-8")
    assert "--expected-head" in record_args.read_text(encoding="utf-8")
    assert "--expected-tree" in record_args.read_text(encoding="utf-8")
    assert "--expected-base" in record_args.read_text(encoding="utf-8")
