"""#820: real-process verification of Windows Job Object ownership.

These tests start real processes and are skipped on non-Windows hosts; the
Windows leg of CI is what exercises them.
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

import pytest

from orchestune.infra import managed_process_windows
from orchestune.infra.managed_process import (
    ManagedProcessSpec,
    ProcessOutcome,
    run_managed,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")

PY = sys.executable


def _write_scripts(tmp_path: Path, *, parent_body: str) -> tuple[Path, Path]:
    """Write a recording grandchild script and a parent script that spawns it."""
    pids = tmp_path / "pids"
    child = tmp_path / "child.py"
    child.write_text(
        "import os, time\n"
        f"open({str(pids)!r}, 'a').write(str(os.getpid()) + ' ')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import os, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(child)!r}])\n" + parent_body,
        encoding="utf-8",
    )
    return parent, pids


def _run_script(script: Path, timeout: float):  # type: ignore[no-untyped-def]
    return run_managed(
        ManagedProcessSpec(
            args=[PY, str(script)],
            stage="ci",
            timeout_seconds=timeout,
            term_grace_seconds=0.3,
        )
    )


_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102


def _alive(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT)
    finally:
        kernel32.CloseHandle(handle)


def _wait_gone(pid: int, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return not _alive(pid)


def _spec(code: str, timeout: float) -> ManagedProcessSpec:
    return ManagedProcessSpec(
        args=[PY, "-c", code], stage="ci", timeout_seconds=timeout
    )


def _read_pids(path: Path, count: int) -> list[int]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.exists():
            pids = [int(p) for p in path.read_text().split() if p]
            if len(pids) >= count:
                return pids
        time.sleep(0.1)
    raise AssertionError(f"expected {count} pids in {path}")


def test_timeout_terminates_child_and_grandchild_and_confirms_empty_job(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "pids"
    parent, pids = _write_scripts(
        tmp_path,
        parent_body=(
            f"open({str(pid_file)!r}, 'a').write(str(os.getpid()) + ' ')\n"
            "time.sleep(60)\n"
        ),
    )

    result = _run_script(parent, timeout=2.0)

    assert result.outcome is ProcessOutcome.TIMED_OUT
    assert result.stop_confirmed is True
    for pid in _read_pids(pids, 2):
        assert _wait_gone(pid), f"pid {pid} survived Job termination"


def test_descendant_surviving_the_main_process_is_terminated(tmp_path: Path) -> None:
    pid_file = tmp_path / "pids"
    parent, pids = _write_scripts(
        tmp_path,
        parent_body=(
            f"while not os.path.exists({str(pid_file)!r}): time.sleep(0.05)\n"
            "print('parent done')\n"
        ),
    )

    result = _run_script(parent, timeout=30.0)

    assert result.outcome is ProcessOutcome.SUCCESS
    assert result.stop_confirmed is True
    assert "descendant" in result.detail
    assert _wait_gone(_read_pids(pids, 1)[0])


def test_job_assignment_failure_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "ran"
    real = managed_process_windows._kernel32()

    class _FailingKernel32:
        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            if name == "AssignProcessToJobObject":
                return lambda *_args: 0
            return getattr(real, name)

    monkeypatch.setattr(
        managed_process_windows, "_kernel32", lambda: _FailingKernel32()
    )

    result = run_managed(_spec(f"open({str(marker)!r}, 'w').close()", timeout=10.0))

    assert result.outcome is ProcessOutcome.START_FAILED
    assert "AssignProcessToJobObject" in result.detail
    time.sleep(0.5)
    assert not marker.exists()
