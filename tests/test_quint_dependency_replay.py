"""Quint dependency-liveness model replayed on the production cycle (#1276).

Three layers, none of which may replace another:

* the model (``specs/quint/dependency_liveness.qnt``) is explored and its own
  properties checked, with controls that inject one model fault each;
* the traces it generates -- fresh from a fixed seed, the saved scenarios, and
  the saved regression fixture -- are replayed on the real harness, where the
  model's obligations are compared with production's observation;
* controls inject one *production* fault each and require the replay to fail with
  the expected contract id.

Quint and Node are required: a missing tool is an error, never a skip.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings

from orchestune.consistency.invariants import status as invariants
from orchestune.dispatch import reconciliation, status_repair
from orchestune.dispatch.rules import CycleContext
from tests.dependency_liveness_test_support import (
    DEPENDENT,
    CompletionPath,
    FaultPlan,
    LivenessTopology,
    LivenessWorld,
    StatusLabel,
    cycle_module,
)
from tests.quint_replay import (
    ACTIONS,
    MODEL,
    ROOT,
    WITNESSES,
    Exploration,
    ModelRun,
    Replayer,
    ReplayFormatError,
    ReplayReport,
    ToolError,
    build_topology,
    normalized,
    parse_trace,
    pinned_quint_version,
    quint_executable,
    replay_trace,
    run_model,
)
from tests.quint_scenarios import (
    EXPLORATION,
    FAULTS_FIXTURE,
    REPLAY_DIR,
    REQUIRED_ROUTES,
    STORED_SCENARIOS,
    SUMMARY_NAME,
    TRACES_FIXTURE,
    generation_command,
    known_defect,
    load_fixture,
    require_traces,
    routes_of,
    run_scenario,
    scenario_ids,
    write_summary,
)
from tests.test_dependency_liveness_stateful import dependency_topologies
from tests.verification_contract_test_support import (
    ContractViolation,
    expect_violation,
    pinned_defect,
    require,
)

STORED = {entry["id"]: entry for entry in load_fixture(TRACES_FIXTURE)["traces"]}
FAULTS = load_fixture(FAULTS_FIXTURE)["faults"]
NON_FAULT_SCENARIOS = tuple(
    name for name in scenario_ids() if not name.startswith("fault_")
)


def assert_model_run_ok(run: ModelRun, label: str) -> None:
    """The model's own properties hold.  Only a property violation is a
    ``P3C-QUINT-MODEL`` violation; parse / type / runtime errors and timeouts are
    tool errors and can never count as a detection."""
    if run.outcome == "error":
        raise ToolError(
            f"{label}: quint failed ({run.returncode}): {run.stdout[-400:]}"
        )
    require(
        "P3C-QUINT-MODEL",
        run.outcome == "ok",
        f"{label} violated {', '.join(run.violated)}. Reproduce: "
        f"{' '.join(run.command)} -- counterexample ITF: "
        f"{', '.join(str(path) for path in run.traces) or 'not written'}",
    )


def replay_stored(scenario: str, root: Path, wiring: FaultPlan | None = None) -> None:
    entry = STORED[scenario]
    replay_trace(
        parse_trace(entry["itf"]), root, known_defect(entry["known_defect"]), wiring
    )


# ---- toolchain ------------------------------------------------------------------------


def test_quint_is_pinned_exactly_and_locked() -> None:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    version = pinned_quint_version()
    assert package["engines"]["node"] == ">=24 <25"
    locked = lock["packages"]["node_modules/@informalsystems/quint"]
    assert locked["version"] == version
    assert "integrity" in locked
    assert quint_executable().exists()  # a missing / wrong tool raises, never skips


def test_ci_and_scripts_install_the_pinned_node() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "actions/setup-node" in workflow and "node-version: '24'" in workflow
    for script in ("local-ci.sh", "local-ci.ps1"):
        text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
        assert "quint-check" in text, f"{script} does not run the Quint check"
    for script in ("quint-check.sh", "quint-check.ps1"):
        assert (ROOT / "scripts" / script).is_file()


def test_the_quint_check_fails_without_node(tmp_path: Path) -> None:
    """No skip: without Node the check stops with exit 2 and the install steps."""
    bare = tmp_path / "bin"
    bare.mkdir()
    if os.name == "nt":
        shell = shutil.which("powershell") or "powershell"
        command = [
            shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "quint-check.ps1"),
        ]
        env = {**os.environ, "PATH": str(bare)}
    else:
        command = ["/bin/bash", str(ROOT / "scripts" / "quint-check.sh")]
        env = {"PATH": str(bare), "HOME": str(tmp_path)}
    done = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 2, done.stdout + done.stderr
    assert "Node.js" in done.stderr and "CONTRIBUTING" in done.stderr


# ---- bounded exploration, replayed on production ---------------------------------------


EXPECTED_TOPOLOGIES = {
    "simple_one": LivenessTopology((11,), ("dep-a",), {}, (11,)),
    "simple_two": LivenessTopology((11, 12), ("dep-a", "dep-b"), {}, (11, 12)),
    "fan_in_three": LivenessTopology(
        (11, 12, 13), ("dep-a", "dep-b", "dep-c"), {}, (11, 12, 13)
    ),
    "fan_in_four": LivenessTopology(
        (11, 12, 13, 14),
        ("dep-a", "dep-b", "dep-c", "dep-d"),
        {},
        (11, 12, 13, 14),
    ),
    "transitive_chain": LivenessTopology(
        (11, 12, 13), ("dep-a",), {11: ("dep-b",), 12: ("dep-c",)}, (11,)
    ),
    "branching_diamond": LivenessTopology(
        (11, 12, 13),
        ("dep-a", "dep-b"),
        {11: ("dep-c",), 12: ("dep-c",)},
        (11, 12),
    ),
    "intermediate_cycle": LivenessTopology(
        (11, 12), ("dep-a",), {11: ("dep-b",), 12: ("dep-a",)}, (11,)
    ),
    "dependent_cycle": LivenessTopology((11,), ("dep-a",), {11: ("dependent",)}, (11,)),
    "unresolved_direct": LivenessTopology(
        (11,), ("dep-a", "unresolved-missing"), {}, None
    ),
    "unresolved_transitive": LivenessTopology(
        (11,), ("dep-a",), {11: ("unresolved-missing",)}, (11,)
    ),
}


@pytest.fixture
def replay_dir(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    """The session directory the local CI created, else a pytest temp directory."""
    if not REPLAY_DIR:
        return tmp_path
    target = Path(REPLAY_DIR) / str(request.node.name)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _shape_key(topology: LivenessTopology) -> tuple[Any, ...]:
    return (
        topology.dep_issues,
        topology.t_depends_on,
        tuple(sorted(topology.issue_depends_on.items())),
        topology.required_to_promote,
    )


def test_bounded_exploration_replays_on_production(replay_dir: Path) -> None:
    """Explore with the fixed seed, then replay every generated trace for real."""
    traces_dir = replay_dir / "traces"
    run = run_model(EXPLORATION, out_dir=traces_dir, witnesses=WITNESSES)
    assert_model_run_ok(run, "bounded exploration")
    paths = require_traces(traces_dir, EXPLORATION.n_traces)
    assert "Witnesses" in run.stdout
    totals = ReplayReport()
    seen: dict[str, tuple[Any, ...]] = {}
    started = time.monotonic()
    for index, path in enumerate(paths):
        trace = parse_trace(json.loads(path.read_text(encoding="utf-8")))
        first = trace.steps[0].state
        seen[first["topo"]] = _shape_key(build_topology(first["shape"]))
        world = replay_dir / f"world-{index}"
        report = replay_trace(trace, world)
        shutil.rmtree(world, ignore_errors=True)
        assert report.transitions == trace.transitions > 0, path.name
        totals.transitions += report.transitions
        totals.cycles += report.cycles
        totals.obligations += report.obligations
    assert totals.cycles > 0 and totals.obligations > 0
    expected = {k: _shape_key(v) for k, v in EXPECTED_TOPOLOGIES.items()}
    assert seen == {k: expected[k] for k in seen}
    write_summary(
        replay_dir / SUMMARY_NAME, EXPLORATION, run, totals, time.monotonic() - started
    )


def test_every_hypothesis_topology_is_in_the_replay_table() -> None:
    """The model's topologies are exactly the ones the Hypothesis machine draws."""
    drawn: set[tuple[Any, ...]] = set()

    @settings(derandomize=True, max_examples=300, database=None, deadline=None)
    @given(dependency_topologies())
    def collect(topology: LivenessTopology) -> None:
        drawn.add(_shape_key(topology))

    collect()
    assert drawn == {_shape_key(t) for t in EXPECTED_TOPOLOGIES.values()}


