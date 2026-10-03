"""POSIX process-group ownership for ``managed_process`` (#820).

The command is started as the leader of a new session, so its pid is also the id of
a process group that contains every descendant that stays in that group. Stopping
signals the *group* (``SIGTERM``, then ``SIGKILL``) and liveness is the group having
no running member.

A daemon that deliberately leaves the session (``setsid``) is outside this guarantee;
catching it would need a cgroup/sandbox, which is out of scope. CI commands are
expected to keep their descendants inside the managed group.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

_PROC = Path("/proc")


def _linux_live_members(pgid: int) -> bool | None:
    """Whether a non-zombie member of ``pgid`` exists; ``None`` if /proc is unusable.

    A zombie whose parent never reaps it (e.g. PID 1 in a bare container) keeps
    ``killpg(pgid, 0)`` succeeding forever, which would make a stopped group look
    alive. Zombies are not running code, so they do not count as members.
    """
    if not _PROC.is_dir():
        return None
    try:
        entries = [entry for entry in _PROC.iterdir() if entry.name.isdigit()]
    except OSError:
        return None
    for entry in entries:
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # exited between listing and reading
        # ``pid (comm) state ppid pgrp ...``; comm may contain spaces and parens.
        tail = stat.rpartition(")")[2].split()
        if len(tail) < 3:
            continue
        state, pgrp = tail[0], tail[2]
        if pgrp == str(pgid) and state not in ("Z", "X"):
            return True
    return False


class PosixProcessGroup:
    """The session/process group started for one managed command."""

    def __init__(self, pgid: int) -> None:
        self._pgid = pgid

    def _signal(self, sig: int) -> None:
        if sys.platform == "win32":  # pragma: no cover - POSIX only
            raise OSError("POSIX process groups are unavailable on Windows")
        try:
            os.killpg(self._pgid, sig)
        except ProcessLookupError:
            return

    def terminate(self) -> None:
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        self._signal(getattr(signal, "SIGKILL", signal.SIGTERM))

    def alive(self) -> bool:
        if sys.platform == "win32":  # pragma: no cover - POSIX only
            return False
        try:
            os.killpg(self._pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        live = _linux_live_members(self._pgid)
        return True if live is None else live

    def close(self) -> None:
        return None


def start_owned_process(
    args: list[str],
    cwd: Path | str | None,
    env: Mapping[str, str] | None,
) -> tuple[subprocess.Popen[bytes], PosixProcessGroup]:
    popen = subprocess.Popen(
        args,
        cwd=None if cwd is None else str(cwd),
        env=None if env is None else dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
    )
    return popen, PosixProcessGroup(popen.pid)
