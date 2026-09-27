from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_SH = REPOSITORY_ROOT / "scripts" / "create-session-dir.sh"
SCRIPT_PS1 = REPOSITORY_ROOT / "scripts" / "create-session-dir.ps1"

SESSION_DIR_PATTERN = re.compile(
    r"^\.orchestune/tmp/(?P<prefix>[a-zA-Z0-9_-]+)-(?P<task>[a-zA-Z0-9_.-]+)-(?P<timestamp>[0-9]{8}T[0-9]{6}Z)-(?P<random>[a-f0-9]{8})$"
)


def _find_bash() -> str | None:
    if sys.platform != "win32":
        return shutil.which("bash")
    git = shutil.which("git")
    if git:
        git_bash = Path(git).resolve().parent.parent / "bin" / "bash.exe"
        if git_bash.is_file():
            return str(git_bash)
        git_usr_bash = Path(git).resolve().parent.parent / "usr" / "bin" / "bash.exe"
        if git_usr_bash.is_file():
            return str(git_usr_bash)
    for standard_path in [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Git"
        / "bin"
        / "bash.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Git"
        / "bin"
        / "bash.exe",
    ]:
        if standard_path.is_file():
            return str(standard_path)
    bash = shutil.which("bash")
    if bash and "WindowsApps" not in Path(bash).parts:
        return bash
    return None


def _list2cmdline(seq: Sequence[str]) -> str:
    """Format argument sequence into a Windows command line, quoting newlines."""
    result: list[str] = []
    for arg in seq:
        bs_buf: list[str] = []
        if result:
            result.append(" ")
        needquote = (
            (" " in arg) or ("\t" in arg) or ("\n" in arg) or ("\r" in arg) or not arg
        )
        if needquote:
            result.append('"')
        for c in arg:
            if c == "\\":
                bs_buf.append(c)
            elif c == '"':
                result.append("\\" * len(bs_buf) * 2)
                bs_buf = []
                result.append('\\"')
            else:
                if bs_buf:
                    result.extend(bs_buf)
                    bs_buf = []
                result.append(c)
        if bs_buf:
            result.extend(bs_buf)
        if needquote:
            result.extend("\\" * len(bs_buf))
            result.append('"')
    return "".join(result)