# ---- scripted scenarios -----------------------------------------------------------------


@pytest.mark.parametrize("scenario", NON_FAULT_SCENARIOS)
def test_each_scenario_runs_on_the_model_and_replays(
    scenario: str, tmp_path: Path
) -> None:
    """Generate the scenario fresh, check it against the saved fixture, replay it.

    A model change without regenerating the fixture fails here.  The defect
    scenarios are replayed by their strict-xfail tests below, not here.
    """
    run = run_scenario(scenario, tmp_path / "model")
    assert_model_run_ok(run, scenario)
    (path,) = run.traces
    raw = json.loads(path.read_text(encoding="utf-8"))
    if scenario in STORED:
        entry = STORED[scenario]
        assert entry["itf"] == normalized(
            raw
        ), "run: uv run python -m tests.quint_scenarios regenerate"
        assert entry["command"] == generation_command(scenario)
        assert entry["seed"] == "0x1" and entry["bounds"]["max_samples"] == 1
    if scenario.startswith("defect_"):
        return
    trace = parse_trace(raw)
    report = replay_trace(trace, tmp_path / "world")
    assert report.transitions == trace.transitions > 0


# ---- the saved regression traces -----------------------------------------------------------


def test_stored_traces_are_the_declared_scenarios_of_the_pinned_tool() -> None:
    document = load_fixture(TRACES_FIXTURE)
    assert tuple(STORED) == STORED_SCENARIOS
    assert document["quint"] == pinned_quint_version()
    assert document["backend"] == "typescript"
    assert document["model"] == MODEL.relative_to(ROOT).as_posix()
    for entry in STORED.values():
        for flag in ("--mbt", "--out-itf=", "--n-traces=", "--backend=typescript"):
            assert flag in entry["command"], (entry["id"], flag)


