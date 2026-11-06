"""Best-effort command measurements scoped to the current synchronous execution."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol

_LOGGER = logging.getLogger(__name__)


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
        except Exception:  # noqa: BLE001 - boundary: telemetry failure cannot replace command behavior
            _LOGGER.exception("command metrics unavailable callback failed")
            pass


@contextmanager
def command_observer_scope(observer: CommandObserver | None) -> Iterator[None]:
    token = None
    previous = None
    try:
        previous = _observer.get()
        token = _observer.set(observer)
    except Exception:  # noqa: BLE001 - boundary: context setup must not disable the measured operation
        _LOGGER.exception("command metrics context setup failed")
        _unavailable(observer)
    try:
        yield
    finally:
        if token is not None:
            try:
                _observer.reset(token)
            except Exception:  # noqa: BLE001 - boundary: context cleanup must not replace command behavior
                _LOGGER.exception("command metrics context reset failed")
                _unavailable(observer)
                try:
                    _observer.set(previous)
                except Exception:  # noqa: BLE001 - boundary: best-effort context restoration
                    _LOGGER.exception("command metrics context restoration failed")
                    pass


def _clock() -> float | None:
    try:
        value = time.monotonic()
        return value if math.isfinite(value) else None
    except Exception:  # noqa: BLE001 - boundary: telemetry clock failure cannot fail the measured operation
        _LOGGER.exception("command metrics clock read failed")
        return None


@contextmanager
def measure_gh_call(*, enabled: bool = True) -> Iterator[None]:
    try:
        observer = _observer.get() if enabled else None
    except Exception:  # noqa: BLE001 - boundary: unavailable telemetry must not fail the measured operation
        _LOGGER.exception("command metrics observer lookup failed")
        observer = None
    if observer is None:
        yield
        return
    try:
        observer.gh_started()
    except Exception:  # noqa: BLE001 - boundary: observer callback cannot replace command behavior
        _LOGGER.exception("command metrics start callback failed")
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
        except Exception:  # noqa: BLE001 - boundary: observer callback cannot replace command behavior
            _LOGGER.exception("command metrics finish callback failed")
            _unavailable(observer)
