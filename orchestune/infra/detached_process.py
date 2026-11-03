"""Detached process launcher for agent CLI processes."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class DetachedProcess:
    """Represents a running detached agent process and its log location."""

    pid: int
    log_path: Path
    log_offset: int


class DetachedProcessStarter(Protocol):
    """Protocol for detached process launch function or callable double."""

    def __call__(
        self,
        args: list[str],
        *,
        cwd: str | Path,
        log_path: Path,
    ) -> DetachedProcess: ...


def start_detached_process(
    args: list[str],
    *,
    cwd: str | Path,
    log_path: Path,
) -> DetachedProcess:
    """Start a detached agent process logging to a file in append mode.

    The log file is opened in append mode ('ab') and its position before launch
    is recorded as log_offset so that only output from this invocation is owned.
    stdout and stderr share the same file handle.
    start_new_session=True creates a new process session on POSIX (ignored on Windows).
    No Windows creationflags (such as CREATE_NEW_PROCESS_GROUP) or Job Object are used.
    """
    resolved_log_path = Path(log_path)
    resolved_log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(resolved_log_path, "ab") as log_fh:
        log_offset = log_fh.tell()
        process = subprocess.Popen(
            args,
            cwd=str(cwd),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return DetachedProcess(
        pid=process.pid,
        log_path=resolved_log_path,
        log_offset=log_offset,
    )
