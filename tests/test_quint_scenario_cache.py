"""Tests for Quint scenario run cache and stored trace cache support (#1367)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from tests.quint_replay import (
    MODEL,
    ModelRun,
    ReplayFormatError,
    ToolError,
    Trace,
    classify_run,
    command_line,
    normalized,
    quint_executable,
)
from tests.quint_scenario_cache_support import (
    ScenarioRunCache,
    StoredTraceCache,
    _batch_prewarm_scenarios,
    _persist,
)
from tests.quint_scenarios import SCENARIO_BOUNDS, run_scenario


def _fake_ok_runner(out_dir: Path, scenario: str) -> ModelRun:
    out_dir.mkdir(parents=True, exist_ok=True)
    itf = out_dir / "trace_0.itf.json"
    itf.write_text('{"#meta": {"status": "ok"}, "states": []}', encoding="utf-8")
    return ModelRun(
        command=("quint", "run", f"--init=init_{scenario}"),
        returncode=0,
        stdout="fake stdout",
        stderr="",
        outcome="ok",
        violated=(),
        seconds=1.23,
        traces=(itf,),
    )


def _fake_violation_runner(out_dir: Path, scenario: str) -> ModelRun:
    out_dir.mkdir(parents=True, exist_ok=True)
    itf = out_dir / "trace_0.itf.json"
    itf.write_text('{"#meta": {"status": "violation"}, "states": []}', encoding="utf-8")
    return ModelRun(
        command=("quint", "run", f"--init=init_{scenario}"),
        returncode=1,
        stdout="fake violation stdout",
        stderr="",
        outcome="violation",
        violated=("safety",),
        seconds=0.45,
        traces=(itf,),
    )


@pytest.fixture
def mock_project_dir(tmp_path: Path) -> Path:
    (tmp_path / ".gitignore").write_text(".orchestune/tmp/\n", encoding="utf-8")
    return tmp_path


# ---- ScenarioRunCache tests --------------------------------------------------


def test_scenario_run_cache_cold_and_hit(mock_project_dir: Path) -> None:
    calls: list[str] = []

    def runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        assert out_dir is not None
        calls.append(scenario)
        return _fake_ok_runner(out_dir, scenario)

    cache = ScenarioRunCache(
        known_scenarios=("final_apply", "final_dry"),
        runner=runner,
        project_dir=mock_project_dir,
    )
    try:
        # First call (cold)
        run1 = cache.get("final_apply")
        assert len(calls) == 1
        assert run1.outcome == "ok"
        assert run1.seconds == 1.23
        assert run1.traces[0].is_file()

        # Second call (hit)
        run2 = cache.get("final_apply")
        assert len(calls) == 1  # Runner not invoked again
        assert run2.outcome == "ok"
        assert run2.seconds == 1.23
        assert run2.traces[0] == run1.traces[0]
        # Defensively copied dataclass instance
        assert run2 is not run1
        assert run2 == run1

        # Callable interface cache(scenario)
        run3 = cache("final_apply")
        assert len(calls) == 1
        assert run3 is not run1
        assert run3 == run1

        # Different scenario
        run_dry = cache.get("final_dry")
        assert len(calls) == 2
        assert run_dry.outcome == "ok"
    finally:
        cache.close()


def test_scenario_run_cache_unknown_scenario_rejected(mock_project_dir: Path) -> None:
    cache = ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=lambda s, out_dir=None: _fake_ok_runner(out_dir or mock_project_dir, s),
        project_dir=mock_project_dir,
    )
    try:
        with pytest.raises(KeyError, match="unknown scenario"):
            cache.get("unknown_foo")
        with pytest.raises(KeyError, match="unknown scenario"):
            cache.get("../traversal")
    finally:
        cache.close()


def test_scenario_run_cache_error_not_cached(mock_project_dir: Path) -> None:
    calls: list[str] = []

    def failing_runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        calls.append(scenario)
        if len(calls) == 1:
            assert out_dir is not None
            out_dir.mkdir(parents=True, exist_ok=True)
            return ModelRun(
                command=("quint", "run"),
                returncode=2,
                stdout="error text",
                stderr="",
                outcome="error",
                violated=(),
                seconds=0.1,
                traces=(),
            )
        assert out_dir is not None
        return _fake_ok_runner(out_dir, scenario)

    cache = ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=failing_runner,
        project_dir=mock_project_dir,
    )
    try:
        with pytest.raises(ToolError):
            cache.get("final_apply")
        assert len(calls) == 1

        # Next call should retry and not serve a broken cache entry
        run = cache.get("final_apply")
        assert len(calls) == 2
        assert run.outcome == "ok"
    finally:
        cache.close()


def test_scenario_run_cache_missing_itf_not_cached(mock_project_dir: Path) -> None:
    calls: list[str] = []

    def missing_itf_runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        calls.append(scenario)
        assert out_dir is not None
        out_dir.mkdir(parents=True, exist_ok=True)
        # Return traces pointing to non-existent file
        return ModelRun(
            command=("quint", "run"),
            returncode=0,
            stdout="",
            stderr="",
            outcome="ok",
            violated=(),
            seconds=0.1,
            traces=(out_dir / "missing.itf.json",),
        )

    cache = ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=missing_itf_runner,
        project_dir=mock_project_dir,
    )
    try:
        with pytest.raises(ReplayFormatError):
            cache.get("final_apply")
        assert len(calls) == 1
    finally:
        cache.close()


def test_scenario_run_cache_empty_itf_not_cached(mock_project_dir: Path) -> None:
    calls: list[str] = []

    def empty_itf_runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        calls.append(scenario)
        assert out_dir is not None
        out_dir.mkdir(parents=True, exist_ok=True)
        empty_itf = out_dir / "trace_0.itf.json"
        empty_itf.write_text("", encoding="utf-8")
        return ModelRun(
            command=("quint", "run"),
            returncode=0,
            stdout="",
            stderr="",
            outcome="ok",
            violated=(),
            seconds=0.1,
            traces=(empty_itf,),
        )

    cache = ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=empty_itf_runner,
        project_dir=mock_project_dir,
    )
    try:
        with pytest.raises(ReplayFormatError, match="empty"):
            cache.get("final_apply")
        assert len(calls) == 1
    finally:
        cache.close()


def test_scenario_run_cache_violation_cached(mock_project_dir: Path) -> None:
    calls: list[str] = []

    def runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        assert out_dir is not None
        calls.append(scenario)
        return _fake_violation_runner(out_dir, scenario)

    cache = ScenarioRunCache(
        known_scenarios=("fault_suppress",),
        runner=runner,
        project_dir=mock_project_dir,
    )
    try:
        run1 = cache.get("fault_suppress")
        assert len(calls) == 1
        assert run1.outcome == "violation"
        assert run1.violated == ("safety",)

        run2 = cache.get("fault_suppress")
        assert len(calls) == 1
        assert run2.outcome == "violation"
        assert run2 is not run1
    finally:
        cache.close()


def test_scenario_run_cache_cleanup_removes_dir(mock_project_dir: Path) -> None:
    cache = ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=lambda s, out_dir=None: _fake_ok_runner(out_dir or mock_project_dir, s),
        project_dir=mock_project_dir,
    )
    session_dir = cache.session_dir
    assert session_dir.is_dir()
    cache.get("final_apply")
    assert (session_dir / "final_apply" / "trace_0.itf.json").is_file()

    cache.close()
    assert not session_dir.exists()


def test_scenario_run_cache_explicit_session_dir_and_context_manager(
    tmp_path: Path,
) -> None:
    custom_dir = tmp_path / "custom_session"
    calls: list[str] = []

    def runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        assert out_dir is not None
        calls.append(scenario)
        return _fake_ok_runner(out_dir, scenario)

    with ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=runner,
        session_dir=custom_dir,
    ) as cache:
        assert cache.session_dir == custom_dir.resolve()
        run = cache("final_apply")
        assert run.outcome == "ok"
        assert len(calls) == 1

    # Caller-supplied session directory is preserved on exit
    assert custom_dir.exists()


def test_scenario_run_cache_unexpected_outcome(tmp_path: Path) -> None:
    def weird_runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        assert out_dir is not None
        out_dir.mkdir(parents=True, exist_ok=True)
        return ModelRun(
            command=("quint", "run"),
            returncode=0,
            stdout="",
            stderr="",
            outcome="weird_status",
            violated=(),
            seconds=0.1,
            traces=(),
        )

    with ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=weird_runner,
        session_dir=tmp_path / "session",
    ) as cache:
        with pytest.raises(ToolError, match="unexpected model outcome"):
            cache.get("final_apply")


def test_scenario_run_cache_unexpected_trace_count(tmp_path: Path) -> None:
    def multi_trace_runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        assert out_dir is not None
        out_dir.mkdir(parents=True, exist_ok=True)
        itf1 = out_dir / "trace_0.itf.json"
        itf2 = out_dir / "trace_1.itf.json"
        itf1.write_text("{}", encoding="utf-8")
        itf2.write_text("{}", encoding="utf-8")
        return ModelRun(
            command=("quint", "run"),
            returncode=0,
            stdout="",
            stderr="",
            outcome="ok",
            violated=(),
            seconds=0.1,
            traces=(itf1, itf2),
        )

    with ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=multi_trace_runner,
        session_dir=tmp_path / "session",
    ) as cache:
        with pytest.raises(ReplayFormatError, match="expected 1 ITF trace"):
            cache.get("final_apply")


def test_scenario_run_cache_cross_instance_disk_hit(tmp_path: Path) -> None:
    shared_dir = tmp_path / "shared_cache"
    calls: list[str] = []

    def runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        assert out_dir is not None
        calls.append(scenario)
        return _fake_ok_runner(out_dir, scenario)

    # First instance runs cold and writes to disk cache
    with ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=runner,
        session_dir=shared_dir,
    ) as cache1:
        run1 = cache1.get("final_apply")
        assert len(calls) == 1
        assert run1.outcome == "ok"
        meta_file = shared_dir / "final_apply" / ".model_run.json"
        assert meta_file.is_file()

    # Second instance shares session_dir but has empty memory cache
    with ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=runner,
        session_dir=shared_dir,
    ) as cache2:
        run2 = cache2.get("final_apply")
        # Runner was NOT called again; served from disk cache
        assert len(calls) == 1
        assert run2.outcome == "ok"
        assert run2.returncode == run1.returncode
        assert run2.command == run1.command
        assert run2.seconds == run1.seconds
        assert run2.traces[0] == run1.traces[0]
        assert run2.traces[0].is_file()


def test_scenario_run_cache_lock_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestune.infra.process_utils import FileLockContentionError

    def contended_lock(*args: Any, **kwargs: Any) -> Any:
        raise FileLockContentionError("contended")

    monkeypatch.setattr("tests.quint_scenario_cache_support.file_lock", contended_lock)

    with ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=lambda s, out_dir=None: _fake_ok_runner(out_dir or tmp_path, s),
        session_dir=tmp_path / "session",
    ) as cache:
        with pytest.raises(ToolError, match="lock contention timed out"):
            cache.get("final_apply")


def test_scenario_run_cache_corrupt_disk_metadata_ignored_and_recomputed(
    tmp_path: Path,
) -> None:
    shared_dir = tmp_path / "corrupt_cache"
    scenario_dir = shared_dir / "final_apply"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    # Write invalid json
    (scenario_dir / ".model_run.json").write_text("{corrupt-json", encoding="utf-8")

    calls: list[str] = []

    def runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        assert out_dir is not None
        calls.append(scenario)
        return _fake_ok_runner(out_dir, scenario)

    with ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=runner,
        session_dir=shared_dir,
    ) as cache:
        run = cache.get("final_apply")
        assert len(calls) == 1
        assert run.outcome == "ok"
        # .model_run.json was rewritten with valid metadata
        assert (scenario_dir / ".model_run.json").is_file()


def test_scenario_run_cache_stale_traces_cleaned_before_runner(tmp_path: Path) -> None:
    shared_dir = tmp_path / "stale_cache"
    scenario_dir = shared_dir / "final_apply"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    stale_trace = scenario_dir / "trace_stale.itf.json"
    stale_trace.write_text("{}", encoding="utf-8")

    def runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        assert out_dir is not None
        # Verify stale trace was deleted before runner execution
        assert not stale_trace.exists()
        return _fake_ok_runner(out_dir, scenario)

    with ScenarioRunCache(
        known_scenarios=("final_apply",),
        runner=runner,
        session_dir=shared_dir,
    ) as cache:
        run = cache.get("final_apply")
        assert run.outcome == "ok"
        assert not stale_trace.exists()


def test_model_scenario_fixture_with_and_without_replay_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_quint_dependency_replay import model_scenario

    fixture_fn = cast(Any, model_scenario).__wrapped__

    # Without REPLAY_DIR
    monkeypatch.setattr("tests.test_quint_dependency_replay.REPLAY_DIR", None)
    gen1 = fixture_fn()
    cache1 = next(gen1)
    try:
        assert cache1._owns_dir is True
    finally:
        next(gen1, None)

    # With REPLAY_DIR
    replay_dir = tmp_path / "replay_session"
    monkeypatch.setattr(
        "tests.test_quint_dependency_replay.REPLAY_DIR", str(replay_dir)
    )
    gen2 = fixture_fn()
    cache2 = next(gen2)
    try:
        assert cache2._owns_dir is False
        assert cache2.session_dir == (replay_dir / "_scenario_cache").resolve()
    finally:
        next(gen2, None)


# ---- StoredTraceCache tests --------------------------------------------------


def test_stored_trace_cache_cold_and_hit() -> None:
    from tests.quint_scenarios import TRACES_FIXTURE, load_fixture

    stored_entries = {
        entry["id"]: entry for entry in load_fixture(TRACES_FIXTURE)["traces"]
    }
    fake_traces = {"final_apply": stored_entries["final_apply"]}
    parse_calls: list[str] = []

    def mock_parse(itf: dict[str, Any]) -> Trace:
        parse_calls.append("called")
        from tests.quint_replay import parse_trace

        return parse_trace(itf)

    cache = StoredTraceCache(stored=fake_traces, parser=mock_parse)

    # First call (cold)
    t1 = cache.get("final_apply")
    assert len(parse_calls) == 1
    assert len(t1.steps) > 0

    # Second call (hit)
    t2 = cache.get("final_apply")
    assert len(parse_calls) == 1  # parser not called again
    assert len(t2.steps) == len(t1.steps)
    assert t2 is not t1

    # Deepcopy isolation test: mutate t1 inner state
    assert isinstance(t1.steps[0].state, dict)
    t1.steps[0].state["mutated_key"] = "leak"
    assert "mutated_key" not in t2.steps[0].state

    # Callable interface
    t3 = cache("final_apply")
    assert len(parse_calls) == 1
    assert "mutated_key" not in t3.steps[0].state


def test_stored_trace_cache_unknown_scenario() -> None:
    cache = StoredTraceCache(stored={})
    with pytest.raises(KeyError, match="unknown stored scenario"):
        cache.get("non_existent")


# ---- Batch prewarm & classify_run tests (#1447) -------------------------------


def test_classify_run_ok() -> None:
    stdout = "Running...\n[ok] No violation found (38ms at 26 traces/second).\n"
    outcome, violated = classify_run(0, stdout)
    assert outcome == "ok"
    assert violated == ()


def test_classify_run_violation() -> None:
    stdout = (
        "Running...\n"
        "[violation] Found an issue (37ms at 27 traces/second).\n"
        "  ❌ liveness\n"
        "  ❌ safety\n"
    )
    outcome, violated = classify_run(1, stdout)
    assert outcome == "violation"
    assert violated == ("liveness", "safety")


def test_classify_run_error() -> None:
    # Non-zero returncode but no violation marker
    outcome1, violated1 = classify_run(1, "Syntax error in model\n")
    assert outcome1 == "error"
    assert violated1 == ()

    # Returncode is 0 but missing [ok]
    outcome2, _ = classify_run(0, "Something unexpected\n")
    assert outcome2 == "error"

    # Returncode is 1 with [violation] but no invariants extracted
    outcome3, _ = classify_run(1, "[violation] Found an issue but no invariant\n")
    assert outcome3 == "error"

    # Returncode > 1
    outcome4, _ = classify_run(2, "[ok] No violation found\n")
    assert outcome4 == "error"


def test_scenario_run_cache_prewarm_called_on_cold_miss_and_avoids_runner(
    tmp_path: Path,
) -> None:
    prewarmer_calls: list[list[str]] = []
    runner_calls: list[str] = []

    def fake_prewarmer(missing: Sequence[str], session_dir: Path) -> set[str]:
        prewarmer_calls.append(list(missing))
        for sc in missing:
            sc_dir = session_dir / sc
            sc_dir.mkdir(parents=True, exist_ok=True)
            run = _fake_ok_runner(sc_dir, sc)
            _persist(sc_dir, run)
        return set(missing)

    def fake_runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        assert out_dir is not None
        runner_calls.append(scenario)
        return _fake_ok_runner(out_dir, scenario)

    cache = ScenarioRunCache(
        known_scenarios=("final_apply", "fault_suppress"),
        runner=fake_runner,
        prewarmer=fake_prewarmer,
        session_dir=tmp_path,
    )
    try:
        # First call: triggers prewarmer for all known scenarios
        run1 = cache.get("final_apply")
        assert run1.outcome == "ok"
        assert len(prewarmer_calls) == 1
        assert sorted(prewarmer_calls[0]) == ["fault_suppress", "final_apply"]
        assert len(runner_calls) == 0

        # Second call for the other scenario: disk/memory hit, runner and prewarmer not called
        run2 = cache.get("fault_suppress")
        assert run2.outcome == "ok"
        assert len(prewarmer_calls) == 1
        assert len(runner_calls) == 0

        # Third call: memory hit
        run3 = cache.get("final_apply")
        assert run3.outcome == "ok"
        assert len(prewarmer_calls) == 1
        assert len(runner_calls) == 0
    finally:
        cache.close()


def test_scenario_run_cache_partial_prewarm_falls_back_to_runner(
    tmp_path: Path,
) -> None:
    prewarmer_calls: list[list[str]] = []
    runner_calls: list[str] = []

    def partial_prewarmer(missing: Sequence[str], session_dir: Path) -> set[str]:
        prewarmer_calls.append(list(missing))
        # Only prewarm final_apply, omit fault_suppress
        if "final_apply" in missing:
            sc_dir = session_dir / "final_apply"
            sc_dir.mkdir(parents=True, exist_ok=True)
            run = _fake_ok_runner(sc_dir, "final_apply")
            _persist(sc_dir, run)
            return {"final_apply"}
        return set()

    def fake_runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        assert out_dir is not None
        runner_calls.append(scenario)
        return _fake_ok_runner(out_dir, scenario)

    cache = ScenarioRunCache(
        known_scenarios=("final_apply", "fault_suppress"),
        runner=fake_runner,
        prewarmer=partial_prewarmer,
        session_dir=tmp_path,
    )
    try:
        # Request fault_suppress (which prewarmer omits)
        run = cache.get("fault_suppress")
        assert run.outcome == "ok"
        assert len(prewarmer_calls) == 1
        # Runner was called for fault_suppress
        assert runner_calls == ["fault_suppress"]

        # Request final_apply (which was prewarmed during the batch)
        run_apply = cache.get("final_apply")
        assert run_apply.outcome == "ok"
        # Prewarmer not called again, runner not called for final_apply
        assert len(prewarmer_calls) == 1
        assert runner_calls == ["fault_suppress"]
    finally:
        cache.close()


def test_scenario_run_cache_prewarmer_exception_disables_and_falls_back(
    tmp_path: Path,
) -> None:
    prewarmer_calls: list[list[str]] = []
    runner_calls: list[str] = []

    def failing_prewarmer(missing: Sequence[str], session_dir: Path) -> set[str]:
        prewarmer_calls.append(list(missing))
        raise RuntimeError("prewarmer crashed")

    def fake_runner(scenario: str, out_dir: Path | None = None) -> ModelRun:
        assert out_dir is not None
        runner_calls.append(scenario)
        return _fake_ok_runner(out_dir, scenario)

    cache = ScenarioRunCache(
        known_scenarios=("final_apply", "fault_suppress"),
        runner=fake_runner,
        prewarmer=failing_prewarmer,
        session_dir=tmp_path,
    )
    try:
        # First call: prewarmer raises, falls back to runner
        run1 = cache.get("final_apply")
        assert run1.outcome == "ok"
        assert len(prewarmer_calls) == 1
        assert runner_calls == ["final_apply"]

        # Second call: prewarmer was disabled on instance, goes straight to runner
        run2 = cache.get("fault_suppress")
        assert run2.outcome == "ok"
        assert len(prewarmer_calls) == 1  # Not called again
        assert runner_calls == ["final_apply", "fault_suppress"]
    finally:
        cache.close()


@pytest.mark.integration
def test_batch_prewarm_matches_run_scenario_for_ok_and_violation(
    tmp_path: Path,
) -> None:
    import json

    scenarios = ("final_apply", "fault_suppress")
    batch_dir = tmp_path / "batch"
    batch_dir.mkdir()

    persisted = _batch_prewarm_scenarios(scenarios, batch_dir)
    assert persisted == set(scenarios)

    single_dir = tmp_path / "single"
    single_dir.mkdir()

    cache = ScenarioRunCache(
        known_scenarios=scenarios,
        prewarmer=_batch_prewarm_scenarios,
        session_dir=batch_dir,
    )
    try:
        for sc in scenarios:
            batch_run = cache.get(sc)
            single_run = run_scenario(sc, out_dir=single_dir / sc)

            assert batch_run.outcome == single_run.outcome
            assert batch_run.returncode == single_run.returncode
            assert batch_run.violated == single_run.violated

            expected_cmd = command_line(
                SCENARIO_BOUNDS,
                init=f"init_{sc}",
                out_dir=batch_dir / sc,
                model=str(MODEL),
                executable=str(quint_executable()),
            )
            assert batch_run.command == tuple(expected_cmd)

            assert len(batch_run.traces) == 1
            assert len(single_run.traces) == 1
            batch_itf = json.loads(batch_run.traces[0].read_text(encoding="utf-8"))
            single_itf = json.loads(single_run.traces[0].read_text(encoding="utf-8"))
            assert normalized(batch_itf) == normalized(single_itf)
    finally:
        cache.close()