def test_stored_traces_cover_the_required_routes() -> None:
    covered: set[str] = set()
    for scenario in STORED_SCENARIOS:
        covered |= routes_of(parse_trace(STORED[scenario]["itf"]))
    assert REQUIRED_ROUTES <= covered, sorted(REQUIRED_ROUTES - covered)


def test_stored_traces_exercise_every_mapped_action() -> None:
    used: set[str] = set()
    for scenario in STORED_SCENARIOS:
        used |= {step.action for step in parse_trace(STORED[scenario]["itf"]).steps[1:]}
    assert used == set(ACTIONS)


@pytest.mark.parametrize(
    "scenario", [s for s in STORED_SCENARIOS if not s.startswith("defect_")]
)
def test_stored_trace_replays_on_production(scenario: str, tmp_path: Path) -> None:
    entry = STORED[scenario]
    assert entry["known_defect"] is None
    trace = parse_trace(entry["itf"])
    report = replay_trace(trace, tmp_path)
    assert report.transitions == trace.transitions > 0
    assert report.cycles > 0


def test_known_defect_registry_names_known_defect_rows() -> None:
    from tests.verification_contracts import CONTRACTS

    rows = {c.id: c for c in CONTRACTS if c.status == "known_defect"}
    registered = {
        s: known_defect(STORED[s]["known_defect"])
        for s in STORED_SCENARIOS
        if s.startswith("defect_")
    }
    assert all(registered.values())
    for scenario, defect in registered.items():
        assert defect is not None
        assert defect.contract in rows, scenario
        assert defect.issue in rows[defect.contract].issues, scenario
    assert {s for s, e in STORED.items() if e["known_defect"]} == set(registered)


