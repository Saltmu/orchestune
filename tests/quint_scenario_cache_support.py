"""Cache support for Quint scenario runs and stored trace parsing (#1367).

Provides worker-local caching of deterministic ModelRun artifacts and parsed
Stored traces to avoid duplicate Quint CLI and JSON parsing overhead across
model verification and control replay tests.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import json
import os
import shutil
import uuid
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from orchestune.infra.process_utils import FileLockContentionError, file_lock
from orchestune.session_dirs import create_session_dir
from tests.quint_replay import (
    ModelRun,
    ReplayFormatError,
    ToolError,
    Trace,
    parse_trace,
)
from tests.quint_scenarios import SCENARIO_BOUNDS, run_scenario, scenario_ids


class ScenarioRunCache:
    """Worker-scoped cache for deterministic scripted ModelRun executions.

    Each scenario is executed at most once per shared session_dir with a dedicated
    out_dir. Cached ModelRun results are returned as defensive copies.
    """

    def __init__(
        self,
        known_scenarios: Iterable[str] | None = None,
        runner: Callable[..., ModelRun] | None = None,
        project_dir: Path | None = None,
        session_dir: Path | None = None,
        lock_timeout: float = SCENARIO_BOUNDS.timeout + 60,
    ) -> None:
        self.known_scenarios: frozenset[str] = (
            frozenset(known_scenarios)
            if known_scenarios is not None
            else frozenset(scenario_ids())
        )
        self._runner = runner or run_scenario
        self._cache: dict[str, ModelRun] = {}
        self._lock_timeout = lock_timeout

        if session_dir is not None:
            self.session_dir = session_dir.resolve()
            self.session_dir.mkdir(parents=True, exist_ok=True)
            self._owns_dir = False
        else:
            self.session_dir = create_session_dir(
                "quint-scenario-cache", "1367", project_dir=project_dir
            )
            self._owns_dir = True

    def _read_disk_cache(self, scenario_dir: Path) -> ModelRun | None:
        meta_file = scenario_dir / ".model_run.json"
        if not meta_file.is_file():
            return None
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            if data.get("format") != 1:
                return None
            outcome = data.get("outcome")
            if outcome not in ("ok", "violation"):
                return None
            traces_raw = data.get("traces")
            if not isinstance(traces_raw, list) or len(traces_raw) != 1:
                return None
            rel_trace = traces_raw[0]
            if not isinstance(rel_trace, str) or not rel_trace:
                return None
            trace_path = scenario_dir / rel_trace
            if not trace_path.is_file() or trace_path.stat().st_size == 0:
                return None
            return ModelRun(
                outcome=outcome,
                returncode=int(data["returncode"]),
                violated=tuple(data.get("violated", ())),
                command=tuple(data.get("command", ())),
                seconds=float(data.get("seconds", 0.0)),
                stdout=str(data.get("stdout", "")),
                stderr=str(data.get("stderr", "")),
                traces=(trace_path,),
            )
        except Exception:
            return None

    def get(self, scenario: str) -> ModelRun:
        if scenario not in self.known_scenarios:
            raise KeyError(f"unknown scenario: {scenario}")

        if scenario in self._cache:
            return dataclasses.replace(self._cache[scenario])

        scenario_dir = self.session_dir / scenario
        scenario_dir.mkdir(parents=True, exist_ok=True)

        lock_path = self.session_dir / f"{scenario}.lock"
        try:
            with file_lock(lock_path, timeout=self._lock_timeout):
                if scenario in self._cache:
                    return dataclasses.replace(self._cache[scenario])

                cached = self._read_disk_cache(scenario_dir)
                if cached is not None:
                    self._cache[scenario] = cached
                    return dataclasses.replace(cached)

                # Remove prior stale trace files and broken metadata
                meta_file = scenario_dir / ".model_run.json"
                if scenario_dir.is_dir():
                    for old_trace in scenario_dir.glob("trace_*.itf.json"):
                        with contextlib.suppress(OSError):
                            old_trace.unlink(missing_ok=True)
                    with contextlib.suppress(OSError):
                        meta_file.unlink(missing_ok=True)

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
                    raise ReplayFormatError(
                        f"scenario {scenario}: generated ITF file is empty"
                    )

                try:
                    rel_trace = trace_path.relative_to(scenario_dir).as_posix()
                except ValueError:
                    rel_trace = trace_path.name

                payload = {
                    "format": 1,
                    "outcome": run.outcome,
                    "returncode": run.returncode,
                    "violated": list(run.violated),
                    "command": list(run.command),
                    "seconds": run.seconds,
                    "stdout": run.stdout,
                    "stderr": run.stderr,
                    "traces": [rel_trace],
                }
                scenario_dir.mkdir(parents=True, exist_ok=True)
                tmp_meta = scenario_dir / f".model_run.json.tmp.{uuid.uuid4().hex}"
                tmp_meta.write_text(json.dumps(payload), encoding="utf-8")
                os.replace(tmp_meta, meta_file)

                self._cache[scenario] = run
                return dataclasses.replace(run)
        except FileLockContentionError as exc:
            raise ToolError(
                f"scenario {scenario}: lock contention timed out waiting for {lock_path}"
            ) from exc

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
