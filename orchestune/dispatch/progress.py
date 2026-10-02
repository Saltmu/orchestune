"""Optional, best-effort dispatch boundary notifications."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol, TextIO


class ProgressSink(Protocol):
    def emit(
        self,
        phase: str,
        event: str,
        *,
        task_issue: int | None = None,
        reason: str | None = None,
    ) -> None: ...


class NoopProgress:
    def emit(
        self,
        phase: str,
        event: str,
        *,
        task_issue: int | None = None,
        reason: str | None = None,
    ) -> None:
        pass


def _stdout_stream() -> tuple[TextIO, bool]:
    """Keep failed writes out of the interpreter's final stdout flush.

    Only the sink's duplicated descriptor is closed on failure. Global stdout
    and injected/captured Python streams keep their existing ownership.
    """
    try:
        fd = os.dup(sys.stdout.fileno())
    except (AttributeError, OSError, ValueError):
        return sys.stdout, False
    try:
        return os.fdopen(
            fd, "w", encoding=sys.stdout.encoding or "utf-8", errors="replace"
        ), True
    except OSError:
        os.close(fd)
        raise


class StdoutProgress:
    def __init__(
        self,
        run_id: str,
        parent_issue: int | None,
        apply: bool | None,
        *,
        stream: TextIO | None = None,
    ) -> None:
        self.run_id = run_id
        self.parent_issue = parent_issue
        self.apply = apply
        self.stream, self.owns_stream = (
            (stream, False) if stream is not None else _stdout_stream()
        )
        self.disabled = False

    def emit(
        self,
        phase: str,
        event: str,
        *,
        task_issue: int | None = None,
        reason: str | None = None,
    ) -> None:
        if self.disabled:
            return
        mode = (
            "configuration"
            if self.apply is None
            else "apply"
            if self.apply
            else "dry-run"
        )
        parts = [self.run_id, f"parent={self.parent_issue}", mode, phase, event]
        if task_issue is not None:
            parts.append(f"task={task_issue}")
        if reason:
            parts.append(" ".join(reason.splitlines()))
        try:
            print(" / ".join(parts), file=self.stream, flush=True)
        except (OSError, ValueError):
            self.close()
            safe_stderr(
                "Warning: progress unavailable; continuing dispatch and report saving"
            )

    def close(self) -> None:
        self.disabled = True
        if self.owns_stream:
            try:
                self.stream.close()
            except (OSError, ValueError):
                pass


def safe_stderr(message: str) -> None:
    try:
        print(message, file=sys.stderr)
    except (OSError, ValueError):
        pass


@contextmanager
def progress_phase(sink: ProgressSink, phase: str) -> Iterator[None]:
    sink.emit(phase, "started")
    try:
        yield
    except BaseException:
        sink.emit(phase, "failed")
        raise
    else:
        sink.emit(phase, "completed")
