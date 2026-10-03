"""Collect existing progress notifications without changing their boundaries."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from orchestune.dispatch.progress import ProgressSink

_TERMINAL_EVENTS = frozenset(
    {"completed", "failed", "held", "unknown", "launched", "warning"}
)


def unavailable_timings() -> dict[str, Any]:
    return {
        "version": 1,
        "scope": "cycle_before_events_record",
        "collection_status": "unavailable",
        "elapsed_seconds": None,
        "phases": {},
        "gh": {"calls": None, "seconds": None},
    }


class CycleTimingCollector:
    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self.clock = clock if clock is not None else time.monotonic
        self.started: float | None = None
        self.elapsed: float | None = None
        self.active: dict[tuple[str, int | None], float] = {}
        self.phases: dict[str, dict[str, Any]] = {}
        self.calls: int | None = 0
        self.gh_seconds: float | None = 0.0
        self.partial = False
        self.frozen: dict[str, Any] | None = None

    def _clock(self) -> float:
        value = self.clock()
        if not math.isfinite(value):
            raise ValueError("nonfinite measurement")
        return value

    def phase_unavailable(self) -> None:
        self.partial = True
        self.active.clear()
        self.started = None
        self.elapsed = None

    def observe(self, phase: str, event: str, *, task_issue: int | None = None) -> None:
        if self.frozen is not None:
            return
        try:
            self._observe(phase, event, task_issue)
        except Exception:
            self.partial = True
            self.active.pop((phase, task_issue), None)
            if phase in {"cycle", "events_record"}:
                self.started = None
                self.elapsed = None
            if phase == "events_record":
                self.frozen = self._snapshot()

    def _observe(self, phase: str, event: str, task_issue: int | None) -> None:
        if phase == "events_record" and event == "started":
            now = self._clock()
            if self.started is not None and now >= self.started:
                self.elapsed = now - self.started
            else:
                self.partial = True
            self.partial |= bool(self.active)
            self.frozen = self._snapshot()
            return
        if phase == "cycle":
            if event == "started":
                self.started = self._clock()
            return
        key = (phase, task_issue)
        if event == "started":
            self.partial |= key in self.active
            self.active[key] = self._clock()
        elif event in _TERMINAL_EVENTS and key in self.active:
            started = self.active.pop(key)
            seconds = self._clock() - started
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError("invalid phase duration")
            entry = self.phases.setdefault(phase, {"seconds": 0.0, "count": 0})
            total = entry["seconds"] + seconds
            if not math.isfinite(total):
                raise ValueError("invalid phase total")
            entry["seconds"] = total
            entry["count"] += 1

    def gh_started(self) -> None:
        if self.frozen is None and self.calls is not None:
            self.calls += 1

    def gh_finished(self, seconds: float | None) -> None:
        if self.frozen is not None:
            return
        if seconds is None or not math.isfinite(seconds) or seconds < 0:
            self.gh_seconds = None
            self.partial = True
        elif self.gh_seconds is not None:
            total = self.gh_seconds + seconds
            if math.isfinite(total):
                self.gh_seconds = total
            else:
                self.gh_seconds = None
                self.partial = True

    def gh_unavailable(self) -> None:
        if self.frozen is None:
            self.calls = None
            self.gh_seconds = None
            self.partial = True

    def _snapshot(self) -> dict[str, Any]:
        return {
            "version": 1,
            "scope": "cycle_before_events_record",
            "collection_status": "partial" if self.partial else "ok",
            "elapsed_seconds": round(self.elapsed, 6)
            if self.elapsed is not None
            else None,
            "phases": {
                phase: {"seconds": round(value["seconds"], 6), "count": value["count"]}
                for phase, value in self.phases.items()
            },
            "gh": {
                "calls": self.calls,
                "seconds": round(self.gh_seconds, 6)
                if self.gh_seconds is not None
                else None,
            },
        }

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self.frozen) if self.frozen is not None else self._snapshot()


class TimingProgress:
    def __init__(self, sink: ProgressSink, collector: CycleTimingCollector) -> None:
        self.sink = sink
        self.collector = collector

    def emit(
        self,
        phase: str,
        event: str,
        *,
        task_issue: int | None = None,
        reason: str | None = None,
    ) -> None:
        try:
            self.collector.observe(phase, event, task_issue=task_issue)
        except Exception:
            try:
                self.collector.phase_unavailable()
            except Exception:
                pass
        self.sink.emit(phase, event, task_issue=task_issue, reason=reason)


def timing_snapshot(collector: CycleTimingCollector | None) -> dict[str, Any]:
    if collector is not None:
        try:
            result = collector.snapshot()
            json.dumps(result, allow_nan=False)
            return result
        except Exception:
            pass
    return unavailable_timings()
