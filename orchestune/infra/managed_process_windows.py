"""Windows Job Object ownership for ``managed_process`` (#820).

The command is created *suspended*, assigned to a Job Object configured with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` and only then resumed, so no descendant can be
spawned before the Job owns the process. Stopping terminates the Job and confirms the
Job reports zero active processes.

If the process cannot be assigned to a Job the command is not run (``START_FAILED``);
this never degrades to killing a single PID or waiting on an unbounded ``taskkill``.
See https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects.

All Win32 entry points get explicit prototypes: ctypes would otherwise pass handles as
32-bit ``int`` and truncate them on 64-bit Windows.
"""

from __future__ import annotations

import ctypes
import functools
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_CREATE_SUSPENDED = 0x00000004
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


def _last_error() -> int:
    if sys.platform != "win32":
        return 0
    return ctypes.get_last_error()


class JobAssignmentError(OSError):
    """The started process could not be placed under a Job Object."""


@functools.cache
def _structures() -> tuple[Any, Any]:
    """Build the ctypes structures lazily so the module imports on any OS."""
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimit),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class BasicAccounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    return ExtendedLimit, BasicAccounting


@functools.cache
def _kernel32() -> Any:
    """``kernel32`` with explicit prototypes for every call this module makes."""
    if sys.platform != "win32":
        raise OSError("Windows Job Objects are unavailable on this platform")
    lib = ctypes.WinDLL("kernel32", use_last_error=True)
    handle, dword, void_p = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p
    lib.CreateJobObjectW.argtypes = [void_p, ctypes.c_wchar_p]
    lib.CreateJobObjectW.restype = handle
    lib.SetInformationJobObject.argtypes = [handle, ctypes.c_int, void_p, dword]
    lib.SetInformationJobObject.restype = ctypes.c_int
    lib.QueryInformationJobObject.argtypes = [
        handle,
        ctypes.c_int,
        void_p,
        dword,
        void_p,
    ]
    lib.QueryInformationJobObject.restype = ctypes.c_int
    lib.AssignProcessToJobObject.argtypes = [handle, handle]
    lib.AssignProcessToJobObject.restype = ctypes.c_int
    lib.TerminateJobObject.argtypes = [handle, ctypes.c_uint]
    lib.TerminateJobObject.restype = ctypes.c_int
    lib.CloseHandle.argtypes = [handle]
    lib.CloseHandle.restype = ctypes.c_int
    return lib


@functools.cache
def _ntdll() -> Any:
    if sys.platform != "win32":
        raise OSError("Windows Job Objects are unavailable on this platform")
    lib = ctypes.WinDLL("ntdll")
    lib.NtResumeProcess.argtypes = [ctypes.c_void_p]
    lib.NtResumeProcess.restype = ctypes.c_long
    return lib


class WindowsJobGroup:
    """A Job Object owning one managed command and all its descendants."""

    def __init__(self, job: int) -> None:
        self._job = job
        self._closed = False

    def terminate(self) -> None:
        self._terminate_job()

    def kill(self) -> None:
        self._terminate_job()

    def _terminate_job(self) -> None:
        if sys.platform != "win32" or self._closed:
            return
        _kernel32().TerminateJobObject(self._job, 1)

    def alive(self) -> bool:
        if sys.platform != "win32" or self._closed:
            return False
        _, accounting_type = _structures()
        accounting = accounting_type()
        ok = _kernel32().QueryInformationJobObject(
            self._job,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        )
        # An unreadable Job is never reported as stopped.
        return True if not ok else bool(accounting.ActiveProcesses)

    def close(self) -> None:
        if sys.platform != "win32" or self._closed:
            return
        self._closed = True
        _kernel32().CloseHandle(self._job)


def _create_kill_on_close_job() -> int:
    kernel32 = _kernel32()
    extended_type, _ = _structures()
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise JobAssignmentError(f"CreateJobObjectW failed: {_last_error()}")
    info = extended_type()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        error = _last_error()
        kernel32.CloseHandle(job)
        raise JobAssignmentError(f"SetInformationJobObject failed: {error}")
    return int(job)


def start_owned_process(
    args: list[str],
    cwd: Path | str | None,
    env: Mapping[str, str] | None,
) -> tuple[subprocess.Popen[bytes], WindowsJobGroup]:
    if sys.platform != "win32":
        raise OSError("Windows Job Objects are unavailable on this platform")
    job = _create_kill_on_close_job()
    kernel32 = _kernel32()
    try:
        popen = subprocess.Popen(
            args,
            cwd=None if cwd is None else str(cwd),
            env=None if env is None else dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=_CREATE_SUSPENDED | _CREATE_NEW_PROCESS_GROUP,
        )
    except BaseException:
        kernel32.CloseHandle(job)
        raise
    handle = int(popen._handle)  # type: ignore[attr-defined]  # noqa: SLF001
    if not kernel32.AssignProcessToJobObject(job, handle):
        error = _last_error()
        popen.kill()
        popen.wait()
        kernel32.CloseHandle(job)
        raise JobAssignmentError(f"AssignProcessToJobObject failed: {error}")
    _ntdll().NtResumeProcess(handle)
    return popen, WindowsJobGroup(job)
