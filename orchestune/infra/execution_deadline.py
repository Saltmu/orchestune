"""Monotonic execution deadlines shared by the Integrator's process boundaries (#820).

One ``ExecutionScope`` is created per parent Issue integration cycle. It owns

* the cycle deadline (a single ``time.monotonic()`` reading, never refreshed per
  task, stage or wait),
* the independent cleanup budget that is spent *after* the deadline on stopping
  processes, draining output, rolling back and recording, and
* the per-call limit for auxiliary ``git``/``gh`` commands.

The active scope is carried in a ``ContextVar`` so the single ``git`` and ``gh``
execution points (``run_git`` and ``GitHubForge``) can bound their calls without
every caller threading a deadline through. Outside a scope nothing changes.

This module is dependency free (L0). It bounds *waiting*; it cannot bound the OS
process-creation API or uninterruptible kernel I/O, which is documented as a limit
of the termination guarantee.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

Clock = Callable[[], float]

PHASE_NORMAL = "normal"
PHASE_CLEANUP = "cleanup"


class ExecutionInterrupt(BaseException):  # noqa: N818 - a control-flow signal
    """Base of every execution-bound signal (#820).

    Deliberately a ``BaseException``: the Integrator's steps contain many best-effort
    ``except Exception`` clauses that log and continue. A deadline or timeout must not
    be absorbed there; it has to reach the pipeline, which stops, cleans up and
    records the cause.
    """


class ExecutionDeadlineExceeded(ExecutionInterrupt):
    """Raised instead of *starting* a command once the scope has no time left.

    ``before_step`` marks a refusal made by the pipeline before a step began, so no
    write of that step can have happened.
    """

    def __init__(
        self, stage: str, phase: str = PHASE_NORMAL, *, before_step: bool = False
    ) -> None:
        super().__init__(f"execution deadline exceeded before {stage} ({phase} phase)")
        self.stage = stage
        self.phase = phase
        self.before_step = before_step


class ExecutionCommandTimeout(ExecutionInterrupt):
    """A started git/gh command exceeded its bound; whether it took effect is unknown."""

    def __init__(self, command: str, timeout_seconds: float, phase: str) -> None:
        super().__init__(
            f"{command} exceeded its {timeout_seconds:g}s bound ({phase} phase)"
        )
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.phase = phase


@dataclass
class CleanupBudget:
    """One total budget for every stop/drain/rollback/record step after the deadline.

    The budget starts when it is first drawn on and is never re-armed, so stopping,
    rolling back and recording share the same 30 seconds instead of each taking a
    fresh allowance.
    """

    total_seconds: float
    clock: Clock = time.monotonic
    _started_at: float | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.total_seconds <= 0:
            raise ValueError("cleanup budget must be positive")

    def start(self) -> None:
        if self._started_at is None:
            self._started_at = self.clock()

    @property
    def started(self) -> bool:
        return self._started_at is not None

    def consumed(self) -> float:
        if self._started_at is None:
            return 0.0
        return max(0.0, self.clock() - self._started_at)

    def remaining(self) -> float:
        return max(0.0, self.total_seconds - self.consumed())

    def exhausted(self) -> bool:
        return self.started and self.remaining() <= 0.0


@dataclass
class ExecutionScope:
    """The deadline, cleanup budget and auxiliary-command limit of one cycle."""

    cycle_seconds: float
    cleanup_seconds: float
    command_seconds: float
    clock: Clock = time.monotonic
    cleanup: CleanupBudget = field(init=False)
    phase: str = field(default=PHASE_NORMAL, init=False)
    started_at: float = field(init=False)

    def __post_init__(self) -> None:
        for name in ("cycle_seconds", "cleanup_seconds", "command_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        self.started_at = self.clock()
        self.cleanup = CleanupBudget(self.cleanup_seconds, self.clock)

    @property
    def deadline(self) -> float:
        return self.started_at + self.cycle_seconds

    def elapsed(self) -> float:
        return max(0.0, self.clock() - self.started_at)

    def remaining(self) -> float:
        return max(0.0, self.deadline - self.clock())

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def stage_limit(self, stage_limit_seconds: float) -> float:
        """``min(stage limit, remaining cycle time)``; the effective wait bound."""
        return min(float(stage_limit_seconds), self.remaining())

    def check(self, stage: str) -> None:
        """Refuse to begin ordinary work after the deadline."""
        if self.phase == PHASE_NORMAL and self.expired():
            raise ExecutionDeadlineExceeded(stage, PHASE_NORMAL)
        if self.phase == PHASE_CLEANUP and self.cleanup.exhausted():
            raise ExecutionDeadlineExceeded(stage, PHASE_CLEANUP)

    def command_timeout(self, stage: str = "command") -> float:
        """Timeout for one auxiliary git/gh call in the current phase."""
        self.check(stage)
        if self.phase == PHASE_CLEANUP:
            self.cleanup.start()
            return self.cleanup.remaining()
        return min(self.command_seconds, self.remaining())

    @contextmanager
    def cleanup_phase(self) -> Iterator[ExecutionScope]:
        """Spend only the cleanup budget inside the block (stop, rollback, record)."""
        previous = self.phase
        self.cleanup.start()
        self.phase = PHASE_CLEANUP
        try:
            yield self
        finally:
            self.phase = previous


_ACTIVE_SCOPE: ContextVar[ExecutionScope | None] = ContextVar(
    "orchestune_execution_scope", default=None
)


def active_scope() -> ExecutionScope | None:
    return _ACTIVE_SCOPE.get()


@contextmanager
def activate_scope(scope: ExecutionScope) -> Iterator[ExecutionScope]:
    token = _ACTIVE_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _ACTIVE_SCOPE.reset(token)


def scoped_command_timeout(
    own_timeout: float | None, stage: str = "command"
) -> float | None:
    """Combine a caller's own timeout with the active scope's bound (if any)."""
    scope = active_scope()
    if scope is None:
        return own_timeout
    bound = scope.command_timeout(stage)
    return bound if own_timeout is None else min(own_timeout, bound)


def scope_bound_applies(own_timeout: float | None, effective: float | None) -> bool:
    """Whether the scope (not the caller's own timeout) set ``effective``."""
    return (
        active_scope() is not None
        and effective is not None
        and (own_timeout is None or effective < own_timeout)
    )


def command_timeout_signal(
    command: str, effective: float, error: BaseException
) -> ExecutionCommandTimeout:
    """The signal for a scope-bounded command that timed out."""
    scope = active_scope()
    phase = PHASE_NORMAL if scope is None else scope.phase
    signal = ExecutionCommandTimeout(command, effective, phase)
    signal.__cause__ = error
    return signal


__all__ = [
    "PHASE_CLEANUP",
    "PHASE_NORMAL",
    "CleanupBudget",
    "Clock",
    "ExecutionCommandTimeout",
    "ExecutionDeadlineExceeded",
    "ExecutionInterrupt",
    "ExecutionScope",
    "activate_scope",
    "active_scope",
    "command_timeout_signal",
    "scope_bound_applies",
    "scoped_command_timeout",
]