@pytest.mark.xfail(
    reason="production bug #1283: a dry run previews T under its own reservation",
    strict=True,
    raises=ContractViolation,
)
def test_known_defect_stored_trace_target_own_reservation(tmp_path: Path) -> None:
    with pinned_defect("P3C-DRYRUN-RESERVATION"):
        replay_stored("defect_target_own_reservation_dry", tmp_path)


@pytest.mark.xfail(
    reason="production bug #1281: a dry run previews an intermediate under its own "
    "reservation",
    strict=True,
    raises=ContractViolation,
)
def test_known_defect_stored_trace_intermediate_own_reservation(tmp_path: Path) -> None:
    with pinned_defect("P3C-DRYRUN-RESERVATION"):
        replay_stored("defect_intermediate_own_reservation_dry", tmp_path)


@pytest.mark.xfail(
    reason="production bug #1281: no preview of a prior merge after faulted apply "
    "cycles",
    strict=True,
    raises=ContractViolation,
)
def test_known_defect_stored_trace_prior_merge_after_faults(tmp_path: Path) -> None:
    with pinned_defect("P3C-LIVENESS-PRIOR-MERGE-DRYRUN"):
        replay_stored("defect_prior_merge_after_faults", tmp_path)


def test_a_mismatch_beside_a_known_defect_is_not_excused(tmp_path: Path) -> None:
    """The registered step / node / contract are exact: any other mismatch fails."""
    entry = copy.deepcopy(STORED["defect_target_own_reservation_dry"])
    entry["known_defect"]["step"] += 1  # the defect is no longer where it is pinned
    with pytest.raises(ContractViolation) as caught:
        replay_trace(
            parse_trace(entry["itf"]), tmp_path, known_defect(entry["known_defect"])
        )
    assert caught.value.contract_id == "P3C-SAFETY"


# ---- model controls: one fault in the model each --------------------------------------------


@pytest.mark.parametrize("entry", FAULTS, ids=lambda entry: entry["fault"])
def test_control_model_fault_is_detected_by_its_invariant(
    entry: dict[str, Any], tmp_path: Path
) -> None:
    ok = run_scenario(entry["ok_scenario"], tmp_path / "ok")
    assert_model_run_ok(ok, entry["ok_scenario"])
    ok_trace = json.loads(ok.traces[0].read_text(encoding="utf-8"))
    assert len(ok_trace["states"]) == entry["ok_states"]  # reached the planned end
    bad = run_scenario(entry["fault_scenario"], tmp_path / "bad")
    with expect_violation("P3C-QUINT-MODEL"):
        assert_model_run_ok(bad, entry["fault_scenario"])
    assert set(bad.violated) == set(entry["expect_violated"])
    bad_trace = json.loads(bad.traces[0].read_text(encoding="utf-8"))
    assert bad_trace["#meta"]["status"] == "violation"
    assert len(bad_trace["states"]) == entry["violation_states"]


def test_a_model_violation_reports_a_reproducible_counterexample(
    tmp_path: Path,
) -> None:
    """The failure names the invariant, the exact command and the saved trace."""
    run = run_scenario("fault_suppress", tmp_path)
    with pytest.raises(ContractViolation) as caught:
        assert_model_run_ok(run, "fault_suppress")
    detail = caught.value.detail
    assert "liveness" in detail
    assert "--seed=0x1" in detail and "--backend=typescript" in detail
    assert "--init=init_fault_suppress" in detail
    (trace,) = run.traces
    assert str(trace) in detail and trace.is_file()
    assert (
        json.loads(trace.read_text(encoding="utf-8"))["#meta"]["status"] == "violation"
    )