def _run_sh(
    *args: str, cwd: Path, check: bool = True
) -> subprocess.CompletedProcess[str]:
    if sys.platform == "win32":
        bash = _find_bash()
        if bash is None:
            pytest.skip("Git Bash is not available on Windows")
        cmd: str | list[str] = _list2cmdline([bash, str(SCRIPT_SH), *args])
    else:
        cmd = [str(SCRIPT_SH), *args]
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def _run_sh_unchecked(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return _run_sh(*args, cwd=cwd, check=False)


def _run_ps1_unchecked(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    ps = _find_powershell()
    if ps is None:
        pytest.skip("powershell/pwsh is not installed")
    return subprocess.run(
        [ps, "-NoProfile", "-File", str(SCRIPT_PS1), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _find_powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def test_script_sh_exists_and_executable():
    assert SCRIPT_SH.is_file(), f"{SCRIPT_SH} does not exist"
    if sys.platform != "win32":
        assert os.access(SCRIPT_SH, os.X_OK), f"{SCRIPT_SH} is not executable"


def test_script_ps1_exists():
    assert SCRIPT_PS1.is_file(), f"{SCRIPT_PS1} does not exist"


def test_create_session_dir_default(tmp_path: Path):
    result = _run_sh(cwd=tmp_path)
    output = result.stdout.strip()
    match = SESSION_DIR_PATTERN.match(output)
    assert match is not None, f"Output '{output}' does not match pattern"
    assert match.group("prefix") == "task"
    assert match.group("task") == "scratch"

    created_dir = tmp_path / output
    assert created_dir.is_dir(), f"Expected directory '{created_dir}' to exist"


def test_create_session_dir_custom_prefix_and_task(tmp_path: Path):
    result = _run_sh("planning", "1084", cwd=tmp_path)
    output = result.stdout.strip()
    match = SESSION_DIR_PATTERN.match(output)
    assert match is not None, f"Output '{output}' does not match pattern"
    assert match.group("prefix") == "planning"
    assert match.group("task") == "1084"

    created_dir = tmp_path / output
    assert created_dir.is_dir(), f"Expected directory '{created_dir}' to exist"


def test_create_session_dir_custom_prefix_only(tmp_path: Path):
    result = _run_sh("decomposition", cwd=tmp_path)
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
        result = _run_sh("task", "rapid", cwd=tmp_path)
        output = result.stdout.strip()
        dirs.add(output)
    # Even if executed within the same second, random suffix ensures distinct dirs
    assert len(dirs) == 5, f"Expected 5 unique directories, got {len(dirs)}: {dirs}"


def test_create_session_dir_ps1_content():
    content = SCRIPT_PS1.read_text(encoding="utf-8")
    assert "Get-Date" in content
    assert "AsUTC" in content or "ToUniversalTime" in content
    assert ".orchestune/tmp" in content.replace("\\", "/")
    assert "Prefix -notmatch" in content
    assert "Task -notmatch" in content
    assert "^[a-zA-Z0-9_-]+$" in content
    assert "^[a-zA-Z0-9_.-]+$" in content


@pytest.mark.skipif(
    _find_powershell() is None, reason="powershell/pwsh is not installed"
)
def test_create_session_dir_ps1_execution(tmp_path: Path):
    ps = _find_powershell()
    assert ps is not None
    result = subprocess.run(
        [ps, "-NoProfile", "-File", str(SCRIPT_PS1), "planning", "1084"],
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


INVALID_PREFIXES = [
    "../escaped",
    "/absolute",
    "a/b",
    r"a\b",
    "pre fix",
    "pre\nfix",
    "",
    "prefix!",
    "prefix.dot",
]

INVALID_TASKS = [
    "../escaped",
    "/absolute",
    "a/b",
    r"a\b",
    "task slug",
    "task\nslug",
    "",
    "task!",
]


@pytest.mark.parametrize("invalid_prefix", INVALID_PREFIXES)
def test_create_session_dir_sh_rejects_invalid_prefix(
    tmp_path: Path, invalid_prefix: str
):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    result = _run_sh_unchecked(invalid_prefix, "valid-task", cwd=sandbox)
    assert (
        result.returncode != 0
    ), f"Expected non-zero exit for prefix '{invalid_prefix}'"
    assert "prefix" in result.stderr.lower()
    assert list(sandbox.iterdir()) == []
    assert list(tmp_path.iterdir()) == [sandbox]


@pytest.mark.parametrize("invalid_task", INVALID_TASKS)
def test_create_session_dir_sh_rejects_invalid_task(tmp_path: Path, invalid_task: str):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    result = _run_sh_unchecked("planning", invalid_task, cwd=sandbox)
    assert result.returncode != 0, f"Expected non-zero exit for task '{invalid_task}'"
    assert "task" in result.stderr.lower()
    assert list(sandbox.iterdir()) == []
    assert list(tmp_path.iterdir()) == [sandbox]


def test_create_session_dir_sh_allows_dot_in_task(tmp_path: Path):
    result = _run_sh("task", "subtask.1", cwd=tmp_path)
    output = result.stdout.strip()
    match = SESSION_DIR_PATTERN.match(output)
    assert match is not None, f"Output '{output}' does not match pattern"
    assert match.group("prefix") == "task"
    assert match.group("task") == "subtask.1"

    created_dir = tmp_path / output
    assert created_dir.is_dir(), f"Expected directory '{created_dir}' to exist"


@pytest.mark.skipif(
    _find_powershell() is None, reason="powershell/pwsh is not installed"
)
@pytest.mark.parametrize("invalid_prefix", INVALID_PREFIXES)
def test_create_session_dir_ps1_rejects_invalid_prefix(
    tmp_path: Path, invalid_prefix: str
):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    result = _run_ps1_unchecked(invalid_prefix, "valid-task", cwd=sandbox)
    assert (
        result.returncode != 0
    ), f"Expected non-zero exit for prefix '{invalid_prefix}'"
    assert "prefix" in result.stderr.lower()
    assert list(sandbox.iterdir()) == []
    assert list(tmp_path.iterdir()) == [sandbox]


@pytest.mark.skipif(
    _find_powershell() is None, reason="powershell/pwsh is not installed"
)
@pytest.mark.parametrize("invalid_task", INVALID_TASKS)
def test_create_session_dir_ps1_rejects_invalid_task(tmp_path: Path, invalid_task: str):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    result = _run_ps1_unchecked("planning", invalid_task, cwd=sandbox)
    assert result.returncode != 0, f"Expected non-zero exit for task '{invalid_task}'"
    assert "task" in result.stderr.lower()
    assert list(sandbox.iterdir()) == []
    assert list(tmp_path.iterdir()) == [sandbox]


@pytest.mark.skipif(
    _find_powershell() is None, reason="powershell/pwsh is not installed"
)
def test_create_session_dir_ps1_allows_dot_in_task(tmp_path: Path):
    ps = _find_powershell()
    assert ps is not None
    result = subprocess.run(
        [ps, "-NoProfile", "-File", str(SCRIPT_PS1), "task", "subtask.1"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    output = result.stdout.strip().replace("\\", "/")
    match = SESSION_DIR_PATTERN.match(output)
    assert match is not None, f"Output '{output}' does not match pattern"
    assert match.group("prefix") == "task"
    assert match.group("task") == "subtask.1"

    created_dir = tmp_path / output
    assert created_dir.is_dir(), f"Expected directory '{created_dir}' to exist"


def test_list2cmdline_quotes_whitespace_and_newlines():
    assert _list2cmdline([]) == ""
    assert _list2cmdline(["a", "b"]) == "a b"
    assert _list2cmdline(["a b"]) == '"a b"'
    assert _list2cmdline(["a\tb"]) == '"a\tb"'
    assert _list2cmdline(["a\nb"]) == '"a\nb"'
    assert _list2cmdline(["a\rb"]) == '"a\rb"'
    assert _list2cmdline([""]) == '""'
    assert _list2cmdline(['a"b']) == r"a\"b"
    assert _list2cmdline(['a " b']) == r'"a \" b"'
    assert _list2cmdline([r"a\b"]) == r"a\b"
    assert _list2cmdline([r"a\ b"]) == r'"a\ b"'


def test_find_bash():
    bash = _find_bash()
    if sys.platform != "win32":
        assert bash is not None
        assert "bash" in bash
    else:
        if bash is not None:
            assert "WindowsApps" not in Path(bash).parts
            assert Path(bash).is_file()
