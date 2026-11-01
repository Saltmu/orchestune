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
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from orchestune.infra.process_utils import FileLockContentionError, file_lock
from orchestune.session_dirs import create_session_dir
from tests.quint_replay import (
    INVARIANTS,
    MODEL,
    ROOT,
    ModelRun,
    ReplayFormatError,
    ToolError,
    Trace,
    _run_bounded,
    classify_run,
    command_line,
    parse_trace,
    quint_executable,
)
from tests.quint_scenarios import SCENARIO_BOUNDS, run_scenario, scenario_ids

_BATCH_PREWARM_JS = """\
const fs = require("fs");
const path = require("path");

(async () => {
    const reqPath = process.argv[2];
    const req = JSON.parse(fs.readFileSync(reqPath, "utf-8"));
    const cliPath = path.resolve(req.root, "node_modules/@informalsystems/quint/dist/src/cliCommands.js");
    const cli = require(cliPath);

    const loaded = await cli.load({ input: req.model });
    if (loaded.isLeft()) {
        throw new Error("failed to load model: " + JSON.stringify(loaded.value));
    }
    const parsed = await cli.parse(loaded.value);
    if (parsed.isLeft()) {
        throw new Error("failed to parse model: " + JSON.stringify(parsed.value));
    }
    const typechecked = await cli.typecheck(parsed.value);
    if (typechecked.isLeft()) {
        throw new Error("failed to typecheck model: " + JSON.stringify(typechecked.value));
    }

    const results = {};
    for (const scenario of req.scenarios) {
        const scenarioDir = path.resolve(req.out_root, scenario);
        fs.mkdirSync(scenarioDir, { recursive: true });

        const outItf = path.posix.join(
            scenarioDir.replace(/\\\\/g, "/"),
            "trace_{seq}.itf.json"
        );

        const args = {
            input: req.model,
            backend: "typescript",
            seed: BigInt(req.seed),
            maxSamples: req.max_samples,
            maxSteps: req.max_steps,
            verbosity: 1,
            mbt: true,
            nTraces: req.n_traces,
            init: `init_${scenario}`,
            step: "step",
            invariant: "true",
            invariants: req.invariants,
            witnesses: [],
            hide: [],
            outItf: outItf,
        };

        let stdoutBuf = "";
        let stderrBuf = "";
        const origLog = console.log;
        const origErr = console.error;
        console.log = (...msgs) => {
            stdoutBuf += msgs.join(" ") + "\\n";
        };
        console.error = (...msgs) => {
            stderrBuf += msgs.join(" ") + "\\n";
        };
        const start = Date.now();
        try {
            const res = await cli.runSimulator({ ...typechecked.value, args });
            const elapsed = (Date.now() - start) / 1000;
            results[scenario] = {
                right: res.isRight(),
                stdout: stdoutBuf,
                stderr: stderrBuf,
                seconds: elapsed,
            };
        } catch (err) {
            const elapsed = (Date.now() - start) / 1000;
            results[scenario] = {
                right: false,
                stdout: stdoutBuf,
                stderr: stderrBuf + (err && err.stack ? err.stack : String(err)),
                seconds: elapsed,
            };
        } finally {
            console.log = origLog;
            console.error = origErr;
        }
    }

    const resultsPath = path.resolve(req.out_root, "_batch_results.json");
    fs.writeFileSync(resultsPath, JSON.stringify(results), "utf-8");
})().catch(err => {
    console.error(err);
    process.exit(1);
});
"""


def _persist(scenario_dir: Path, run: ModelRun) -> None:
    scenario = scenario_dir.name
    if run.outcome == "error":
        raise ToolError(
            f"scenario {scenario}: quint failed ({run.returncode}): {run.stdout}"
        )
    if run.outcome not in ("ok", "violation"):
        raise ToolError(f"scenario {scenario}: unexpected model outcome {run.outcome}")

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

    meta_file = scenario_dir / ".model_run.json"
    if scenario_dir.is_dir():
        for old_trace in scenario_dir.glob("trace_*.itf.json"):
            if old_trace.resolve() != trace_path.resolve():
                with contextlib.suppress(OSError):
                    old_trace.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            meta_file.unlink(missing_ok=True)

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
    # Short name: long worktree paths otherwise exceed Windows MAX_PATH (#1531).
    tmp_meta = scenario_dir / f".model_run.{uuid.uuid4().hex[:12]}.tmp"
    tmp_meta.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp_meta, meta_file)


