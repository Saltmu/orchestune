"""Saved Quint scenarios, regression fixtures and the CI exploration (#1276).

The replay itself lives in ``tests/quint_replay.py``; this module holds what the
tests and the local CI feed it: the scenario lists, the fixed exploration bounds,
the fixture readers and the command that regenerates the saved traces.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from tests.dependency_liveness_test_support import DEPENDENT
from tests.quint_replay import (
    BACKEND,
    MODEL,
    ROOT,
    WITNESSES,
    Exploration,
    KnownDefect,
    ModelRun,
    ReplayFormatError,
    ReplayReport,
    ToolError,
    Trace,
    command_line,
    normalized,
    pinned_quint_version,
    run_model,
)

# ---- scenarios, fixtures and the CI exploration ---------------------------------

FIXTURES = ROOT / "tests" / "fixtures" / "quint"
TRACES_FIXTURE = FIXTURES / "dependency_liveness_traces.json"
FAULTS_FIXTURE = FIXTURES / "dependency_liveness_fault_scenarios.json"
#: A scripted scenario is deterministic; the seed only pins the simulator's state.
SCENARIO_BOUNDS = Exploration(seed="0x1", max_samples=1, max_steps=40)
#: Local CI explores with this seed and these bounds every time (recorded in the
#: summary and the PR).  A sampled search, never an exhaustive one.
EXPLORATION = Exploration(seed="0x2f9c", max_samples=1500, max_steps=30, n_traces=100)
#: Run summary file inside the replay session directory.
SUMMARY_NAME = "exploration-summary.json"


#: Scenarios whose traces are saved as regression fixtures.  Together they cover
#: every route in ``REQUIRED_ROUTES``; every other scenario is generated fresh.
STORED_SCENARIOS = (
    "final_apply",
    "final_dry",
    "record_dry_then_apply",
    "not_needed_apply",
    "prior_merge_dry",
    "lagged_apply",
    "lagged_dry",
    "recompute_release",
    "no_op_cycles",
    "chain_apply",
    "chain_dry",
    "diamond_apply",
    "dependency_reservation_apply",
    "target_reservation_apply",
    "dependency_reservation_dry",
    "intermediate_dependency_reservation_dry",
    "base_red_apply",
    "failure_after_add",
    "restart_loses_ledger",
    "duplicate_completion",
    "stale_revoke",
    "stale_complete",
    "defect_target_own_reservation_dry",
    "defect_intermediate_own_reservation_dry",
    "defect_prior_merge_after_faults",
)
#: Routes the saved traces must cover (#1276: final / intermediate, apply / dry
#: run, live / lagged, no-op / real change, dependency-side / target-side holds).
REQUIRED_ROUTES = frozenset(
    {
        "apply",
        "dry-run",
        "live",
        "lagged",
        "target-promotion",
        "intermediate-promotion",
        "no-op",
        "real-change",
        "dependency-reservation",
        "target-reservation",
        "base-red-hold",
        "evidence:label",
        "evidence:record_completion",
        "evidence:outcome_not_needed",
        "evidence:prior_merge",
        "restart:ledger-loss",
        "failure:add-after",
        "stale:revoke",
        "stale:complete",
        "duplicate",
    }
)


def scenario_ids() -> tuple[str, ...]:
    """Scenario ids the model offers an ``init_<id>`` entry point for."""
    text = MODEL.read_text(encoding="utf-8")
    return tuple(re.findall(r"^  action init_([a-z0-9_]+):", text, re.M))


def run_scenario(scenario: str, out_dir: Path | None = None) -> ModelRun:
    return run_model(SCENARIO_BOUNDS, init=f"init_{scenario}", out_dir=out_dir)


def generation_command(scenario: str) -> str:
    """The reproducible command line (model path relative to the repository)."""
    return " ".join(
        command_line(
            SCENARIO_BOUNDS,
            init=f"init_{scenario}",
            out_dir=Path("<dir>"),
            model=MODEL.relative_to(ROOT).as_posix(),
            executable="quint",
        )
    )


def load_fixture(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != 1:
        raise ReplayFormatError(f"{path.name}: unsupported fixture format")
    return data  # type: ignore[no-any-return]


def known_defect(raw: Mapping[str, Any] | None) -> KnownDefect | None:
    if raw is None:
        return None
    return KnownDefect(
        issue=int(raw["issue"]),
        contract=str(raw["contract"]),
        step=int(raw["step"]),
        node=int(raw["node"]),
        violates=str(raw["violates"]),
    )


def routes_of(trace: Trace) -> frozenset[str]:
    """Routes a trace exercises, derived from the model states (not production)."""
    routes: set[str] = set()
    held: set[int] = set()
    for step in trace.steps[1:]:
        state = step.state
        if step.action == "complete_dependency":
            routes.add(f"evidence:{step.picks['path']}")
        if step.action == "toggle_hold" and step.picks["kind"] == "reservation":
            target = step.picks["target"]
            held = held ^ {target}
        if step.action == "restart" and step.picks["ledgerLoss"]:
            routes.add("restart:ledger-loss")
        if step.action == "restart" and not step.picks["ledgerLoss"]:
            routes.add("restart:ledger-kept")
        if step.action == "fail_next":
            routes.add(f"failure:{step.picks['op']}-{step.picks['mode']}")
        if step.action == "stale_snapshot":
            routes.add(f"stale:{step.picks['kind']}")
        if step.action == "duplicate_completion":
            routes.add("duplicate")
        if step.action != "cycle":
            continue
        routes.add("apply" if step.picks["apply"] else "dry-run")
        routes.add("lagged" if step.picks["lag"] else "live")
        routes |= _cycle_routes(state, held)
    return frozenset(routes)


def _cycle_routes(state: Mapping[str, Any], held: set[int]) -> set[str]:
    routes: set[str] = set()
    obliged = state["mustQueue"] | state["mustNot"]
    if DEPENDENT in state["mustQueue"]:
        routes.add("target-promotion")
    if state["mustQueue"] - {DEPENDENT}:
        routes.add("intermediate-promotion")
    if state["mustNot"] - {DEPENDENT}:
        routes.add("intermediate-held-back")
    if not state["events"] and not state["newlyQueued"]:
        routes.add("no-op")
    if state["newlyQueued"]:
        routes.add("real-change")
    if held - {DEPENDENT} and state["mustNot"]:
        routes.add("dependency-reservation")
    if DEPENDENT in held and (state["mustNot"] or obliged):
        routes.add("target-reservation")
    if state["base"] and state["mustNot"]:
        routes.add("base-red-hold")
    return routes


REPLAY_DIR_ENV = "ORCHESTUNE_QUINT_REPLAY_DIR"


@lru_cache(maxsize=1)
def node_version() -> str:
    done = subprocess.run(
        ["node", "--version"], capture_output=True, text=True, timeout=60, check=False
    )
    return done.stdout.strip()


def require_traces(directory: Path, expected: int) -> tuple[Path, ...]:
    """The trace files a generation must have produced (never an empty success)."""
    found = (
        tuple(sorted(directory.glob("trace_*.itf.json"))) if directory.is_dir() else ()
    )
    if len(found) != expected:
        raise ReplayFormatError(
            f"{directory}: expected {expected} ITF traces, found {len(found)}"
        )
    for path in found:
        if path.stat().st_size == 0:
            raise ReplayFormatError(f"{path.name} is empty")
    return found


def write_summary(
    target: Path,
    bounds: Exploration,
    run: ModelRun,
    replayed: ReplayReport,
    replay_seconds: float,
) -> dict[str, Any]:
    """The reproducibility record: tool versions, seed, bounds, counts, duration."""
    summary = {
        "quint": pinned_quint_version(),
        "node": node_version(),
        "backend": BACKEND,
        "seed": bounds.seed,
        "max_samples": bounds.max_samples,
        "max_steps": bounds.max_steps,
        "n_traces": bounds.n_traces,
        "explore_seconds": round(run.seconds, 1),
        "traces": len(run.traces),
        "replay_seconds": round(replay_seconds, 1),
        "replayed_transitions": replayed.transitions,
        "replayed_cycles": replayed.cycles,
        "checked_obligations": replayed.obligations,
        "command": " ".join(
            command_line(
                bounds,
                init=None,
                out_dir=Path("<dir>"),
                model=MODEL.relative_to(ROOT).as_posix(),
                executable="quint",
                witnesses=WITNESSES,
            )
        ),
    }
    target.write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    return summary


def regenerate_traces(target: Path = TRACES_FIXTURE) -> int:
    """Rewrite the saved ITF traces from the current model (known defects are kept)."""

    kept: dict[str, Any] = {}
    if target.exists():
        kept = {t["id"]: t.get("known_defect") for t in load_fixture(target)["traces"]}
    traces = []
    with tempfile.TemporaryDirectory() as scratch:
        for scenario in STORED_SCENARIOS:
            run = run_scenario(scenario, Path(scratch) / scenario)
            if run.outcome != "ok" or len(run.traces) != 1:
                raise ToolError(f"scenario {scenario}: {run.outcome} {run.violated}")
            raw = json.loads(run.traces[0].read_text(encoding="utf-8"))
            traces.append(
                {
                    "id": scenario,
                    "seed": SCENARIO_BOUNDS.seed,
                    "bounds": {
                        "max_samples": SCENARIO_BOUNDS.max_samples,
                        "max_steps": SCENARIO_BOUNDS.max_steps,
                        "n_traces": SCENARIO_BOUNDS.n_traces,
                    },
                    "command": generation_command(scenario),
                    "known_defect": kept.get(scenario),
                    "itf": normalized(raw),
                }
            )
    document = {
        "format": 1,
        "quint": pinned_quint_version(),
        "backend": BACKEND,
        "model": MODEL.relative_to(ROOT).as_posix(),
        "regenerate": "uv run python -m tests.quint_scenarios regenerate",
        "traces": traces,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_one_trace_per_line(document), encoding="utf-8")
    return len(traces)


def _one_trace_per_line(document: dict[str, Any]) -> str:
    """Header fields readable, every trace on its own line (small, diffable)."""
    header = {k: v for k, v in document.items() if k != "traces"}
    lines = [json.dumps(header, indent=1, sort_keys=True)[:-2].rstrip()]
    lines.append(',\n "traces": [')
    rows = [
        " " + json.dumps(trace, separators=(",", ":"), sort_keys=True)
        for trace in document["traces"]
    ]
    return "\n".join([lines[0] + lines[1], ",\n".join(rows), " ]", "}"]) + "\n"


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["regenerate"]:
        print(f"{regenerate_traces()} traces written to {TRACES_FIXTURE}")
    else:
        raise SystemExit("usage: python -m tests.quint_scenarios regenerate")