def test_every_model_fault_scenario_has_a_control() -> None:
    declared = {name for name in scenario_ids() if name.startswith("fault_")}
    assert declared == {entry["fault_scenario"] for entry in FAULTS}
    assert {entry["fault"] for entry in FAULTS} == {
        "promotion-suppressed",
        "promotion-delayed",
        "stale-evidence",
        "intermediate-ignored",
        "reservation-guard-bypass",
        "hold-guard-bypass",
        "event-only",
        "dry-run-writes",
    }


def test_a_broken_model_is_an_error_not_a_detection(tmp_path: Path) -> None:
    broken = tmp_path / "broken.qnt"
    broken.write_text("module broken { val x = ", encoding="utf-8")
    run = run_model(Exploration("0x1", 1, 1), model=broken)
    assert run.outcome == "error"
    with pytest.raises(ToolError), expect_violation("P3C-QUINT-MODEL"):
        assert_model_run_ok(run, "broken")


def test_a_timeout_is_an_error_not_a_success() -> None:
    with pytest.raises(ToolError, match="timed out"):
        run_model(Exploration("0x1", 10**9, 10**6, timeout=2.0))


def test_a_violation_with_the_wrong_invariant_does_not_match_the_fault() -> None:
    entry = next(e for e in FAULTS if e["fault"] == "stale-evidence")
    run = run_scenario(entry["fault_scenario"])
    assert run.outcome == "violation" and run.violated == ("safety",)
    assert set(run.violated) != {"liveness"}


# ---- replay controls: one fault in production each --------------------------------------------


def _skip_promotion(
    monkeypatch: pytest.MonkeyPatch, calls: frozenset[int] | None
) -> None:
    """Skip the promotion boundary in the n-th cycle (``None``: in every cycle)."""
    real = cycle_module._run_status_repair_boundary
    seen = {"n": -1}

    def boundary(name: str, finding_code: str, **kwargs: Any) -> Any:
        if name == "status-blocked-promotion":
            seen["n"] += 1
            if calls is None or seen["n"] in calls:
                return []
        return real(name, finding_code, **kwargs)

    monkeypatch.setattr(cycle_module, "_run_status_repair_boundary", boundary)


def test_control_replay_detects_suppressed_promotion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    replay_stored("final_apply", tmp_path / "normal")
    _skip_promotion(monkeypatch, None)
    with expect_violation("P3C-LIVENESS-BOUND"):
        replay_stored("final_apply", tmp_path / "fault")


def test_control_replay_detects_delayed_promotion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    replay_stored("final_apply", tmp_path / "normal")
    _skip_promotion(monkeypatch, frozenset({0}))
    with expect_violation("P3C-LIVENESS-BOUND"):
        replay_stored("final_apply", tmp_path / "fault")


def test_control_replay_detects_ignored_intermediate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    replay_stored("chain_apply", tmp_path / "normal")
    real = CycleContext.assess_dependencies

    def only_the_target(self: CycleContext, number: int) -> Any:
        return real(self, number) if number == DEPENDENT else None

    monkeypatch.setattr(CycleContext, "assess_dependencies", only_the_target)
    with expect_violation("P3C-INTERMEDIATE-LIVENESS"):
        replay_stored("chain_apply", tmp_path / "fault")


def test_control_replay_detects_stale_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    replay_stored("stale_revoke", tmp_path / "normal")
    monkeypatch.setattr(CycleContext, "is_effectively_done", lambda s, n: True)
    monkeypatch.setattr(CycleContext, "is_completion_confirmed", lambda s, n: True)
    with expect_violation("P3C-SAFETY"):
        replay_stored("stale_revoke", tmp_path / "fault")


def test_control_replay_detects_a_reservation_guard_bypass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    replay_stored("dependency_reservation_apply", tmp_path / "normal")
    monkeypatch.setattr(CycleContext, "is_completion_blocked", lambda s, n: False)
    with expect_violation("P3C-SAFETY"):
        replay_stored("dependency_reservation_apply", tmp_path / "fault")


def test_control_replay_detects_a_hold_guard_bypass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    replay_stored("base_red_apply", tmp_path / "normal")
    for module in (status_repair, invariants, reconciliation):
        monkeypatch.setattr(module, "PROMOTION_HOLD_LABELS", ())
    with expect_violation("P3C-SAFETY"):
        replay_stored("base_red_apply", tmp_path / "fault")