def _batch_prewarm_scenarios(
    missing: Sequence[str],
    session_dir: Path,
    *,
    timeout: float = SCENARIO_BOUNDS.timeout,
) -> set[str]:
    if not missing:
        return set()

    node_bin = shutil.which("node")
    if node_bin is None:
        raise ToolError("node executable not found")

    quint_executable()

    for sc in missing:
        sc_dir = session_dir / sc
        if sc_dir.is_dir():
            for old_trace in sc_dir.glob("trace_*.itf.json"):
                with contextlib.suppress(OSError):
                    old_trace.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                (sc_dir / ".model_run.json").unlink(missing_ok=True)

    session_dir.mkdir(parents=True, exist_ok=True)
    script_path = session_dir / "_batch_prewarm.js"
    script_path.write_text(_BATCH_PREWARM_JS, encoding="utf-8")

    request_payload = {
        "root": str(ROOT),
        "model": str(MODEL),
        "out_root": str(session_dir),
        "scenarios": list(missing),
        "seed": SCENARIO_BOUNDS.seed,
        "max_samples": SCENARIO_BOUNDS.max_samples,
        "max_steps": SCENARIO_BOUNDS.max_steps,
        "n_traces": SCENARIO_BOUNDS.n_traces,
        "invariants": list(INVARIANTS),
    }
    request_path = session_dir / "_batch_request.json"
    request_path.write_text(json.dumps(request_payload), encoding="utf-8")

    results_path = session_dir / "_batch_results.json"
    with contextlib.suppress(OSError):
        results_path.unlink(missing_ok=True)

    done = _run_bounded(
        [node_bin, str(script_path), str(request_path)],
        timeout=timeout,
    )
    if done.returncode != 0:
        raise ToolError(
            f"batch prewarm node process failed ({done.returncode}): "
            f"{done.stderr or done.stdout}"
        )

    if not results_path.is_file():
        raise ToolError("batch prewarm results file missing")

    raw_results = json.loads(results_path.read_text(encoding="utf-8"))
    if not isinstance(raw_results, dict):
        raise ToolError("batch prewarm results format invalid")

    persisted: set[str] = set()
    for sc in missing:
        entry = raw_results.get(sc)
        if not isinstance(entry, dict):
            continue
        right = bool(entry.get("right", False))
        stdout = str(entry.get("stdout", ""))
        stderr = str(entry.get("stderr", ""))
        seconds = float(entry.get("seconds", 0.0))
        returncode = 0 if right else 1
        outcome, violated = classify_run(returncode, stdout)
        if outcome not in ("ok", "violation"):
            continue

        sc_dir = session_dir / sc
        traces = tuple(sorted(sc_dir.glob("trace_*.itf.json")))
        if len(traces) != 1 or not traces[0].is_file() or traces[0].stat().st_size == 0:
            continue

        cmd = command_line(
            SCENARIO_BOUNDS,
            init=f"init_{sc}",
            out_dir=sc_dir,
            model=str(MODEL),
            executable=str(quint_executable()),
        )
        run = ModelRun(
            outcome=outcome,
            returncode=returncode,
            violated=violated,
            command=tuple(cmd),
            seconds=seconds,
            stdout=stdout,
            stderr=stderr,
            traces=traces,
        )
        try:
            _persist(sc_dir, run)
            persisted.add(sc)
        except Exception:
            continue

    return persisted


class ScenarioRunCache:
    """Worker-scoped cache for deterministic scripted ModelRun executions.

    Each scenario is executed at most once per shared session_dir with a dedicated
    out_dir. Cached ModelRun results are returned as defensive copies.
    """

    def __init__(
        self,
        known_scenarios: Iterable[str] | None = None,
        runner: Callable[..., ModelRun] | None = None,
        prewarmer: Callable[[Sequence[str], Path], set[str]] | None = None,
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
        if prewarmer is not None:
            self._prewarmer: Callable[[Sequence[str], Path], set[str]] | None = (
                prewarmer
            )
        elif runner is None:
            self._prewarmer = _batch_prewarm_scenarios
        else:
            self._prewarmer = None
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

                if self._prewarmer is not None:
                    batch_lock = self.session_dir / "_batch_prewarm.lock"
                    try:
                        with file_lock(batch_lock, timeout=self._lock_timeout):
                            cached = self._read_disk_cache(scenario_dir)
                            if cached is not None:
                                self._cache[scenario] = cached
                                return dataclasses.replace(cached)

                            missing = [
                                sc
                                for sc in sorted(self.known_scenarios)
                                if self._read_disk_cache(self.session_dir / sc) is None
                            ]
                            if missing:
                                self._prewarmer(missing, self.session_dir)
                    except Exception:
                        self._prewarmer = None

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
                _persist(scenario_dir, run)
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
