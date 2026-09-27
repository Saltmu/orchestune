from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_SH = REPOSITORY_ROOT / "scripts" / "create-session-dir.sh"
SCRIPT_PS1 = REPOSITORY_ROOT / "scripts" / "create-session-dir.ps1"

SESSION_DIR_PATTERN = re.compile(
    r"^\.orchestune/tmp/(?P<prefix>[a-zA-Z0-9_-]+)-(?P<task>[a-zA-Z0-9_.-]+)-(?P<timestamp>[0-9]{8}T[0-9]{6}Z)-(?P<random>[a-f0-9]{8})$"
)


def test_script_sh_exists_and_executable():
    assert SCRIPT_SH.is_file(), f"{SCRIPT_SH} does not exist"
    assert os.access(SCRIPT_SH, os.X_OK), f"{SCRIPT_SH} is not executable"


def test_script_ps1_exists():
    assert SCRIPT_PS1.is_file(), f"{SCRIPT_PS1} does not exist"


def test_create_session_dir_default(tmp_path: Path):
    result = subprocess.run(
        [str(SCRIPT_SH)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    output = result.stdout.strip()
    match = SESSION_DIR_PATTERN.match(output)
    assert match is not None, f"Output '{output}' does not match pattern"
    assert match.group("prefix") == "task"
    assert match.group("task") == "scratch"

    created_dir = tmp_path / output
    assert created_dir.is_dir(), f"Expected directory '{created_dir}' to exist"


def test_create_session_dir_custom_prefix_and_task(tmp_path: Path):
    result = subprocess.run(
        [str(SCRIPT_SH), "planning", "1084"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    output = result.stdout.strip()
    match = SESSION_DIR_PATTERN.match(output)
    assert match is not None, f"Output '{output}' does not match pattern"
    assert match.group("prefix") == "planning"
    assert match.group("task") == "1084"

    created_dir = tmp_path / output
    assert created_dir.is_dir(), f"Expected directory '{created_dir}' to exist"


def test_create_session_dir_custom_prefix_only(tmp_path: Path):
    result = subprocess.run(
        [str(SCRIPT_SH), "decomposition"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    output = result.stdout.strip()
    match = SESSION_DIR_PATTERN.match(output)
    assert match is not None, f"Output '{output}' does not match pattern"
    assert match.group("prefix") == "decomposition"
    assert match.group("task") == "scratch"

    created_dir = tmp_path / output
    assert created_dir.is_dir(), f"Expected directory '{created_dir}' to exist"


def test_create_session_dir_uniqueness_across_consecutive_calls(tmp_path: Path):
    dirs = set()
    for _ in range(5):
        result = subprocess.run(
            [str(SCRIPT_SH), "task", "rapid"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        output = result.stdout.strip()
        dirs.add(output)
    # Even if executed within the same second, random suffix ensures distinct dirs
    assert len(dirs) == 5, f"Expected 5 unique directories, got {len(dirs)}: {dirs}"


def test_create_session_dir_ps1_content():
    content = SCRIPT_PS1.read_text(encoding="utf-8")
    assert "Get-Date" in content
    assert "AsUTC" in content or "ToUniversalTime" in content
    assert ".orchestune/tmp" in content.replace("\\", "/")


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh is not installed")
def test_create_session_dir_ps1_execution(tmp_path: Path):
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(SCRIPT_PS1), "planning", "1084"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    output = result.stdout.strip().replace("\\", "/")
    match = SESSION_DIR_PATTERN.match(output)
    assert match is not None, f"Output '{output}' does not match pattern"
    assert match.group("prefix") == "planning"
    assert match.group("task") == "1084"

    created_dir = tmp_path / output
    assert created_dir.is_dir(), f"Expected directory '{created_dir}' to exist"
