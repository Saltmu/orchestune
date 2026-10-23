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

from orchestune.session_dirs import (
    SessionDirError,
    create_session_dir,
    find_project_root,
)
from tests.quint_replay import (
    BACKEND,
    MODEL,
    ROOT,
    ModelRun,
    ReplayFormatError,
    ToolError,
    Trace,
    parse_trace,
    pinned_quint_version,
)
from tests.quint_scenarios import SCENARIO_BOUNDS, run_scenario, scenario_ids


@dataclasses.dataclass(frozen=True)
class ScenarioExecutionContext:
    """Fixed execution context snapshot for cached scenario runs."""

    quint_version: str
    backend: str
    model_path: str
    seed: str
    max_samples: int
    max_steps: int

    @classmethod
    def current(cls) -> ScenarioExecutionContext:
        return cls(
            quint_version=pinned_quint_version(),
            backend=BACKEND,
            model_path=MODEL.relative_to(ROOT).as_posix(),
            seed=SCENARIO_BOUNDS.seed,
            max_samples=SCENARIO_BOUNDS.max_samples,
            max_steps=SCENARIO_BOUNDS.max_steps,
        )


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
        self.context = ScenarioExecutionContext.current()
        self._cache: dict[str, ModelRun] = {}

        if session_dir is not None:
            self.session_dir = session_dir.resolve()
            self.session_dir.mkdir(parents=True, exist_ok=True)
        else:
            base_project = (project_dir or find_project_root()).resolve()
            # In synthetic test environments without git / gitignore, supply local ignore
            if (
                not (base_project / ".git").exists()
                and not (base_project / ".gitignore").is_file()
            ):
                base_project.mkdir(parents=True, exist_ok=True)
                (base_project / ".gitignore").write_text(
                    ".orchestune/tmp/\n", encoding="utf-8"
                )
            try:
                self.session_dir = create_session_dir(
                    "quint-scenario-cache", "1367", project_dir=base_project
                )
            except SessionDirError:
                tmp_dir = base_project / ".orchestune" / "tmp"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                import uuid

                self.session_dir = (
                    tmp_dir / f"quint-scenario-cache-1367-{uuid.uuid4().hex[:8]}"
                )
                self.session_dir.mkdir(parents=True, exist_ok=True)

    def get(self, scenario: str) -> ModelRun:
        if scenario not in self.known_scenarios or "/" in scenario or "\\" in scenario:
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
