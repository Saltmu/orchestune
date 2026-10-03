"""Run an external command under a deadline and own its whole process group (#820).

``subprocess.run(timeout=...)`` only kills the direct child and then blocks in
``communicate()`` while a surviving descendant keeps the output pipes open. This
adapter instead

* starts the command in a process group it owns (POSIX session / Windows Job Object),
* reads stdout/stderr concurrently into bounded tails,
* on timeout stops the *group*, confirms it is empty and drains the output, all
  against one cleanup budget, and
* reports a typed outcome instead of folding every failure into "command failed".

``shell=False`` always; OS specific code lives in ``managed_process_posix`` and
``managed_process_windows``. A group that cannot be confirmed stopped is reported as
``STOP_UNCONFIRMED`` and is never treated as success or as retryable.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO, Protocol

from orchestune.infra.execution_deadline import CleanupBudget
from orchestune.infra.managed_process_posix import (
    start_owned_process as start_posix_process,
)
from orchestune.infra.managed_process_windows import (
    start_owned_process as start_windows_process,
)

DEFAULT_TAIL_BYTES = 64 * 1024
DEFAULT_TERM_GRACE_SECONDS = 5.0
DEFAULT_CLEANUP_SECONDS = 30.0
_POLL_SECONDS = 0.02
_FREE_DRAIN_SECONDS = 1.0
_READ_CHUNK = 65536


class ProcessOutcome(StrEnum):
    SUCCESS = "success"
    NONZERO_EXIT = "nonzero_exit"
    TIMED_OUT = "timed_out"
    START_FAILED = "start_failed"
    STOP_UNCONFIRMED = "stop_unconfirmed"


@dataclass(frozen=True)
class ManagedProcessSpec:
    """One command to run: argv list, cwd, env, stage label and effective timeout."""

    args: Sequence[str]
    stage: str
    timeout_seconds: float
    cwd: Path | str | None = None
    env: Mapping[str, str] | None = None
    cleanup: CleanupBudget | None = None
    tail_bytes: int = DEFAULT_TAIL_BYTES
    term_grace_seconds: float = DEFAULT_TERM_GRACE_SECONDS


@dataclass(frozen=True)
class ManagedProcessResult:
    outcome: ProcessOutcome
    stage: str
    returncode: int | None
    elapsed_seconds: float
    timeout_seconds: float
    stdout_tail: str = ""
    stderr_tail: str = ""
    # True: the owned group was observed empty. False: it could not be confirmed.
    # None: no group was ever started.
    stop_confirmed: bool | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is ProcessOutcome.SUCCESS


class ProcessGroup(Protocol):
    """A group of processes the runner owns and can stop as a unit."""

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def alive(self) -> bool: ...

    def close(self) -> None: ...


class ProcessRunner(Protocol):
    def run(self, spec: ManagedProcessSpec) -> ManagedProcessResult: ...


class _TailReader(threading.Thread):
    """Drain one pipe, keeping only the last ``limit`` bytes."""

    def __init__(self, stream: IO[bytes] | None, limit: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = max(1, limit)
        self._chunks: deque[bytes] = deque()
        self._size = 0

    def run(self) -> None:
        stream = self._stream
        if stream is None:
            return
        read = getattr(stream, "read1", None) or stream.read
        try:
            while True:
                chunk = read(_READ_CHUNK)
                if not chunk:
                    return
                self._chunks.append(chunk)
                self._size += len(chunk)
                while self._size - len(self._chunks[0]) >= self._limit:
                    self._size -= len(self._chunks.popleft())
        except (OSError, ValueError):
            return

    def tail(self) -> str:
        data = b"".join(self._chunks)[-self._limit :]
        return data.decode("utf-8", errors="replace")


def _wait_until(predicate: Callable[[], bool], seconds: float) -> bool:
    """Poll ``predicate`` for at most ``seconds``; return its last value."""
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_SECONDS)


def _stop_group(
    group: ProcessGroup, budget: CleanupBudget, term_grace_seconds: float
) -> bool:
    """Terminate, wait for a grace period, kill, and confirm the group is empty."""
    if not group.alive():
        return True
    budget.start()
    group.terminate()
    if _wait_until(
        lambda: not group.alive(), min(term_grace_seconds, budget.remaining())
    ):
        return True
    group.kill()
    return _wait_until(lambda: not group.alive(), budget.remaining())


def _start(
    spec: ManagedProcessSpec,
) -> tuple[subprocess.Popen[bytes], ProcessGroup]:
    start_owned_process = (
        start_windows_process if sys.platform == "win32" else start_posix_process
    )
    return start_owned_process(list(spec.args), spec.cwd, spec.env)


class ManagedProcessRunner:
    """The real runner: owns the process group and stops it on timeout."""

    def run(self, spec: ManagedProcessSpec) -> ManagedProcessResult:
        return run_managed(spec)


_default_runner: ProcessRunner = ManagedProcessRunner()


def get_default_runner() -> ProcessRunner:
    """The runner used when a caller does not inject one."""
    return _default_runner


def set_default_runner(runner: ProcessRunner) -> ProcessRunner:
    """Replace the default runner (test seam); returns the previous one."""
    global _default_runner
    previous = _default_runner
    _default_runner = runner
    return previous


def _result(
    spec: ManagedProcessSpec,
    outcome: ProcessOutcome,
    started: float,
    *,
    returncode: int | None = None,
    stdout: str = "",
    stderr: str = "",
    stop_confirmed: bool | None = None,
    detail: str = "",
) -> ManagedProcessResult:
    return ManagedProcessResult(
        outcome=outcome,
        stage=spec.stage,
        returncode=returncode,
        elapsed_seconds=max(0.0, time.monotonic() - started),
        timeout_seconds=spec.timeout_seconds,
        stdout_tail=stdout,
        stderr_tail=stderr,
        stop_confirmed=stop_confirmed,
        detail=detail,
    )


def run_managed(spec: ManagedProcessSpec) -> ManagedProcessResult:
    started = time.monotonic()
    if spec.timeout_seconds <= 0:
        return _result(
            spec,
            ProcessOutcome.TIMED_OUT,
            started,
            detail="deadline expired before the command started",
        )
    budget = spec.cleanup or CleanupBudget(DEFAULT_CLEANUP_SECONDS)
    try:
        popen, group = _start(spec)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return _result(
            spec,
            ProcessOutcome.START_FAILED,
            started,
            detail=f"{type(error).__name__}: {error}",
        )

    readers = (
        _TailReader(popen.stdout, spec.tail_bytes),
        _TailReader(popen.stderr, spec.tail_bytes),
    )
    for reader in readers:
        reader.start()
    try:
        return _supervise(spec, popen, group, readers, budget, started)
    finally:
        group.close()


def _supervise(
    spec: ManagedProcessSpec,
    popen: subprocess.Popen[bytes],
    group: ProcessGroup,
    readers: tuple[_TailReader, _TailReader],
    budget: CleanupBudget,
    started: float,
) -> ManagedProcessResult:
    timed_out = False
    try:
        popen.wait(timeout=spec.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True

    if timed_out:
        confirmed = _stop_group(group, budget, spec.term_grace_seconds)
        detail = "command exceeded its time limit"
    else:
        # The main process exited; descendants it left behind are still ours to stop.
        leftovers = group.alive()
        confirmed = _stop_group(group, budget, spec.term_grace_seconds)
        detail = "descendant processes remained after exit" if leftovers else ""

    returncode = _reap(popen, budget)
    if not _drain(readers, budget):
        detail = (detail + "; " if detail else "") + "output drain incomplete"
    outcome = _final_outcome(confirmed, timed_out, returncode)
    if outcome is ProcessOutcome.STOP_UNCONFIRMED:
        detail = (
            (detail + "; " if detail else "")
            + "owned process group could not be confirmed stopped"
            + (" after timeout" if timed_out else "")
        )
    return _result(
        spec,
        outcome,
        started,
        returncode=returncode,
        stdout=readers[0].tail(),
        stderr=readers[1].tail(),
        stop_confirmed=confirmed,
        detail=detail,
    )


def _final_outcome(
    confirmed: bool, timed_out: bool, returncode: int | None
) -> ProcessOutcome:
    """An unconfirmed stop outranks everything; success needs a confirmed, clean exit."""
    if not confirmed:
        return ProcessOutcome.STOP_UNCONFIRMED
    if timed_out:
        return ProcessOutcome.TIMED_OUT
    return ProcessOutcome.SUCCESS if returncode == 0 else ProcessOutcome.NONZERO_EXIT


def _reap(popen: subprocess.Popen[bytes], budget: CleanupBudget) -> int | None:
    try:
        return popen.wait(timeout=max(0.001, budget.remaining()))
    except subprocess.TimeoutExpired:
        return None


def _join_all(readers: tuple[_TailReader, _TailReader], seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, seconds)
    for reader in readers:
        reader.join(timeout=max(0.0, deadline - time.monotonic()))
    return not any(reader.is_alive() for reader in readers)


def _drain(readers: tuple[_TailReader, _TailReader], budget: CleanupBudget) -> bool:
    """Join readers against the cleanup budget so a held pipe cannot block forever.

    With every writer gone the readers reach EOF at once, so a short free window
    covers the normal case; only a pipe still held open arms the shared budget.
    """
    if _join_all(readers, min(_FREE_DRAIN_SECONDS, budget.remaining())):
        return True
    budget.start()
    return _join_all(readers, budget.remaining())


__all__ = [
    "DEFAULT_CLEANUP_SECONDS",
    "DEFAULT_TAIL_BYTES",
    "DEFAULT_TERM_GRACE_SECONDS",
    "ManagedProcessResult",
    "ManagedProcessRunner",
    "ManagedProcessSpec",
    "ProcessGroup",
    "ProcessOutcome",
    "ProcessRunner",
    "get_default_runner",
    "run_managed",
    "set_default_runner",
]