def test_control_replay_detects_an_event_without_the_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    replay_stored("final_apply", tmp_path / "normal")
    monkeypatch.setattr(status_repair, "_apply_command", lambda *a, **k: None)
    monkeypatch.setattr(
        status_repair, "_verified_status_labels", lambda number, label, config: (label,)
    )
    with expect_violation("P3C-LIVENESS-BOUND"):
        replay_stored("final_apply", tmp_path / "fault")


def test_control_replay_detects_an_apply_event_without_the_label_against_an_obligation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A held-back T whose promotion event appears although no label changed (#1295)."""
    replay_stored("base_red_apply", tmp_path / "normal")
    for module in (status_repair, invariants, reconciliation):
        monkeypatch.setattr(module, "PROMOTION_HOLD_LABELS", ())
    monkeypatch.setattr(status_repair, "_apply_command", lambda *a, **k: None)
    monkeypatch.setattr(
        status_repair, "_verified_status_labels", lambda number, label, config: (label,)
    )
    with expect_violation("P3C-SAFETY"):
        replay_stored("base_red_apply", tmp_path / "fault")


def test_control_replay_detects_a_dry_run_that_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A "dry run" that really promotes: the label is queued, the preview is shown."""
    replay_stored("final_dry", tmp_path / "normal")
    real = LivenessWorld.config
    monkeypatch.setattr(
        LivenessWorld, "config", lambda self, *, apply: real(self, apply=True)
    )
    with expect_violation("P3C-DRYRUN-READONLY"):
        replay_stored("final_dry", tmp_path / "fault")


def test_control_replay_detects_a_dry_run_write_beside_a_hold_only_stale_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``add_hold`` changes only a hold label, so T's lifecycle labels stay checked."""
    replay_stored("stale_add_hold_dry", tmp_path / "normal")
    real = LivenessWorld.config
    monkeypatch.setattr(
        LivenessWorld, "config", lambda self, *, apply: real(self, apply=True)
    )
    for module in (status_repair, invariants, reconciliation):
        monkeypatch.setattr(module, "PROMOTION_HOLD_LABELS", ())
    with expect_violation("P3C-DRYRUN-READONLY"):
        replay_stored("stale_add_hold_dry", tmp_path / "fault")


def test_the_converse_of_events_are_label_changes_is_not_a_contract_yet(
    tmp_path: Path,
) -> None:
    """Under listing lag production promotes an intermediate node without an event.

    This records #1296: the model claims "every apply event is a label change" and
    the replay checks exactly that.  The converse (every promotion has an event) is
    not claimed because production does not provide it.  When #1296 is fixed this
    test fails: make ``_require_events_are_label_changes`` bidirectional (and the
    model's ``eventsAreLabelChanges`` an equality) instead of deleting it.
    """
    topology = EXPECTED_TOPOLOGIES["transitive_chain"]
    for lag, expected_events in ((False, (12,)), (True, ())):
        world = LivenessWorld(tmp_path / f"lag-{lag}", topology=topology)
        world.complete(13, CompletionPath.LABEL)
        world.faults.listing_lag = lag
        observation = world.cycle(apply=True)
        assert StatusLabel.QUEUED.value not in observation.labels_before[12]
        assert StatusLabel.QUEUED.value in observation.labels_after[12]
        assert (
            observation.promotion_issue_numbers == expected_events
        ), "#1296 changed: see the docstring"


def test_control_replay_detects_an_empty_completion_set(tmp_path: Path) -> None:
    replay_stored("recompute_release", tmp_path / "normal")
    with expect_violation("P3C-LIVENESS-BOUND"):
        replay_stored(
            "recompute_release",
            tmp_path / "fault",
            FaultPlan(empty_completion_set=True),
        )


def test_control_replay_detects_a_throwaway_repair_context(tmp_path: Path) -> None:
    replay_stored("lagged_apply", tmp_path / "normal")
    with expect_violation("P3C-LIVENESS-BOUND"):
        replay_stored(
            "lagged_apply", tmp_path / "fault", FaultPlan(throwaway_repair_context=True)
        )


# ---- mapping regressions ---------------------------------------------------------------------


def test_control_a_swapped_action_mapping_fails_the_replay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    replay_stored("duplicate_completion", tmp_path / "normal")
    monkeypatch.setattr(
        Replayer, "_do_complete_dependency", Replayer._do_duplicate_completion
    )
    with expect_violation("P3C-QUINT-OBSERVATION"):
        replay_stored("duplicate_completion", tmp_path / "swapped")


def test_control_a_dropped_restart_mapping_fails_the_replay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    replay_stored("restart_loses_ledger", tmp_path / "normal")
    monkeypatch.setattr(Replayer, "_do_restart", lambda self, step, previous: None)
    with expect_violation("P3C-SAFETY"):
        replay_stored("restart_loses_ledger", tmp_path / "dropped")


def _mutated(scenario: str, change: Any) -> dict[str, Any]:
    itf = copy.deepcopy(STORED[scenario]["itf"])
    change(itf)
    return itf  # type: ignore[no-any-return]


def _action(itf: dict[str, Any], index: int, name: str) -> None:
    itf["states"][index]["mbt::actionTaken"] = name


def _pick(itf: dict[str, Any], index: int, key: str, value: Any) -> None:
    itf["states"][index]["mbt::nondetPicks"][key] = {"tag": "Some", "value": value}


def _drop_pick(itf: dict[str, Any], index: int, key: str) -> None:
    itf["states"][index]["mbt::nondetPicks"][key] = {
        "tag": "None",
        "value": {"#tup": []},
    }


MALFORMED = {
    "unsupported action": lambda itf: _action(itf, 1, "complete_everything"),
    "init after the start": lambda itf: _action(itf, 2, "init"),
    "missing pick": lambda itf: _drop_pick(itf, 1, "dep"),
    "extra pick": lambda itf: _pick(itf, 1, "ledgerLoss", True),
    "unsupported value": lambda itf: _pick(itf, 1, "path", "carrier-pigeon"),
    "wrong type": lambda itf: _pick(itf, 2, "apply", {"#bigint": "1"}),
    "unknown dependency": lambda itf: _pick(itf, 1, "dep", {"#bigint": "99"}),
    "unserializable value": lambda itf: itf["states"][1].update(
        {"res": {"#unserializable": "x"}}
    ),
    "violation trace": lambda itf: itf["#meta"].update({"status": "violation"}),
    "not ITF": lambda itf: itf["#meta"].update({"format": "JSON"}),
    "only the initial state": lambda itf: itf.update({"states": itf["states"][:1]}),
    "truncated trace": lambda itf: itf.update({"states": itf["states"][:-1]}),
    "reordered states": lambda itf: itf["states"].reverse(),
    "no variables": lambda itf: itf.update({"vars": []}),
    "empty": lambda itf: itf.update({"states": []}),
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_a_malformed_or_unsupported_trace_fails_explicitly(
    name: str, tmp_path: Path
) -> None:
    itf = _mutated("final_apply", MALFORMED[name])
    with pytest.raises(ReplayFormatError):
        replay_trace(parse_trace(itf), tmp_path)


def test_a_trace_directory_without_enough_traces_is_not_a_success(
    tmp_path: Path,
) -> None:
    with pytest.raises(ReplayFormatError):
        require_traces(tmp_path / "missing", 1)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ReplayFormatError):
        require_traces(empty, 1)
    (empty / "trace_0.itf.json").write_text("", encoding="utf-8")
    with pytest.raises(ReplayFormatError, match="empty"):
        require_traces(empty, 1)
    (empty / "trace_0.itf.json").write_text("{}", encoding="utf-8")
    assert require_traces(empty, 1) == (empty / "trace_0.itf.json",)
    with pytest.raises(ReplayFormatError):
        require_traces(empty, 2)
