"""Cache support for Quint scenario runs and stored trace parsing (#1367).

Provides worker-local caching of deterministic ModelRun artifacts and parsed
Stored traces to avoid duplicate Quint CLI and JSON parsing overhead across
model verification and control replay tests.
"""

from __future__ import annotations

import copy
import dataclasses
import shutil
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from orchestune.session_dirs import create_session_dir
from tests.quint_replay import (
    ModelRun,
    ReplayFormatError,
    ToolError,
    Trace,
    parse_trace,
)
from tests.quint_scenarios import run_scenario, scenario_ids


class ScenarioRunCache:
    """Worker-scoped cache for deterministic scripted ModelRun executions.

    Each scenario is executed at most once with a dedicated out_dir.
    Cached ModelRun results are returned as defensive copies.
    """

    def __init__(
        self,
        known_scenarios: Iterable[str] | None = None,
        runner: Callable[..., ModelRun] | None = None,
        project_dir: Path | None = None,
        session_dir: Path | None = None,
    ) -> None:
        self.known_scenarios: frozenset[str] = (
            frozenset(known_scenarios)
            if known_scenarios is not None
            else frozenset(scenario_ids())
        )
        self._runner = runner or run_scenario
        self._cache: dict[str, ModelRun] = {}

        if session_dir is not None:
            self.session_dir = session_dir.resolve()
            self.session_dir.mkdir(parents=True, exist_ok=True)
            self._owns_dir = False
        else:
            self.session_dir = create_session_dir(
                "quint-scenario-cache", "1367", project_dir=project_dir
            )
            self._owns_dir = True

    def get(self, scenario: str) -> ModelRun:
        if scenario not in self.known_scenarios:
            raise KeyError(f"unknown scenario: {scenario}")

        if scenario in self._cache:
            return dataclasses.replace(self._cache[scenario])

        scenario_dir = self.session_dir / scenario
        scenario_dir.mkdir(parents=True, exist_ok=True)

        run = self._runner(scenario, out_dir=scenario_dir)

        if run.outcome == "error":
            raise ToolError(
                f"scenario {scenario}: quint failed ({run.returncode}): {run.stdout}"
            )
        if run.outcome not in ("ok", "violation"):
            raise ToolError(
                f"scenario {scenario}: unexpected model outcome {run.outcome}"
            )

        if len(run.traces) != 1:
            raise ReplayFormatError(
                f"scenario {scenario}: expected 1 ITF trace, found {len(run.traces)}"
            )
        trace_path = run.traces[0]
        if not trace_path.is_file():
            raise ReplayFormatError(
                f"scenario {scenario}: missing ITF trace file {trace_path}"
            )
        if trace_path.stat().st_size == 0:
            raise ReplayFormatError(f"scenario {scenario}: generated ITF file is empty")

        self._cache[scenario] = run
        return dataclasses.replace(run)

    def __call__(self, scenario: str) -> ModelRun:
        return self.get(scenario)

    def close(self) -> None:
        if self._owns_dir:
            shutil.rmtree(self.session_dir, ignore_errors=True)
        self._cache.clear()

    def __enter__(self) -> ScenarioRunCache:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


class StoredTraceCache:
    """Lazy cache for parsed stored regression traces with deepcopy isolation.

    Parses raw STORED fixture entries at most once per scenario, and always
    returns a deepcopy of Trace so that caller modifications cannot pollute
    subsequent tests.
    """

    def __init__(
        self,
        stored: Mapping[str, Any] | None = None,
        parser: Callable[[dict[str, Any]], Trace] | None = None,
    ) -> None:
        self._stored: Mapping[str, Any] = stored if stored is not None else {}
        self._parser = parser or parse_trace
        self._cache: dict[str, Trace] = {}

    def get(self, scenario: str) -> Trace:
        if scenario not in self._stored:
            raise KeyError(f"unknown stored scenario: {scenario}")

        if scenario not in self._cache:
            raw_itf = self._stored[scenario]["itf"]
            self._cache[scenario] = self._parser(raw_itf)

        return copy.deepcopy(self._cache[scenario])

    def __call__(self, scenario: str) -> Trace:
        return self.get(scenario)
