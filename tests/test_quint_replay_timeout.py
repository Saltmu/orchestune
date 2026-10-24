"""Tests for bounded execution, timeout handling and process-tree termination (#1399)."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.quint_replay import (
    Exploration,
    ToolError,
    _run_bounded,
    quint_executable,
    run_model,
)


def _is_pid_alive(pid: int) -> bool:
    """Check if process with given PID is still running across platforms."""
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            0x1000, False, pid
        )  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            exit_code = ctypes.wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == 259  # STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    else:
        import os

        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def test_run_bounded_terminates_process_tree_on_timeout(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    # Child spawns a grandchild that sleeps 60s and inherits stdout/stderr pipes,
    # then child itself sleeps 60s.
    child_code = (
        "import subprocess, sys, time, pathlib\n"
        "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"pathlib.Path(r'{pid_file}').write_text(str(grandchild.pid), encoding='utf-8')\n"
        "time.sleep(60)\n"
    )

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _run_bounded([sys.executable, "-c", child_code], timeout=1.5)
    elapsed = time.monotonic() - started

    # Must timeout promptly without waiting for the 60s grandchild sleep
    assert elapsed < 15.0

    # Verify grandchild PID was recorded and is no longer running
    assert pid_file.is_file(), "Child should have recorded grandchild PID"
    grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())

    # Wait up to 3 seconds for OS cleanup if necessary
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and _is_pid_alive(grandchild_pid):
        time.sleep(0.1)

    assert not _is_pid_alive(
        grandchild_pid
    ), f"Grandchild {grandchild_pid} should have been terminated"


def test_run_bounded_normal_completion() -> None:
    cmd = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('hello\\n'); sys.stderr.write('world\\n'); sys.exit(0)",
    ]
    proc = _run_bounded(cmd, timeout=10.0)
    assert proc.returncode == 0
    assert proc.stdout == "hello\n"
    assert proc.stderr == "world\n"


def test_run_model_timeout_raises_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_bounded(
        command: list[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr("tests.quint_replay._run_bounded", fake_run_bounded)
    monkeypatch.setattr("tests.quint_replay.quint_executable", lambda: Path("quint"))

    bounds = Exploration(seed="0x1", max_samples=1, max_steps=10, timeout=1.0)
    with pytest.raises(ToolError, match="quint timed out after 1.0s"):
        run_model(bounds)


def test_quint_executable_timeout_raises_tool_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_exe = tmp_path / ("quint.cmd" if sys.platform == "win32" else "quint")
    fake_exe.write_text("", encoding="utf-8")

    def fake_run_bounded(
        command: list[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, timeout)

    quint_executable.cache_clear()
    monkeypatch.setattr("tests.quint_replay._run_bounded", fake_run_bounded)
    monkeypatch.setattr("tests.quint_replay.ROOT", tmp_path)
    node_bin = tmp_path / "node_modules" / ".bin"
    node_bin.mkdir(parents=True, exist_ok=True)
    real_link = node_bin / fake_exe.name
    real_link.write_text("", encoding="utf-8")

    try:
        with pytest.raises(ToolError, match="timed out"):
            quint_executable()
    finally:
        quint_executable.cache_clear()
