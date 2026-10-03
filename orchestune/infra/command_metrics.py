"""Best-effort command measurements scoped to the current synchronous execution."""

from __future__ import annotations

import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol


class CommandObserver(Protocol):
    def gh_started(self) -> None: ...
    def gh_finished(self, seconds: float | None) -> None: ...
    def gh_unavailable(self) -> None: ...


_observer: ContextVar[CommandObserver | None] = ContextVar(
    "command_observer", default=None
)


def _unavailable(observer: CommandObserver | None) -> None:
    if observer is not None:
        try:
            observer.gh_unavailable()
        except Exception:
            pass


@contextmanager
def command_observer_scope(observer: CommandObserver | None) -> Iterator[None]:
    token = None
    previous = None
    try:
        previous = _observer.get()
        token = _observer.set(observer)
    except Exception:
        _unavailable(observer)
    try:
        yield
    finally:
        if token is not None:
            try:
                _observer.reset(token)
            except Exception:
                _unavailable(observer)
                try:
                    _observer.set(previous)
                except Exception:
                    pass


def _clock() -> float | None:
    try:
        value = time.monotonic()
        return value if math.isfinite(value) else None
    except Exception:
        return None


@contextmanager
def measure_gh_call(*, enabled: bool = True) -> Iterator[None]:
    try:
        observer = _observer.get() if enabled else None
    except Exception:
        observer = None
    if observer is None:
        yield
        return
    try:
        observer.gh_started()
    except Exception:
        _unavailable(observer)
    started = _clock()
    try:
        yield
    finally:
        finished = _clock()
        seconds = (
            finished - started
            if started is not None and finished is not None and finished >= started
            else None
        )
        try:
            observer.gh_finished(seconds)
        except Exception:
            _unavailable(observer)
